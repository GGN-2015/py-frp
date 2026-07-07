from __future__ import annotations

import hashlib
import secrets
import string

from .config import ConfigError


TOKEN_ALPHABET = "".join(
    char for char in string.ascii_letters + string.digits if char not in {"I", "O", "0", "1", "l"}
)


def generate_tokens(count: int, *, length: int = 24) -> tuple[str, ...]:
    if count <= 0:
        raise ConfigError("token count must be greater than zero")
    if length <= 0:
        raise ConfigError("token length must be greater than zero")
    tokens: set[str] = set()
    while len(tokens) < count:
        tokens.add("".join(secrets.choice(TOKEN_ALPHABET) for _ in range(length)))
    return tuple(tokens)


def parse_port_pool(value: str) -> tuple[int, ...]:
    ports: list[int] = []
    seen: set[int] = set()
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start = _port(start_text)
            end = _port(end_text)
            if end < start:
                raise ConfigError(f"invalid descending port range {item!r}")
            candidates = range(start, end + 1)
        else:
            candidates = (_port(item),)
        for port in candidates:
            if port not in seen:
                seen.add(port)
                ports.append(port)
    if not ports:
        raise ConfigError("port pool must include at least one port")
    return tuple(ports)


def token_service_name(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return f"token-{digest}"


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ConfigError(f"port {value!r} is not an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"port {port!r} is outside the valid range")
    return port
