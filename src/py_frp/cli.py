from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from . import __version__
from .client import Client
from .config import (
    ClientConfig,
    ConfigError,
    ProxyConfig,
    ServerConfig,
    describe_client_config,
    describe_server_config,
    load_client_config,
    load_server_config,
)
from .elevate import ElevationError, is_admin, relaunch_once, should_elevate_server
from .pool import generate_tokens, parse_port_pools, token_service_name
from .server import Server


LOGGER = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(effective_argv)
    _configure_logging(args.log_level)

    try:
        if args.command == "server":
            return _run_server_command(args, effective_argv)
        if args.command == "client":
            return _run_client_command(args)
    except KeyboardInterrupt:
        return 130
    except ConfigError as exc:
        LOGGER.error("configuration error: %s", exc)
        return 2
    except ElevationError as exc:
        LOGGER.error("elevation failed: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("fatal runtime error")
        return 1
    return 2


def server_main(argv: Sequence[str] | None = None) -> int:
    return main(["server", *(sys.argv[1:] if argv is None else argv)])


def client_main(argv: Sequence[str] | None = None) -> int:
    return main(["client", *(sys.argv[1:] if argv is None else argv)])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="py-frp",
        description="A small Python TCP reverse tunnel.",
    )
    parser.add_argument("--version", action="version", version=f"py-frp {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("server", aliases=["frps"], help="run the public server")
    _add_runtime_options(server)
    server.add_argument("-c", "--config", help="server config path")
    server.add_argument("--bind-host", default="0.0.0.0", help="control bind host for configless mode")
    server.add_argument("--bind-port", type=int, default=7000, help="control bind port for configless mode")
    server.add_argument(
        "--port-pool",
        action="append",
        help=(
            "repeatable public ports/ranges for configless token-pool mode, "
            "e.g. --port-pool 6000-6010 --port-pool 7000"
        ),
    )
    server.add_argument("--pool-bind-host", help="public port bind host for configless mode")
    server.add_argument("--token-length", type=int, default=24, help="generated token length")
    server.add_argument(
        "--elevate",
        action="store_true",
        help="relaunch as administrator/root once before binding ports",
    )
    server.add_argument(
        "--auto-elevate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="auto elevate once when configured listen ports are privileged",
    )
    server.add_argument("--elevation-attempted", action="store_true", help=argparse.SUPPRESS)

    client = subparsers.add_parser("client", aliases=["frpc"], help="run the private client")
    _add_runtime_options(client)
    client.add_argument("-c", "--config", help="client config path")
    client.add_argument("--server", help="server control address for configless mode, e.g. example.com:7000")
    client.add_argument("--token", help="token for configless mode")
    client.add_argument(
        "--server-fingerprint",
        help="trusted SHA-256 TLS fingerprint; omit to confirm interactively",
    )
    client.add_argument(
        "--local",
        default="127.0.0.1:22",
        help=(
            "target host and port reachable from this client (including a LAN host); "
            "defaults to 127.0.0.1:22"
        ),
    )
    client.add_argument("--reconnect-delay", type=float, default=3.0, help="client reconnect delay")
    client.add_argument("--connect-timeout", type=float, default=10.0, help="connection timeout")
    client.add_argument("--heartbeat-interval", type=float, default=30.0, help="control heartbeat interval")

    return parser


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="logging level",
    )


def _run_server_command(args: argparse.Namespace, effective_argv: Sequence[str]) -> int:
    config = _load_server_command_config(args)
    LOGGER.info("loaded server config: %s", describe_server_config(config))
    needs_elevation = args.elevate or (args.auto_elevate and should_elevate_server(config))
    if needs_elevation and not args.elevation_attempted and not is_admin():
        LOGGER.info("relaunching with administrator/root privileges")
        return relaunch_once(effective_argv)
    if config.port_pool:
        _print_token_pool(config)
    return asyncio.run(Server(config).serve_forever())


def _run_client_command(args: argparse.Namespace) -> int:
    config = _load_client_command_config(args)
    LOGGER.info("loaded client config: %s", describe_client_config(config))
    callback = _print_assigned_ports if config.source_flavor == "token-pool" else None
    asyncio.run(Client(config, on_registered=callback).run())
    return 0


def _load_server_command_config(args: argparse.Namespace) -> ServerConfig:
    if args.config:
        return load_server_config(args.config)
    if not args.port_pool:
        raise ConfigError("server requires --config or --port-pool")
    ports = parse_port_pools(args.port_pool)
    tokens = generate_tokens(1, length=args.token_length)
    return ServerConfig(
        bind_host=args.bind_host,
        bind_port=args.bind_port,
        services=(),
        allow_dynamic=False,
        source_flavor="token-pool",
        port_pool=ports,
        pool_tokens=tokens,
        pool_bind_host=args.pool_bind_host or args.bind_host,
    )


def _load_client_command_config(args: argparse.Namespace) -> ClientConfig:
    if args.config:
        return load_client_config(args.config)
    if not args.server or not args.token:
        raise ConfigError("client requires --config or both --server and --token")
    server_host, server_port = _split_host_port(args.server, default_host="127.0.0.1")
    local_host, local_port = _split_host_port(args.local, default_host="127.0.0.1")
    token = str(args.token)
    return ClientConfig(
        server_host=server_host,
        server_port=server_port,
        token=token,
        source_flavor="token-pool",
        reconnect_delay=_positive_float(args.reconnect_delay, "reconnect delay"),
        connect_timeout=_positive_float(args.connect_timeout, "connect timeout"),
        heartbeat_interval=_positive_float(args.heartbeat_interval, "heartbeat interval"),
        server_fingerprint=args.server_fingerprint,
        proxies=(
            ProxyConfig(
                name=token_service_name(token),
                local_host=local_host,
                local_port=local_port,
                token=token,
            ),
        ),
    )


def _split_host_port(value: str, *, default_host: str) -> tuple[str, int]:
    text = value.strip()
    if text.startswith("["):
        host, separator, port_text = text.rpartition("]:")
        if not separator:
            raise ConfigError(f"address {value!r} must include a port")
        host = host.removeprefix("[")
    else:
        host, separator, port_text = text.rpartition(":")
        if not separator:
            raise ConfigError(f"address {value!r} must include a port")
    return host or default_host, _port(port_text)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ConfigError(f"port {value!r} is not an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"port {port!r} is outside the valid range")
    return port


def _positive_float(value: float, label: str) -> float:
    if value <= 0:
        raise ConfigError(f"{label} must be greater than zero")
    return value


def _print_token_pool(config: ServerConfig) -> None:
    print("py-frp token pool", flush=True)
    print(f"control {config.bind_host}:{config.bind_port}", flush=True)
    print(f"public_bind {config.pool_bind_host}", flush=True)
    for token in config.pool_tokens:
        print(f"token {token}", flush=True)


def _print_assigned_ports(message: dict[str, object]) -> None:
    services = message.get("services")
    if not isinstance(services, list):
        return
    for service in services:
        if not isinstance(service, dict):
            continue
        port = service.get("bind_port")
        if port is not None:
            print(port, flush=True)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
