from __future__ import annotations

import configparser
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # pragma: no cover - exercised only on Python < 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(ValueError):
    """Raised when a configuration file cannot be interpreted."""


@dataclass(frozen=True)
class ServerServiceConfig:
    name: str
    bind_host: str
    bind_port: int
    token: str | None = None


@dataclass(frozen=True)
class ServerConfig:
    bind_host: str = "0.0.0.0"
    bind_port: int = 7000
    token: str | None = None
    services: tuple[ServerServiceConfig, ...] = ()
    allow_dynamic: bool = True
    source_flavor: str = "py-frp"
    open_timeout: float = 15.0


@dataclass(frozen=True)
class ProxyConfig:
    name: str
    local_host: str
    local_port: int
    remote_host: str | None = None
    remote_port: int | None = None
    token: str | None = None


@dataclass(frozen=True)
class ClientConfig:
    server_host: str
    server_port: int
    proxies: tuple[ProxyConfig, ...]
    token: str | None = None
    source_flavor: str = "py-frp"
    reconnect_delay: float = 3.0
    connect_timeout: float = 10.0
    heartbeat_interval: float = 30.0


def load_server_config(path: str | Path) -> ServerConfig:
    path = Path(path)
    if path.suffix.lower() in {".ini", ".conf"}:
        return _load_frp_ini_server(path)

    data = _load_mapping_file(path)
    if _looks_like_rathole_server(data):
        return _load_rathole_server(data)
    return _load_frp_or_native_server(data)


def load_client_config(path: str | Path) -> ClientConfig:
    path = Path(path)
    if path.suffix.lower() in {".ini", ".conf"}:
        return _load_frp_ini_client(path)

    data = _load_mapping_file(path)
    if _looks_like_rathole_client(data):
        return _load_rathole_client(data)
    return _load_frp_or_native_client(data)


def privileged_listen_ports(config: ServerConfig) -> tuple[int, ...]:
    ports = [config.bind_port]
    ports.extend(service.bind_port for service in config.services)
    return tuple(port for port in ports if 0 < port < 1024)


def describe_server_config(config: ServerConfig) -> str:
    dynamic = "dynamic" if config.allow_dynamic else "preconfigured"
    return (
        f"{config.bind_host}:{config.bind_port} "
        f"({config.source_flavor}, {dynamic}, services={len(config.services)})"
    )


def describe_client_config(config: ClientConfig) -> str:
    return (
        f"{config.server_host}:{config.server_port} "
        f"({config.source_flavor}, proxies={len(config.proxies)})"
    )


def _load_mapping_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        else:
            with path.open("rb") as file:
                data = tomllib.load(file)
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid configuration syntax in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be a table/object")
    return data


def _load_frp_or_native_server(data: Mapping[str, Any]) -> ServerConfig:
    server_table = _as_mapping(_get_any(data, "server"), default={})
    root = server_table if server_table and _get_any(data, "mode") == "server" else data

    bind_addr = _get_any(root, "bind_addr", "bindAddr")
    default_host = _string(_get_any(root, "bind_host", "bindHost"), "0.0.0.0")
    default_port = _int(_get_any(root, "bind_port", "bindPort"), 7000)
    bind_host, bind_port = _addr_from_fields(bind_addr, default_host, default_port)

    token = _string_or_none(
        _get_any(root, "token", "auth_token", "authToken", default=_auth_token(root))
    )
    allow_dynamic = _bool(_get_any(root, "allow_dynamic", "allowDynamic"), True)
    open_timeout = _positive_float(_get_any(root, "open_timeout", "openTimeout"), 15.0)
    source = "py-frp" if _get_any(data, "mode") else "frp"

    services = tuple(_native_server_services(root, token))
    return ServerConfig(
        bind_host=bind_host,
        bind_port=_port(bind_port, allow_zero=True),
        token=token,
        services=services,
        allow_dynamic=allow_dynamic,
        source_flavor=source,
        open_timeout=open_timeout,
    )


def _load_frp_or_native_client(data: Mapping[str, Any]) -> ClientConfig:
    server_addr = _get_any(data, "server_addr", "serverAddr", "remote_addr", "remoteAddr")
    default_host = _string(_get_any(data, "server_host", "serverHost"), "127.0.0.1")
    default_port = _int(_get_any(data, "server_port", "serverPort"), 7000)
    server_host, server_port = _addr_from_fields(server_addr, default_host, default_port)

    token = _string_or_none(_get_any(data, "token", default=_auth_token(data)))
    reconnect_delay = _positive_float(_get_any(data, "reconnect_delay", "reconnectDelay"), 3.0)
    connect_timeout = _positive_float(_get_any(data, "connect_timeout", "connectTimeout"), 10.0)
    heartbeat_interval = _positive_float(
        _get_any(data, "heartbeat_interval", "heartbeatInterval"),
        30.0,
    )
    proxy_rows = _get_any(data, "proxies", "services", default=())
    if isinstance(proxy_rows, Mapping):
        proxy_rows = [
            {"name": name, **_as_mapping(value)}
            for name, value in proxy_rows.items()
        ]
    if not isinstance(proxy_rows, Iterable) or isinstance(proxy_rows, (str, bytes)):
        raise ConfigError("client proxies/services must be a list or table")

    proxies = [_frp_or_native_proxy(row, token) for row in proxy_rows]
    if not proxies:
        raise ConfigError("client configuration must define at least one proxy")

    source = "py-frp" if _get_any(data, "mode") else "frp"
    return ClientConfig(
        server_host=server_host,
        server_port=_port(server_port, allow_zero=False),
        token=token,
        proxies=tuple(proxies),
        source_flavor=source,
        reconnect_delay=reconnect_delay,
        connect_timeout=connect_timeout,
        heartbeat_interval=heartbeat_interval,
    )


def _load_rathole_server(data: Mapping[str, Any]) -> ServerConfig:
    server = _required_mapping(data, "server")
    bind_host, bind_port = _split_addr(
        _string(_get_any(server, "bind_addr", "bindAddr"), "0.0.0.0:2333"),
        default_host="0.0.0.0",
    )
    default_token = _string_or_none(_get_any(server, "default_token", "defaultToken"))
    open_timeout = _positive_float(_get_any(server, "open_timeout", "openTimeout"), 15.0)
    services_table = _as_mapping(_get_any(server, "services"), default={})
    services: list[ServerServiceConfig] = []

    for name, raw_service in services_table.items():
        service = _as_mapping(raw_service)
        bind_addr = _get_any(service, "bind_addr", "bindAddr")
        if bind_addr is None:
            raise ConfigError(f"rathole server service {name!r} is missing bind_addr")
        host, port = _split_addr(_string(bind_addr), default_host="0.0.0.0")
        services.append(
            ServerServiceConfig(
                name=_service_name(name),
                bind_host=host,
                bind_port=_port(port, allow_zero=True),
                token=_string_or_none(_get_any(service, "token", default=default_token)),
            )
        )

    return ServerConfig(
        bind_host=bind_host,
        bind_port=_port(bind_port, allow_zero=True),
        token=default_token,
        services=tuple(services),
        allow_dynamic=_bool(_get_any(server, "allow_dynamic", "allowDynamic"), False),
        source_flavor="rathole",
        open_timeout=open_timeout,
    )


def _load_rathole_client(data: Mapping[str, Any]) -> ClientConfig:
    client = _required_mapping(data, "client")
    server_host, server_port = _split_addr(
        _string(_get_any(client, "remote_addr", "remoteAddr"), "127.0.0.1:2333"),
        default_host="127.0.0.1",
    )
    default_token = _string_or_none(_get_any(client, "default_token", "defaultToken"))
    reconnect_delay = _positive_float(_get_any(client, "reconnect_delay", "reconnectDelay"), 3.0)
    connect_timeout = _positive_float(_get_any(client, "connect_timeout", "connectTimeout"), 10.0)
    heartbeat_interval = _positive_float(
        _get_any(client, "heartbeat_interval", "heartbeatInterval"),
        30.0,
    )
    services_table = _as_mapping(_get_any(client, "services"), default={})
    proxies: list[ProxyConfig] = []

    for name, raw_service in services_table.items():
        service = _as_mapping(raw_service)
        local_addr = _get_any(service, "local_addr", "localAddr")
        if local_addr is None:
            raise ConfigError(f"rathole client service {name!r} is missing local_addr")
        local_host, local_port = _split_addr(_string(local_addr), default_host="127.0.0.1")
        proxies.append(
            ProxyConfig(
                name=_service_name(name),
                local_host=local_host,
                local_port=_port(local_port, allow_zero=False),
                token=_string_or_none(_get_any(service, "token", default=default_token)),
            )
        )

    if not proxies:
        raise ConfigError("rathole client configuration must define services")
    return ClientConfig(
        server_host=server_host,
        server_port=_port(server_port, allow_zero=False),
        token=default_token,
        proxies=tuple(proxies),
        source_flavor="rathole",
        reconnect_delay=reconnect_delay,
        connect_timeout=connect_timeout,
        heartbeat_interval=heartbeat_interval,
    )


def _load_frp_ini_server(path: Path) -> ServerConfig:
    parser = _load_ini(path)
    common = parser["common"] if parser.has_section("common") else parser.defaults()
    bind_addr = common.get("bind_addr")
    bind_host = common.get("bind_host", "0.0.0.0")
    bind_port = _int(common.get("bind_port", "7000"), 7000)
    if bind_addr and ":" in bind_addr:
        bind_host, bind_port = _split_addr(bind_addr, default_host="0.0.0.0")
    elif bind_addr:
        bind_host = bind_addr
    token = _empty_to_none(common.get("token") or common.get("auth_token"))
    allow_dynamic = common.get("allow_dynamic", "true").lower() not in {"0", "false", "no"}
    return ServerConfig(
        bind_host=bind_host,
        bind_port=_port(bind_port, allow_zero=True),
        token=token,
        allow_dynamic=allow_dynamic,
        source_flavor="frp-ini",
    )


def _load_frp_ini_client(path: Path) -> ClientConfig:
    parser = _load_ini(path)
    if not parser.has_section("common"):
        raise ConfigError("frp ini client config requires a [common] section")
    common = parser["common"]
    server_host = common.get("server_addr", "127.0.0.1")
    server_port = _int(common.get("server_port", "7000"), 7000)
    token = _empty_to_none(common.get("token") or common.get("auth_token"))
    proxies: list[ProxyConfig] = []

    for section in parser.sections():
        if section == "common":
            continue
        row = parser[section]
        proxy_type = row.get("type", "tcp").lower()
        if proxy_type != "tcp":
            raise ConfigError(f"only tcp proxies are supported, got {proxy_type!r}")
        local_addr = row.get("local_addr")
        if local_addr:
            local_host, local_port = _split_addr(local_addr, default_host="127.0.0.1")
        else:
            local_host = row.get("local_ip", row.get("local_host", "127.0.0.1"))
            local_port = _int(_required_value(row, "local_port", section), 0)
        remote_host = row.get("remote_host") or row.get("remote_ip") or "0.0.0.0"
        remote_port = _int(_required_value(row, "remote_port", section), 0)
        proxies.append(
            ProxyConfig(
                name=_service_name(section),
                local_host=local_host,
                local_port=_port(local_port, allow_zero=False),
                remote_host=remote_host,
                remote_port=_port(remote_port, allow_zero=True),
                token=_empty_to_none(row.get("token")),
            )
        )

    if not proxies:
        raise ConfigError("frp ini client configuration must define at least one proxy")
    return ClientConfig(
        server_host=server_host,
        server_port=_port(server_port, allow_zero=False),
        token=token,
        proxies=tuple(proxies),
        source_flavor="frp-ini",
    )


def _native_server_services(
    root: Mapping[str, Any],
    default_token: str | None,
) -> Iterable[ServerServiceConfig]:
    services_table = _as_mapping(_get_any(root, "services"), default={})
    for name, raw_service in services_table.items():
        service = _as_mapping(raw_service)
        bind_addr = _get_any(service, "bind_addr", "bindAddr", "remote_addr", "remoteAddr")
        default_host = _string(_get_any(service, "bind_host", "bindHost"), "0.0.0.0")
        default_port = _get_any(service, "bind_port", "bindPort", "remote_port", "remotePort")
        if bind_addr is None and default_port is None:
            raise ConfigError(f"server service {name!r} is missing bind_addr or bind_port")
        host, port = _addr_from_fields(bind_addr, default_host, _int(default_port, 0))
        yield ServerServiceConfig(
            name=_service_name(name),
            bind_host=host,
            bind_port=_port(port, allow_zero=True),
            token=_string_or_none(_get_any(service, "token", default=default_token)),
        )


def _frp_or_native_proxy(row: Any, default_token: str | None) -> ProxyConfig:
    proxy = _as_mapping(row)
    proxy_type = _string(_get_any(proxy, "type"), "tcp").lower()
    if proxy_type != "tcp":
        raise ConfigError(f"only tcp proxies are supported, got {proxy_type!r}")
    name = _service_name(_get_any(proxy, "name"))

    local_addr = _get_any(proxy, "local_addr", "localAddr")
    if local_addr is not None:
        local_host, local_port = _split_addr(_string(local_addr), default_host="127.0.0.1")
    else:
        local_host = _string(
            _get_any(proxy, "local_ip", "localIP", "local_host", "localHost"),
            "127.0.0.1",
        )
        local_port = _int(_required_any(proxy, "local_port", "localPort"), 0)

    remote_addr = _get_any(proxy, "remote_addr", "remoteAddr")
    if remote_addr is not None:
        remote_host, remote_port = _split_addr(_string(remote_addr), default_host="0.0.0.0")
    else:
        remote_port_value = _get_any(proxy, "remote_port", "remotePort")
        remote_host = _string(
            _get_any(proxy, "remote_host", "remoteHost", "remote_ip", "remoteIP"),
            "0.0.0.0",
        )
        remote_port = None if remote_port_value is None else _int(remote_port_value, 0)

    if remote_port is None:
        raise ConfigError(f"proxy {name!r} is missing remote_port/remotePort")

    return ProxyConfig(
        name=name,
        local_host=local_host,
        local_port=_port(local_port, allow_zero=False),
        remote_host=remote_host,
        remote_port=_port(remote_port, allow_zero=True),
        token=_string_or_none(_get_any(proxy, "token", default=default_token)),
    )


def _load_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def _looks_like_rathole_server(data: Mapping[str, Any]) -> bool:
    server = _get_any(data, "server")
    return isinstance(server, Mapping) and (
        "bind_addr" in server or "bindAddr" in server or "services" in server
    )


def _looks_like_rathole_client(data: Mapping[str, Any]) -> bool:
    client = _get_any(data, "client")
    return isinstance(client, Mapping) and (
        "remote_addr" in client or "remoteAddr" in client or "services" in client
    )


def _required_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise ConfigError(f"configuration requires [{key}] table")
    return value


def _as_mapping(value: Any, default: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if value is None and default is not None:
        return default
    if not isinstance(value, Mapping):
        raise ConfigError("expected a table/object")
    return value


def _get_any(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _required_any(mapping: Mapping[str, Any], *keys: str) -> Any:
    value = _get_any(mapping, *keys)
    if value is None:
        joined = "/".join(keys)
        raise ConfigError(f"missing required field {joined}")
    return value


def _required_value(section: Mapping[str, str], key: str, section_name: str) -> str:
    value = section.get(key)
    if value in {None, ""}:
        raise ConfigError(f"section [{section_name}] is missing {key}")
    return value


def _addr_from_fields(
    addr: Any,
    default_host: str,
    default_port: int,
) -> tuple[str, int]:
    if addr is None:
        return default_host, default_port
    text = _string(addr)
    if ":" not in text:
        return text, default_port
    return _split_addr(text, default_host=default_host)


def _split_addr(value: str, *, default_host: str) -> tuple[str, int]:
    value = value.strip()
    if not value:
        raise ConfigError("address must not be empty")
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\]:(\d+)", value)
        if not match:
            raise ConfigError(f"invalid address {value!r}")
        return match.group(1), _int(match.group(2), 0)
    if ":" not in value:
        raise ConfigError(f"address {value!r} must include a port")
    host, port_text = value.rsplit(":", 1)
    return host or default_host, _int(port_text, 0)


def _port(value: int, *, allow_zero: bool) -> int:
    lower = 0 if allow_zero else 1
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"port {value!r} is not an integer") from exc
    if not lower <= port <= 65535:
        raise ConfigError(f"port {value!r} is outside the valid range")
    return port


def _int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"value {value!r} is not an integer") from exc


def _positive_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"value {value!r} is not a number") from exc
    if result <= 0:
        raise ConfigError(f"value {value!r} must be greater than zero")
    return result


def _string(value: Any, default: str | None = None) -> str:
    if value is None:
        if default is None:
            raise ConfigError("missing required string value")
        return default
    return str(value)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return _empty_to_none(str(value))


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _auth_token(mapping: Mapping[str, Any]) -> str | None:
    auth = _get_any(mapping, "auth")
    if isinstance(auth, Mapping):
        return _string_or_none(_get_any(auth, "token"))
    return None


def _service_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ConfigError("service/proxy name must not be empty")
    return name
