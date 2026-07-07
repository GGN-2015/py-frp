from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import ServerConfig, ServerServiceConfig
from .protocol import ProtocolError, close_writer, pipe_streams, read_message, write_message


LOGGER = logging.getLogger(__name__)


class AuthenticationError(ProtocolError):
    """Raised when a peer presents a wrong token."""


@dataclass
class ClientSession:
    id: str
    writer: asyncio.StreamWriter
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.write_lock:
            await write_message(self.writer, message)


@dataclass
class RegisteredService:
    name: str
    bind_host: str
    bind_port: int
    token: str | None
    client: ClientSession
    listener: asyncio.AbstractServer
    configured: bool

    @property
    def public_address(self) -> tuple[str, int]:
        sockets = self.listener.sockets or ()
        if sockets:
            host, port, *_ = sockets[0].getsockname()
            return str(host), int(port)
        return self.bind_host, self.bind_port


@dataclass
class PendingTunnel:
    id: str
    service_name: str
    token: str | None
    client: ClientSession
    future: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
    created_at: float = field(default_factory=time.monotonic)


class Server:
    def __init__(self, config: ServerConfig):
        self.config = config
        self._configured = {service.name: service for service in config.services}
        self._services: dict[str, RegisteredService] = {}
        self._pending: dict[str, PendingTunnel] = {}
        self._lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_connection,
            self.config.bind_host,
            self.config.bind_port,
        )
        LOGGER.info("control listening on %s", _server_addresses(self._server))

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        async with self._lock:
            services = list(self._services.values())
            self._services.clear()
            pending = list(self._pending.values())
            self._pending.clear()

        for item in pending:
            if not item.future.done():
                item.future.set_exception(ConnectionError("server is closing"))

        for service in services:
            service.listener.close()
        await asyncio.gather(
            *(service.listener.wait_closed() for service in services),
            return_exceptions=True,
        )

    def control_addresses(self) -> tuple[tuple[str, int], ...]:
        if self._server is None or self._server.sockets is None:
            return ()
        return tuple((str(sock.getsockname()[0]), int(sock.getsockname()[1])) for sock in self._server.sockets)

    def service_address(self, name: str) -> tuple[str, int] | None:
        service = self._services.get(name)
        return None if service is None else service.public_address

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            first = await read_message(reader)
            if first is None:
                return
            message_type = first.get("type")
            if message_type == "tunnel":
                await self._handle_tunnel(first, reader, writer)
                return
            await self._handle_control(first, reader, writer)
        except (ProtocolError, OSError, ConnectionError) as exc:
            LOGGER.debug("connection rejected: %s", exc)
            await _best_effort_error(writer, str(exc))
            await close_writer(writer)

    async def _handle_control(
        self,
        first: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        client = ClientSession(id=uuid.uuid4().hex, writer=writer)
        try:
            current = first
            if current.get("type") == "hello":
                await client.send({"type": "hello", "version": 1, "server": "py-frp"})
                current = await read_message(reader) or {}
            if current.get("type") != "register":
                raise ProtocolError("control connection must register services")

            registered = await self._register_client(client, current.get("services"))
            await client.send(
                {
                    "type": "registered",
                    "status": "ok",
                    "client_id": client.id,
                    "services": [
                        {
                            "name": item.name,
                            "bind_host": item.public_address[0],
                            "bind_port": item.public_address[1],
                        }
                        for item in registered
                    ],
                }
            )
            LOGGER.info("client %s registered %d service(s)", client.id, len(registered))

            while True:
                message = await read_message(reader)
                if message is None:
                    break
                message_type = message.get("type")
                if message_type == "ping":
                    await client.send({"type": "pong", "time": message.get("time")})
                elif message_type == "close":
                    break
                else:
                    LOGGER.debug("ignored control message from %s: %s", client.id, message)
        except (ProtocolError, OSError, ConnectionError) as exc:
            LOGGER.warning("client %s disconnected: %s", client.id, exc)
            await _best_effort_error(writer, str(exc))
        finally:
            await self._remove_client(client)
            await close_writer(writer)

    async def _register_client(
        self,
        client: ClientSession,
        raw_services: Any,
    ) -> list[RegisteredService]:
        if not isinstance(raw_services, list) or not raw_services:
            raise ProtocolError("register message must include services")

        registered: list[RegisteredService] = []
        try:
            for raw in raw_services:
                service = self._service_from_registration(raw)
                listener = await asyncio.start_server(
                    lambda reader, writer, name=service.name: self._handle_public(name, reader, writer),
                    service.bind_host,
                    service.bind_port,
                )
                registered_service = RegisteredService(
                    name=service.name,
                    bind_host=service.bind_host,
                    bind_port=service.bind_port,
                    token=service.token,
                    client=client,
                    listener=listener,
                    configured=service.name in self._configured,
                )
                async with self._lock:
                    if service.name in self._services:
                        listener.close()
                        await listener.wait_closed()
                        raise ProtocolError(f"service {service.name!r} is already registered")
                    self._services[service.name] = registered_service
                registered.append(registered_service)
                LOGGER.info(
                    "service %s listening on %s",
                    service.name,
                    _server_addresses(listener),
                )
        except Exception:
            for item in registered:
                async with self._lock:
                    self._services.pop(item.name, None)
                item.listener.close()
                await item.listener.wait_closed()
            raise
        return registered

    def _service_from_registration(self, raw: Any) -> ServerServiceConfig:
        if not isinstance(raw, dict):
            raise ProtocolError("service registration must be an object")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ProtocolError("service name must not be empty")
        provided_token = _optional_string(raw.get("token"))

        configured = self._configured.get(name)
        if configured is not None:
            _require_token(configured.token or self.config.token, provided_token)
            return configured

        if not self.config.allow_dynamic:
            raise ProtocolError(f"service {name!r} is not configured on this server")
        _require_token(self.config.token, provided_token)

        remote_host = str(raw.get("remote_host") or "0.0.0.0")
        remote_port = raw.get("remote_port")
        if remote_port is None:
            raise ProtocolError(f"dynamic service {name!r} is missing remote_port")
        return ServerServiceConfig(
            name=name,
            bind_host=remote_host,
            bind_port=_port(remote_port),
            token=provided_token or self.config.token,
        )

    async def _remove_client(self, client: ClientSession) -> None:
        async with self._lock:
            services = [item for item in self._services.values() if item.client is client]
            for service in services:
                self._services.pop(service.name, None)
            pending = [item for item in self._pending.values() if item.client is client]
            for item in pending:
                self._pending.pop(item.id, None)

        for item in pending:
            if not item.future.done():
                item.future.set_exception(ConnectionError("client disconnected"))

        for service in services:
            service.listener.close()
        await asyncio.gather(
            *(service.listener.wait_closed() for service in services),
            return_exceptions=True,
        )
        if services:
            LOGGER.info("client %s unregistered %d service(s)", client.id, len(services))

    async def _handle_public(
        self,
        service_name: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        async with self._lock:
            service = self._services.get(service_name)
        if service is None:
            await close_writer(writer)
            return

        tunnel_id = secrets.token_urlsafe(24)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = loop.create_future()
        pending = PendingTunnel(
            id=tunnel_id,
            service_name=service_name,
            token=service.token,
            client=service.client,
            future=future,
        )
        async with self._lock:
            self._pending[tunnel_id] = pending

        tunnel_writer: asyncio.StreamWriter | None = None
        try:
            await service.client.send(
                {
                    "type": "open",
                    "id": tunnel_id,
                    "service": service_name,
                }
            )
            tunnel_reader, tunnel_writer = await asyncio.wait_for(
                future,
                timeout=self.config.open_timeout,
            )
            await pipe_streams(reader, writer, tunnel_reader, tunnel_writer)
        except (asyncio.TimeoutError, ConnectionError, OSError, ProtocolError) as exc:
            LOGGER.debug("public connection for %s closed: %s", service_name, exc)
        finally:
            async with self._lock:
                if self._pending.get(tunnel_id) is pending:
                    self._pending.pop(tunnel_id, None)
            if not future.done():
                future.cancel()
            await close_writer(writer)
            await close_writer(tunnel_writer)

    async def _handle_tunnel(
        self,
        message: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        tunnel_id = str(message.get("id") or "")
        service_name = str(message.get("service") or "")
        provided_token = _optional_string(message.get("token"))
        if not tunnel_id or not service_name:
            raise ProtocolError("tunnel message requires id and service")

        async with self._lock:
            pending = self._pending.get(tunnel_id)
        if pending is None:
            raise ProtocolError("unknown tunnel id")
        if pending.service_name != service_name:
            raise ProtocolError("tunnel service does not match pending connection")
        _require_token(pending.token, provided_token)

        if not pending.future.done():
            pending.future.set_result((reader, writer))
        LOGGER.debug("paired tunnel %s for service %s", tunnel_id, service_name)


async def _best_effort_error(writer: asyncio.StreamWriter, error: str) -> None:
    if writer.is_closing():
        return
    try:
        await write_message(writer, {"type": "error", "error": error})
    except Exception:
        pass


def _require_token(expected: str | None, provided: str | None) -> None:
    if expected is not None and provided != expected:
        raise AuthenticationError("authentication failed")


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _port(value: Any) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise ProtocolError("remote_port is outside the valid range")
    return port


def _server_addresses(server: asyncio.AbstractServer) -> str:
    sockets = server.sockets or ()
    return ", ".join(f"{sock.getsockname()[0]}:{sock.getsockname()[1]}" for sock in sockets)
