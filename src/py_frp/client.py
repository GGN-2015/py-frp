from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from .config import ClientConfig, ProxyConfig
from .protocol import ProtocolError, close_writer, pipe_streams, read_message, write_message


LOGGER = logging.getLogger(__name__)


class FatalClientError(RuntimeError):
    """Raised when the server says reconnecting would repeat the same failure."""


class Client:
    def __init__(
        self,
        config: ClientConfig,
        on_registered: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self.on_registered = on_registered
        self._proxies = {proxy.name: proxy for proxy in config.proxies}
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                await self._cancel_tunnel_tasks()
                raise
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
            await write_message(writer, {"type": "hello", "version": 1, "client": "py-frp"})
            hello = await read_message(reader)
            if hello is None:
                raise ProtocolError("server closed during hello")
            _raise_error_response(hello)

            await write_message(writer, {"type": "register", "services": self._service_payloads()})
            registered = await read_message(reader)
            if registered is None:
                raise ProtocolError("server closed during register")
            _raise_error_response(registered)
            if registered.get("type") != "registered" or registered.get("status") != "ok":
                raise ProtocolError(f"unexpected register response: {registered!r}")
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
        return await asyncio.wait_for(
            asyncio.open_connection(self.config.server_host, self.config.server_port),
            timeout=self.config.connect_timeout,
        )

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
            payloads.append(payload)
        return payloads

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
            await pipe_streams(local_reader, local_writer, tunnel_reader, tunnel_writer)
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
