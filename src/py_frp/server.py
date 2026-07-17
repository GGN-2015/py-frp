import asyncio
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .compat import get_running_loop
from .config import ServerConfig, ServerServiceConfig
from .protocol import ProtocolError, close_writer, pipe_tunnel_streams, read_message, write_message
from .security import ServerTLS, create_server_tls


LOGGER = logging.getLogger(__name__)
SERVER_RESTART_RETRY_DELAY = 3.0


class AuthenticationError(ProtocolError):
    """Raised when a peer presents a wrong token."""


class ResourceExhaustedError(ProtocolError):
    """Raised when no pool port can be assigned to a client."""


class ForceRequiredError(ResourceExhaustedError):
    """Raised when a pool client may take a port by evicting another client."""


class PriorityPreemptionError(ResourceExhaustedError):
    """Raised when a forced client cannot preempt any existing pool client."""

    def __init__(self, priority: int, max_priority: int):
        self.priority = priority
        self.max_priority = max_priority
        super().__init__(
            f"forced connection denied: client priority {priority} cannot preempt any "
            f"existing client; maximum existing priority is {max_priority}"
        )


@dataclass
class ClientSession:
    id: str
    writer: asyncio.StreamWriter
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.monotonic)

    async def send(self, message: Dict[str, Any]) -> None:
        async with self.write_lock:
            await write_message(self.writer, message)


@dataclass
class RegisteredService:
    name: str
    bind_host: str
    bind_port: int
    token: Optional[str]
    client: ClientSession
    listener: asyncio.AbstractServer
    configured: bool
    pool_token: Optional[str] = None
    priority: int = 0

    @property
    def public_address(self) -> Tuple[str, int]:
        sockets = self.listener.sockets or ()
        if sockets:
            host, port, *_ = sockets[0].getsockname()
            return str(host), int(port)
        return self.bind_host, self.bind_port


@dataclass
class PendingTunnel:
    id: str
    service_name: str
    token: Optional[str]
    client: ClientSession
    future: asyncio.Future
    created_at: float = field(default_factory=time.monotonic)


class Server:
    def __init__(self, config: ServerConfig):
        self.config = config
        self._configured = {service.name: service for service in config.services}
        self._pool_tokens = set(config.pool_tokens)
        self._services: Dict[str, RegisteredService] = {}
        self._pending: Dict[str, PendingTunnel] = {}
        self._clients: Dict[str, ClientSession] = {}
        self._lock = asyncio.Lock()
        self._server: Optional[asyncio.AbstractServer] = None
        self._tls: Optional[ServerTLS] = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._tls = create_server_tls(self.config.bind_host)
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                self.config.bind_host,
                self.config.bind_port,
                ssl=self._tls.context,
            )
        except Exception:
            self._tls.close()
            self._tls = None
            raise
        print(f"tls_fingerprint {self._tls.fingerprint}", flush=True)
        LOGGER.info("control listening on %s", _server_addresses(self._server))

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        if not hasattr(self._server, "serve_forever"):
            await asyncio.Future()
            return
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if self._tls is not None:
            self._tls.close()
            self._tls = None

        async with self._lock:
            services = list(self._services.values())
            self._services.clear()
            pending = list(self._pending.values())
            self._pending.clear()
            clients = list(self._clients.values())
            self._clients.clear()

        for item in pending:
            if not item.future.done():
                item.future.set_exception(ConnectionError("server is closing"))

        for service in services:
            service.listener.close()
        await asyncio.gather(
            *(service.listener.wait_closed() for service in services),
            *(close_writer(client.writer) for client in clients),
            return_exceptions=True,
        )

    async def notify_restarting(
        self,
        retry_after: float = SERVER_RESTART_RETRY_DELAY,
    ) -> None:
        if retry_after <= 0:
            raise ValueError("restart retry delay must be greater than zero")
        async with self._lock:
            clients = list(self._clients.values())
        message = {
            "type": "server_restarting",
            "reason": "package_update",
            "retry_after": retry_after,
        }
        await asyncio.gather(
            *(self._notify_restarting_client(client, message) for client in clients),
            return_exceptions=True,
        )

    async def _notify_restarting_client(
        self,
        client: ClientSession,
        message: Dict[str, Any],
    ) -> None:
        try:
            await asyncio.wait_for(client.send(message), timeout=1.0)
        except (asyncio.TimeoutError, ConnectionError, OSError, ProtocolError):
            LOGGER.debug("could not notify restarting client %s", client.id)

    def control_addresses(self) -> Tuple[Tuple[str, int], ...]:
        if self._server is None or self._server.sockets is None:
            return ()
        return tuple((str(sock.getsockname()[0]), int(sock.getsockname()[1])) for sock in self._server.sockets)

    @property
    def tls_fingerprint(self) -> str:
        if self._tls is None:
            raise RuntimeError("server has not been started")
        return self._tls.fingerprint

    def preserve_tls_for_restart(self) -> None:
        if self._tls is None:
            raise RuntimeError("server has not been started")
        self._tls.preserve_for_restart()

    def service_address(self, name: str) -> Optional[Tuple[str, int]]:
        service = self._services.get(name)
        return None if service is None else service.public_address

    async def online_pool_clients(self) -> Dict[str, Tuple[str, int]]:
        async with self._lock:
            return {
                service.name: service.public_address
                for service in self._services.values()
                if service.pool_token is not None
            }

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            first = await read_message(reader)
            if first is None:
                await close_writer(writer)
                return
            message_type = first.get("type")
            if message_type == "tunnel":
                await self._handle_tunnel(first, reader, writer)
                return
            await self._handle_control(first, reader, writer)
        except asyncio.CancelledError:
            raise
        except (ProtocolError, OSError, ConnectionError) as exc:
            LOGGER.debug("connection rejected: %s", exc)
            await _best_effort_error(writer, str(exc), fatal=isinstance(exc, AuthenticationError))
            await close_writer(writer)
        except Exception as exc:
            LOGGER.exception("connection handler crashed")
            await _best_effort_error(writer, str(exc))
            await close_writer(writer)

    async def _handle_control(
        self,
        first: Dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        client = ClientSession(id=uuid.uuid4().hex, writer=writer)
        async with self._lock:
            self._clients[client.id] = client
        try:
            current = first
            if current.get("type") == "hello":
                await client.send({"type": "hello", "version": 2, "server": "py-frp"})
                current = await read_message(reader) or {}
            if current.get("type") != "register":
                raise ProtocolError("control connection must register services")

            try:
                registered = await self._register_client(
                    client,
                    current.get("services"),
                    force=current.get("force") is True,
                )
            except ForceRequiredError as exc:
                await client.send(
                    {
                        "type": "force_required",
                        "status": "full",
                        "error": str(exc),
                    }
                )
                retry = await read_message(reader)
                if retry is None:
                    return
                current = retry
                if current.get("type") == "close":
                    return
                if current.get("type") != "register" or current.get("force") is not True:
                    raise ProtocolError("force retry must register services with force enabled")
                registered = await self._register_client(
                    client,
                    current.get("services"),
                    force=True,
                )
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
                            "priority": item.priority,
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
        except asyncio.CancelledError:
            raise
        except (ProtocolError, OSError, ConnectionError) as exc:
            LOGGER.warning("client %s disconnected: %s", client.id, exc)
            await _best_effort_error(
                writer,
                str(exc),
                fatal=isinstance(exc, (AuthenticationError, ResourceExhaustedError)),
                max_priority=(
                    exc.max_priority if isinstance(exc, PriorityPreemptionError) else None
                ),
            )
        except Exception as exc:
            LOGGER.exception("client %s handler crashed", client.id)
            await _best_effort_error(writer, str(exc))
        finally:
            await self._remove_client(client)
            await close_writer(writer)

    async def _register_client(
        self,
        client: ClientSession,
        raw_services: Any,
        *,
        force: bool = False,
    ) -> List[RegisteredService]:
        if not isinstance(raw_services, list) or not raw_services:
            raise ProtocolError("register message must include services")

        registered: List[RegisteredService] = []
        try:
            for raw in raw_services:
                if self._is_pool_registration(raw):
                    registered_service = await self._register_pool_service(
                        client,
                        raw,
                        force=force,
                    )
                else:
                    registered_service = await self._register_regular_service(client, raw)
                registered.append(registered_service)
                LOGGER.info(
                    "service %s listening on %s",
                    registered_service.name,
                    _server_addresses(registered_service.listener),
                )
        except Exception:
            for item in registered:
                await self._drop_registered_service(item)
            raise
        return registered

    async def _register_regular_service(
        self,
        client: ClientSession,
        raw: Any,
    ) -> RegisteredService:
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
        return registered_service

    async def _register_pool_service(
        self,
        client: ClientSession,
        raw: Any,
        *,
        force: bool = False,
    ) -> RegisteredService:
        if not isinstance(raw, dict):
            raise ProtocolError("service registration must be an object")
        token = _optional_string(raw.get("token"))
        if token not in self._pool_tokens:
            raise AuthenticationError("authentication failed")
        priority = _priority(raw.get("priority", 0))

        service_name = _pool_service_name(client)
        victim: Optional[ClientSession] = None
        victim_services: List[RegisteredService] = []
        victim_pending: List[PendingTunnel] = []
        registered_service: Optional[RegisteredService] = None
        last_error: Optional[OSError] = None

        async with self._lock:
            registered_service, last_error = await self._bind_pool_service_locked(
                client,
                service_name,
                token,
                priority,
            )
            if registered_service is not None:
                return registered_service

            pool_services = [
                service
                for service in self._services.values()
                if service.pool_token is not None and service.client is not client
            ]
            if not pool_services:
                raise _resource_exhausted(last_error)
            if not force:
                raise ForceRequiredError(
                    "resource insufficient: the port pool is full; force connection can "
                    "disconnect an eligible equal-or-lower-priority pool client"
                )

            max_priority = max(service.priority for service in pool_services)
            eligible_services = [
                service for service in pool_services if service.priority >= priority
            ]
            if not eligible_services:
                raise PriorityPreemptionError(priority, max_priority)
            victim_priority = max(service.priority for service in eligible_services)
            victim_service = min(
                (
                    service
                    for service in eligible_services
                    if service.priority == victim_priority
                ),
                key=lambda service: service.client.created_at,
            )
            victim = victim_service.client
            victim_services = [
                service for service in self._services.values() if service.client is victim
            ]
            for service in victim_services:
                self._services.pop(service.name, None)
                service.listener.close()
            victim_pending = [
                item for item in self._pending.values() if item.client is victim
            ]
            for item in victim_pending:
                self._pending.pop(item.id, None)
            await asyncio.gather(
                *(service.listener.wait_closed() for service in victim_services),
                return_exceptions=True,
            )

            registered_service, last_error = await self._bind_pool_service_locked(
                client,
                service_name,
                token,
                priority,
            )

        for item in victim_pending:
            if not item.future.done():
                item.future.set_exception(ConnectionError("client was preempted"))
        assert victim is not None
        try:
            await victim.send(
                {
                    "type": "error",
                    "error": "connection preempted by a forced pool connection",
                    "code": "preempted",
                    "fatal": False,
                    "priority": victim_priority,
                    "preempted_by_priority": priority,
                }
            )
        except (ConnectionError, OSError, ProtocolError):
            pass
        await close_writer(victim.writer)
        LOGGER.warning(
            "client %s at priority %d was preempted by forced client %s at priority %d",
            victim.id,
            victim_priority,
            client.id,
            priority,
        )

        if registered_service is None:
            raise _resource_exhausted(last_error)
        return registered_service

    async def _bind_pool_service_locked(
        self,
        client: ClientSession,
        service_name: str,
        token: str,
        priority: int,
    ) -> Tuple[Optional[RegisteredService], Optional[OSError]]:
        used_ports = {
            service.bind_port
            for service in self._services.values()
            if service.pool_token is not None
        }
        last_error: Optional[OSError] = None
        for port in sorted(self.config.port_pool):
            if port in used_ports:
                continue
            try:
                listener = await asyncio.start_server(
                    lambda reader, writer, name=service_name: self._handle_public(
                        name,
                        reader,
                        writer,
                    ),
                    self.config.pool_bind_host,
                    port,
                )
            except OSError as exc:
                last_error = exc
                LOGGER.warning("pool port %d is unavailable: %s", port, exc)
                continue

            registered_service = RegisteredService(
                name=service_name,
                bind_host=self.config.pool_bind_host,
                bind_port=port,
                token=token,
                client=client,
                listener=listener,
                configured=True,
                pool_token=token,
                priority=priority,
            )
            self._services[service_name] = registered_service
            return registered_service, last_error
        return None, last_error

    def _is_pool_registration(self, raw: Any) -> bool:
        if not self.config.port_pool:
            return False
        if not isinstance(raw, dict):
            return False
        if raw.get("remote_port") is not None or raw.get("remotePort") is not None:
            return False
        return True

    async def _drop_registered_service(self, service: RegisteredService) -> None:
        async with self._lock:
            self._services.pop(service.name, None)
        service.listener.close()
        await service.listener.wait_closed()

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
            self._clients.pop(client.id, None)
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
        loop = get_running_loop()
        future = loop.create_future()
        pending = PendingTunnel(
            id=tunnel_id,
            service_name=service_name,
            token=service.token,
            client=service.client,
            future=future,
        )
        async with self._lock:
            self._pending[tunnel_id] = pending

        tunnel_writer: Optional[asyncio.StreamWriter] = None
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
            await pipe_tunnel_streams(reader, writer, tunnel_reader, tunnel_writer)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, ConnectionError, OSError, ProtocolError) as exc:
            LOGGER.debug("public connection for %s closed: %s", service_name, exc)
        except Exception:
            LOGGER.exception("public connection for %s crashed", service_name)
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
        message: Dict[str, Any],
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

        if pending.future.done():
            raise ProtocolError("pending tunnel is no longer open")
        pending.future.set_result((reader, writer))
        LOGGER.debug("paired tunnel %s for service %s", tunnel_id, service_name)


async def _best_effort_error(
    writer: asyncio.StreamWriter,
    error: str,
    *,
    fatal: bool = False,
    max_priority: Optional[int] = None,
) -> None:
    is_closing = getattr(writer, "is_closing", None)
    if is_closing is not None and is_closing():
        return
    try:
        message: Dict[str, Any] = {"type": "error", "error": error, "fatal": fatal}
        if max_priority is not None:
            message["max_priority"] = max_priority
        await write_message(writer, message)
    except Exception:
        pass


def _require_token(expected: Optional[str], provided: Optional[str]) -> None:
    if expected is not None and provided != expected:
        raise AuthenticationError("authentication failed")


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text or None


def _port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("remote_port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise ProtocolError("remote_port is outside the valid range")
    return port


def _priority(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError("priority must be an integer")
    return value


def _server_addresses(server: asyncio.AbstractServer) -> str:
    sockets = server.sockets or ()
    return ", ".join(f"{sock.getsockname()[0]}:{sock.getsockname()[1]}" for sock in sockets)


def _pool_service_name(client: ClientSession) -> str:
    return f"pool-{client.id}"


def _resource_exhausted(cause: Optional[OSError]) -> ResourceExhaustedError:
    error = ResourceExhaustedError(
        "resource insufficient: no available port in the configured pool"
    )
    if cause is not None:
        error.__cause__ = cause
    return error
