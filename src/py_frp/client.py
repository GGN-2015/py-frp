from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from .config import ClientConfig
from .protocol import ProtocolError, close_writer, pipe_tunnel_streams, read_message, write_message
from .security import (
    SecurityError,
    RESTART_SERVER_FINGERPRINT_ENV,
    create_client_tls_context,
    fingerprints_equal,
    normalize_fingerprint,
    peer_fingerprint,
)


LOGGER = logging.getLogger(__name__)


class FatalClientError(RuntimeError):
    """Raised when the server says reconnecting would repeat the same failure."""


class Client:
    def __init__(
        self,
        config: ClientConfig,
        on_registered: Callable[[dict[str, Any]], None] | None = None,
        confirm_fingerprint: Callable[[str], bool] | None = None,
        confirm_force: Callable[[str], bool] | None = None,
        force_connect: bool = False,
        priority: int = 0,
    ):
        self.config = config
        self.on_registered = on_registered
        self.confirm_fingerprint = confirm_fingerprint
        self.confirm_force = confirm_force
        self.force_connect = force_connect
        self.priority = priority
        self._base_proxies = {proxy.name: proxy for proxy in config.proxies}
        self._proxies = dict(self._base_proxies)
        self._tasks: set[asyncio.Task[None]] = set()
        self._tls_context = create_client_tls_context()
        configured_fingerprint = (
            config.server_fingerprint
            or os.environ.get(RESTART_SERVER_FINGERPRINT_ENV)
            or None
        )
        self._trusted_fingerprint = (
            normalize_fingerprint(configured_fingerprint)
            if configured_fingerprint is not None
            else None
        )
        self._fingerprint_ready = asyncio.Event()
        if self._trusted_fingerprint is not None:
            self._fingerprint_ready.set()

    def preserve_fingerprint_for_restart(self) -> None:
        if self._trusted_fingerprint is None:
            raise SecurityError(
                "cannot restart safely before a server TLS fingerprint has been trusted"
            )
        os.environ[RESTART_SERVER_FINGERPRINT_ENV] = self._trusted_fingerprint

    async def wait_until_fingerprint_trusted(self) -> None:
        await self._fingerprint_ready.wait()

    async def run(self) -> None:
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                await self._cancel_tunnel_tasks()
                raise
            except SecurityError as exc:
                LOGGER.error("TLS security error: %s", exc)
                await self._cancel_tunnel_tasks()
                return
            except FatalClientError as exc:
                LOGGER.error("%s", exc)
                await self._cancel_tunnel_tasks()
                return
            except Exception as exc:
                LOGGER.warning(
                    "client connection failed: %s; reconnecting in %.1fs",
                    exc,
                    self.config.reconnect_delay,
                )
                await self._cancel_tunnel_tasks()
                await asyncio.sleep(self.config.reconnect_delay)

    async def _run_once(self) -> None:
        reader, writer = await self._open_server_connection()
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            await write_message(writer, {"type": "hello", "version": 2, "client": "py-frp"})
            hello = await read_message(reader)
            if hello is None:
                raise ProtocolError("server closed during hello")
            _raise_error_response(hello)

            register_message: dict[str, Any] = {
                "type": "register",
                "services": self._service_payloads(),
            }
            if self.force_connect:
                register_message["force"] = True
            await write_message(writer, register_message)
            registered = await read_message(reader)
            if registered is None:
                raise ProtocolError("server closed during register")
            if registered.get("type") == "force_required":
                question = _message_error(
                    registered,
                    "the server port pool is full",
                )
                if not self.force_connect and not await self._confirm_force_connection(question):
                    raise FatalClientError("force connection declined; client stopped")
                register_message["force"] = True
                await write_message(writer, register_message)
                registered = await read_message(reader)
                if registered is None:
                    raise ProtocolError("server closed during forced register")
            _raise_error_response(registered)
            if registered.get("type") != "registered" or registered.get("status") != "ok":
                raise ProtocolError(f"unexpected register response: {registered!r}")
            self._apply_registered_aliases(registered)
            LOGGER.info("registered %d service(s)", len(self.config.proxies))
            if self.on_registered is not None:
                self.on_registered(registered)
            heartbeat_task = asyncio.create_task(self._heartbeat(writer))

            while True:
                message = await self._read_control_message(reader, heartbeat_task)
                if message is None:
                    raise ConnectionError("control connection closed")
                if message.get("type") == "open":
                    task = asyncio.create_task(self._open_tunnel(message))
                    self._track_tunnel_task(task)
                elif message.get("type") == "pong":
                    LOGGER.debug("received pong")
                elif message.get("type") == "error":
                    _raise_error_response(message)
                else:
                    LOGGER.debug("ignored control message: %s", message)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            await close_writer(writer)

    async def _open_server_connection(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.config.server_host,
                self.config.server_port,
                ssl=self._tls_context,
            ),
            timeout=self.config.connect_timeout,
        )
        try:
            await self._verify_server_fingerprint(writer)
        except Exception:
            await close_writer(writer)
            raise
        return reader, writer

    async def _verify_server_fingerprint(self, writer: asyncio.StreamWriter) -> None:
        fingerprint = peer_fingerprint(writer)
        if self._trusted_fingerprint is not None:
            if not fingerprints_equal(fingerprint, self._trusted_fingerprint):
                raise SecurityError(
                    "server TLS fingerprint changed or does not match the configured value"
                )
            return

        if self.confirm_fingerprint is None:
            print(f"server_tls_fingerprint {fingerprint}", flush=True)
            try:
                answer = await asyncio.to_thread(
                    input,
                    "Trust this server fingerprint? [y/N]: ",
                )
            except (EOFError, KeyboardInterrupt) as exc:
                raise SecurityError("server fingerprint was not confirmed") from exc
            confirmed = answer.strip().lower() in {"y", "yes"}
        else:
            confirmed = bool(self.confirm_fingerprint(fingerprint))

        if not confirmed:
            raise SecurityError("server fingerprint was rejected")
        self._trusted_fingerprint = fingerprint
        self._fingerprint_ready.set()

    async def _read_control_message(
        self,
        reader: asyncio.StreamReader,
        heartbeat_task: asyncio.Task[None],
    ) -> dict[str, Any] | None:
        read_task = asyncio.create_task(read_message(reader))
        try:
            done, _ = await asyncio.wait(
                {read_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                exc = heartbeat_task.exception()
                if exc is not None:
                    raise ConnectionError("control heartbeat failed") from exc
                raise ConnectionError("control heartbeat stopped")
            return read_task.result()
        finally:
            if not read_task.done():
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)

    async def _heartbeat(self, writer: asyncio.StreamWriter) -> None:
        while True:
            await asyncio.sleep(self.config.heartbeat_interval)
            await write_message(writer, {"type": "ping", "time": time.time()})

    async def _confirm_force_connection(self, reason: str) -> bool:
        if self.confirm_force is not None:
            return bool(self.confirm_force(reason))
        print(reason, flush=True)
        try:
            answer = await asyncio.to_thread(
                input,
                "Force connection and disconnect an eligible equal-or-lower-priority pool client? [y/N]: ",
            )
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}

    def _service_payloads(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for proxy in self.config.proxies:
            payload: dict[str, Any] = {
                "name": proxy.name,
                "token": proxy.token or self.config.token,
            }
            if proxy.remote_host is not None:
                payload["remote_host"] = proxy.remote_host
            if proxy.remote_port is not None:
                payload["remote_port"] = proxy.remote_port
            if self.config.source_flavor == "token-pool":
                payload["priority"] = self.priority
            payloads.append(payload)
        return payloads

    def _apply_registered_aliases(self, message: dict[str, Any]) -> None:
        self._proxies = dict(self._base_proxies)
        services = message.get("services")
        if not isinstance(services, list) or len(services) != len(self.config.proxies):
            return
        for raw, proxy in zip(services, self.config.proxies):
            if not isinstance(raw, dict):
                continue
            name = raw.get("name")
            if isinstance(name, str) and name:
                self._proxies[name] = proxy

    async def _open_tunnel(self, message: dict[str, Any]) -> None:
        tunnel_id = str(message.get("id") or "")
        service_name = str(message.get("service") or "")
        proxy = self._proxies.get(service_name)
        if not tunnel_id or proxy is None:
            LOGGER.warning("received open for unknown service %r", service_name)
            return

        local_reader: asyncio.StreamReader | None = None
        local_writer: asyncio.StreamWriter | None = None
        tunnel_writer: asyncio.StreamWriter | None = None
        local_error: str | None = None
        try:
            try:
                local_reader, local_writer = await asyncio.wait_for(
                    asyncio.open_connection(proxy.local_host, proxy.local_port),
                    timeout=self.config.connect_timeout,
                )
            except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                local_error = str(exc)
                LOGGER.warning("cannot connect local service %s: %s", service_name, exc)

            tunnel_reader, tunnel_writer = await self._open_server_connection()
            await write_message(
                tunnel_writer,
                {
                    "type": "tunnel",
                    "id": tunnel_id,
                    "service": service_name,
                    "token": proxy.token or self.config.token,
                    "error": local_error,
                    "time": time.time(),
                },
            )
            if local_reader is None or local_writer is None:
                return
            await pipe_tunnel_streams(
                local_reader,
                local_writer,
                tunnel_reader,
                tunnel_writer,
            )
        except (asyncio.TimeoutError, ConnectionError, OSError, ProtocolError) as exc:
            LOGGER.debug("tunnel %s for %s closed: %s", tunnel_id, service_name, exc)
        finally:
            await close_writer(local_writer)
            await close_writer(tunnel_writer)

    def _track_tunnel_task(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._on_tunnel_task_done)

    def _on_tunnel_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.exception("tunnel task crashed")

    async def _cancel_tunnel_tasks(self) -> None:
        tasks = list(self._tasks)
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _message_error(message: dict[str, Any] | None, default: str) -> str:
    if isinstance(message, dict) and message.get("error"):
        return str(message["error"])
    return default


def _raise_error_response(message: dict[str, Any]) -> None:
    if message.get("type") != "error":
        return
    error = _message_error(message, "server error")
    if message.get("fatal") is True:
        raise FatalClientError(error)
    raise ProtocolError(error)
