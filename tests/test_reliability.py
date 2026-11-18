from __future__ import annotations

import asyncio
import json
import socket
import unittest
from unittest import mock

from py_frp.cli import _run_client_until_update
from py_frp import cli
from py_frp.client import Client
from py_frp.config import ClientConfig, ProxyConfig, ServerConfig
from py_frp.protocol import (
    MAX_MESSAGE_BYTES,
    MESSAGE_STREAM_LIMIT,
    ProtocolError,
    close_writer,
    pipe_streams,
    pipe_tunnel_streams,
    read_message,
    write_message,
)
from py_frp.security import create_client_tls_context
from py_frp.server import ClientSession, Server
from py_frp.update import run_until_version_change


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.servers = []
        self.writers = []
        self.tasks = []
        self.local_servers = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(*(close_writer(writer) for writer in self.writers))
        for server in self.servers:
            await asyncio.wait_for(server.close(), timeout=3)
        for server in self.local_servers:
            server.close()
            await asyncio.wait_for(server.wait_closed(), timeout=3)

    async def server(self, **options) -> Server:
        config = dict(bind_host="127.0.0.1", bind_port=0, token="secret")
        config.update(options)
        server = Server(ServerConfig(**config))
        self.servers.append(server)
        await server.start()
        return server

    async def connect(self, server):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.control_addresses()[0][1],
            ssl=create_client_tls_context(), limit=MESSAGE_STREAM_LIMIT,
        )
        self.writers.append(writer)
        return reader, writer

    async def register(self, server, *, pool=False, force=False, services=None):
        reader, writer = await self.connect(server)
        if services is None:
            service = {"name": "test", "token": "secret"}
            if not pool:
                service.update(remote_host="127.0.0.1", remote_port=0)
            services = [service]
        await write_message(writer, {"type": "register", "services": services, "force": force})
        response = await read_message(reader, timeout=1)
        return reader, writer, response

    def client(self, server, **options):
        config = dict(
            server_host="127.0.0.1", server_port=server.control_addresses()[0][1],
            token="secret", server_fingerprint=server.tls_fingerprint,
            proxies=(ProxyConfig("test", "127.0.0.1", 1, "127.0.0.1", 0),),
            connect_timeout=0.15, reconnect_delay=0.01, heartbeat_interval=0.02,
        )
        config.update(options)
        return Client(ClientConfig(**config), confirm_force=lambda _: False)

    def start_client(self, client):
        task = asyncio.create_task(client.run())
        self.tasks.append(task)
        return task

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), timeout=1)

    async def echo_server(self):
        async def echo(reader, writer):
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            finally:
                await close_writer(writer)
        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.local_servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def test_close_releases_registered_and_unauthenticated_connections(self):
        server = await self.server()
        idle_reader, _ = await self.connect(server)
        reader, _, response = await self.register(server)
        self.assertEqual(response["type"], "registered")
        public_reader, public_writer = await asyncio.open_connection(
            "127.0.0.1", response["services"][0]["bind_port"],
        )
        self.writers.append(public_writer)
        self.assertEqual((await read_message(reader, timeout=1))["type"], "open")
        await asyncio.wait_for(server.close(), timeout=1)
        for peer in (idle_reader, reader, public_reader):
            self.assertEqual(await asyncio.wait_for(peer.read(), timeout=1), b"")
        self.assertFalse(server._writers)
        self.assertFalse(server._pending)
        self.assertFalse(server._clients)
        self.assertFalse(server._services)
        self.assertFalse(server._handlers)

    async def test_runtime_cancellation_allows_restart_notice_before_close(self):
        server = await self.server()
        reader, _, _ = await self.register(server)
        runtime = asyncio.create_task(server.serve_forever())
        self.tasks.append(runtime)
        await asyncio.sleep(0)
        runtime.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(runtime, timeout=1)
        await server.notify_restarting()
        self.assertEqual((await read_message(reader, timeout=1))["type"], "server_restarting")
        await asyncio.wait_for(server.close(), timeout=1)
        self.assertEqual(await reader.read(), b"")

    async def test_update_closes_live_connections_before_requesting_restart(self):
        server = await self.server()
        reader, _, _ = await self.register(server)
        async def monitor(runtime, **kwargs):
            return await run_until_version_change(
                runtime, initial_version="0.7.0", interval=0.01,
                version_reader=lambda: "0.8.0",
            )
        with mock.patch.dict("os.environ"), \
             mock.patch.object(cli, "run_until_version_change", new=monitor), \
             mock.patch.object(cli, "request_restart") as restart:
            await asyncio.wait_for(cli._run_server_until_update(server), timeout=1)
        self.assertEqual((await read_message(reader, timeout=1))["type"], "server_restarting")
        self.assertEqual(await reader.read(), b"")
        restart.assert_called_once_with("0.8.0")
        self.assertFalse(server._writers)

    async def test_cancelled_preemption_rolls_back_new_listener_and_closes_victim(self):
        port = unused_port()
        server = await self.server(
            allow_dynamic=False, port_pool=(port,), pool_tokens=("secret",), pool_bind_host="127.0.0.1",
        )
        old_reader, _, response = await self.register(server, pool=True)
        victim = server._clients[response["client_id"]]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        await read_message(old_reader, timeout=1)
        notifying = asyncio.Event()
        async def stalled_notification(message):
            notifying.set()
            await asyncio.Event().wait()
        incoming = ClientSession("incoming", FakeWriter())
        with mock.patch.object(victim, "send", new=stalled_notification):
            task = asyncio.create_task(server._register_client(
                incoming, [{"name": "test", "token": "secret"}], force=True,
            ))
            self.tasks.append(task)
            await asyncio.wait_for(notifying.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
        self.assertFalse(server._services)
        self.assertFalse(server._pending)
        self.assertFalse(server._lock.locked())
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))

    async def test_close_releases_active_tunnel(self):
        server = await self.server()
        local_port = await self.echo_server()
        client = self.client(server, proxies=(ProxyConfig("test", "127.0.0.1", local_port, "127.0.0.1", 0),))
        self.start_client(client)
        await self.wait_until(lambda: server.service_address("test") is not None)
        reader, writer = await asyncio.open_connection(*server.service_address("test"))
        self.writers.append(writer)
        writer.write(b"alive")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(5), 1), b"alive")
        await asyncio.wait_for(server.close(), timeout=1)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertFalse(server._writers)
        self.assertFalse(server._pending)

    async def test_force_preemption_closes_waiting_public_connection(self):
        port = unused_port()
        server = await self.server(
            allow_dynamic=False, port_pool=(port,), pool_tokens=("secret",),
            pool_bind_host="127.0.0.1", open_timeout=0.05,
        )
        old_reader, _, response = await self.register(server, pool=True)
        self.assertEqual(response["type"], "registered")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        await read_message(old_reader, timeout=1)
        _, _, response = await self.register(server, pool=True, force=True)
        self.assertEqual(response["type"], "registered")
        self.assertEqual(response["services"][0]["bind_port"], port)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertFalse(server._lock.locked())
        self.assertFalse(server._pending)

    async def test_force_preemption_closes_active_tunnel(self):
        port = unused_port()
        local_port = await self.echo_server()
        server = await self.server(
            allow_dynamic=False, port_pool=(port,), pool_tokens=("secret",), pool_bind_host="127.0.0.1",
        )
        client = self.client(server, source_flavor="token-pool",
                             proxies=(ProxyConfig("test", "127.0.0.1", local_port),))
        self.start_client(client)
        await self.wait_until(lambda: bool(server._services))
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        writer.write(b"alive")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(5), 1), b"alive")
        _, _, response = await self.register(server, pool=True, force=True)
        self.assertEqual(response["type"], "registered")
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertFalse(server._lock.locked())

    async def test_duplicate_pool_services_are_rejected_without_binding(self):
        ports = (unused_port(), unused_port())
        server = await self.server(
            allow_dynamic=False, port_pool=ports, pool_tokens=("secret",), pool_bind_host="127.0.0.1",
        )
        _, _, response = await self.register(server, services=[
            {"name": "first", "token": "secret"}, {"name": "second", "token": "secret"},
        ])
        self.assertEqual(response["type"], "error")
        self.assertIn("only one", response["error"])
        self.assertFalse(server._services)
        for port in ports:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))

    async def test_client_reconnects_when_pongs_stop(self):
        server = await self.server()
        client = self.client(server)
        registrations = []
        client.on_registered = registrations.append
        original = ClientSession.send
        async def without_pong(session, message):
            if message.get("type") != "pong":
                await original(session, message)
        with mock.patch.object(ClientSession, "send", new=without_pong):
            self.start_client(client)
            await self.wait_until(lambda: len(registrations) >= 2)

    async def test_valid_heartbeats_keep_session_alive(self):
        server = await self.server(idle_timeout=0.1)
        client = self.client(server)
        registrations = []
        client.on_registered = registrations.append
        task = self.start_client(client)
        await self.wait_until(lambda: bool(registrations))
        await asyncio.sleep(0.2)
        self.assertEqual(len(registrations), 1)
        self.assertFalse(task.done())

    async def test_client_retries_after_control_handshake_timeout(self):
        server = await self.server()
        client = self.client(server, connect_timeout=0.08)
        original = ClientSession.send
        async def without_hello(session, message):
            if message.get("type") != "hello":
                await original(session, message)
        with mock.patch.object(ClientSession, "send", new=without_hello), \
             mock.patch.object(client, "_open_server_connection", wraps=client._open_server_connection) as connect:
            self.start_client(client)
            await self.wait_until(lambda: connect.call_count >= 2)

    async def test_server_times_out_first_message_and_registration(self):
        server = await self.server(handshake_timeout=0.05)
        for hello in (False, True):
            with self.subTest(hello=hello):
                reader, writer = await self.connect(server)
                if hello:
                    await write_message(writer, {"type": "hello"})
                    self.assertEqual((await read_message(reader, timeout=1))["type"], "hello")
                await asyncio.wait_for(reader.read(), timeout=1)
                await self.wait_until(lambda: not server._writers)
                self.assertFalse(server._clients)

    async def test_server_releases_idle_registered_service(self):
        server = await self.server(idle_timeout=0.05)
        reader, _, response = await self.register(server)
        self.assertEqual(response["type"], "registered")
        await asyncio.wait_for(reader.read(), timeout=1)
        await self.wait_until(lambda: not server._services)

    async def test_fatal_authentication_and_tls_errors_return_failure_status(self):
        server = await self.server()
        for auto_restart in (False, True):
            with self.subTest(auto_restart=auto_restart):
                client = self.client(server, token="wrong-token")
                code = await asyncio.wait_for(_run_client_until_update(client, auto_restart=auto_restart), 1)
                self.assertNotEqual(code, 0)
        client = self.client(server, server_fingerprint="SHA256:" + ":".join(["00"] * 32))
        self.assertNotEqual(await asyncio.wait_for(client.run(), 1), 0)

    async def test_large_messages_work_in_both_directions(self):
        server = await self.server()
        reader, writer = await self.connect(server)
        # Exercise the exact 1 MiB payload boundary, excluding the newline.
        hello = {"type": "hello", "padding": ""}
        overhead = len(json.dumps(hello, separators=(",", ":")).encode())
        hello["padding"] = "x" * (MAX_MESSAGE_BYTES - overhead)
        await write_message(writer, hello)
        self.assertEqual((await read_message(reader, timeout=1))["type"], "hello")
        await write_message(writer, {"type": "register", "services": [
            {"name": "test", "token": "secret", "remote_host": "127.0.0.1", "remote_port": 0},
        ]})
        self.assertEqual((await read_message(reader, timeout=1))["type"], "registered")
        payload = "x" * 70000
        await write_message(writer, {"type": "ping", "time": payload})
        self.assertEqual((await read_message(reader, timeout=1))["time"], payload)

    async def test_oversized_message_raises_protocol_error(self):
        reader = asyncio.StreamReader(limit=MESSAGE_STREAM_LIMIT)
        reader.feed_data(b"x" * (MAX_MESSAGE_BYTES + 2) + b"\n")
        with self.assertRaises(ProtocolError):
            await read_message(reader)
        with self.assertRaises(ProtocolError):
            await write_message(mock.Mock(), {"value": "x" * MAX_MESSAGE_BYTES})

    async def test_close_writer_aborts_peer_that_never_finishes_shutdown(self):
        writer = mock.Mock()
        writer.is_closing.return_value = False
        writer.wait_closed = mock.AsyncMock(side_effect=asyncio.Event().wait)
        await asyncio.wait_for(close_writer(writer, timeout=0.01), 1)
        writer.transport.abort.assert_called_once()

    async def test_relay_cancellation_closes_both_writers(self):
        for relay in (pipe_streams, pipe_tunnel_streams):
            with self.subTest(relay=relay.__name__):
                left, right = FakeWriter(), FakeWriter()
                task = asyncio.create_task(relay(asyncio.StreamReader(), left, asyncio.StreamReader(), right))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(left.closed)
                self.assertTrue(right.closed)


class FakeWriter:
    def __init__(self):
        self.closed = False

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    def write_eof(self):
        pass

    async def wait_closed(self):
        pass


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
