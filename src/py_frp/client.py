from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import ClientConfig, ProxyConfig
from .protocol import ProtocolError, close_writer, pipe_streams, read_message, write_message


LOGGER = logging.getLogger(__name__)


class Client:
    def __init__(self, config: ClientConfig):
        self.config = config
        self._proxies = {proxy.name: proxy for proxy in config.proxies}
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                await self._cancel_tunnel_tasks()
                raise
            except Exception as exc:
                LOGGER.warning(
                    "client connection failed: %s; reconnecting in %.1fs",
                    exc,
                    self.config.reconnect_delay,
                )
                await self._cancel_tunnel_tasks()
                await asyncio.sleep(self.config.reconnect_delay)

    async def _run_once(self) -> None:
        reader, writer = await asyncio.open_connection(
            self.config.server_host,
            self.config.server_port,
        )
        try:
            await write_message(writer, {"type": "hello", "version": 1, "client": "py-frp"})
            hello = await read_message(reader)
            if hello is None or hello.get("type") == "error":
                raise ProtocolError(_message_error(hello, "server closed during hello"))

            await write_message(writer, {"type": "register", "services": self._service_payloads()})
            registered = await read_message(reader)
            if registered is None or registered.get("type") == "error":
                raise ProtocolError(_message_error(registered, "server closed during register"))
            if registered.get("type") != "registered" or registered.get("status") != "ok":
                raise ProtocolError(f"unexpected register response: {registered!r}")
            LOGGER.info("registered %d service(s)", len(self.config.proxies))

            while True:
                message = await read_message(reader)
                if message is None:
                    raise ConnectionError("control connection closed")
                if message.get("type") == "open":
                    task = asyncio.create_task(self._open_tunnel(message))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                elif message.get("type") == "pong":
                    LOGGER.debug("received pong")
                elif message.get("type") == "error":
                    raise ProtocolError(_message_error(message, "server error"))
                else:
                    LOGGER.debug("ignored control message: %s", message)
        finally:
            await close_writer(writer)

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
                local_reader, local_writer = await asyncio.open_connection(
                    proxy.local_host,
                    proxy.local_port,
                )
            except (ConnectionError, OSError) as exc:
                local_error = str(exc)
                LOGGER.warning("cannot connect local service %s: %s", service_name, exc)

            tunnel_reader, tunnel_writer = await asyncio.open_connection(
                self.config.server_host,
                self.config.server_port,
            )
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
        except (ConnectionError, OSError, ProtocolError) as exc:
            LOGGER.debug("tunnel %s for %s closed: %s", tunnel_id, service_name, exc)
        finally:
            await close_writer(local_writer)
            await close_writer(tunnel_writer)

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
