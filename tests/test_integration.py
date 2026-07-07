from __future__ import annotations

import asyncio
import contextlib
import unittest

from py_frp.client import Client
from py_frp.config import ClientConfig, ProxyConfig, ServerConfig, ServerServiceConfig
from py_frp.protocol import close_writer, read_message, write_message
from py_frp.server import Server


class TunnelIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await asyncio.sleep(0)

    async def test_tcp_reverse_tunnel_round_trip(self) -> None:
        async def echo(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            data = await reader.read(1024)
            writer.write(data.upper())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        echo_port = int(echo_server.sockets[0].getsockname()[1])

        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                token="secret",
                allow_dynamic=False,
                services=(
                    ServerServiceConfig(
                        name="echo",
                        bind_host="127.0.0.1",
                        bind_port=0,
                        token="secret",
                    ),
                ),
            )
        )
        await tunnel_server.start()
        control_port = tunnel_server.control_addresses()[0][1]

        client = Client(
            ClientConfig(
                server_host="127.0.0.1",
                server_port=control_port,
                token="secret",
                reconnect_delay=0.1,
                proxies=(
                    ProxyConfig(
                        name="echo",
                        local_host="127.0.0.1",
                        local_port=echo_port,
                        token="secret",
                    ),
                ),
            )
        )
        client_task = asyncio.create_task(client.run())
        try:
            public_addr = await _wait_for_service(tunnel_server, "echo")
            reader, writer = await asyncio.open_connection(*public_addr)
            writer.write(b"hello")
            await writer.drain()
            self.assertEqual(await reader.read(5), b"HELLO")
            writer.close()
            await writer.wait_closed()
        finally:
            client_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client_task
            await tunnel_server.close()
            echo_server.close()
            await echo_server.wait_closed()

    async def test_dynamic_tcp_reverse_tunnel_round_trip(self) -> None:
        async def echo(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            data = await reader.read(1024)
            writer.write(b"dynamic:" + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        echo_port = int(echo_server.sockets[0].getsockname()[1])
        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                token="secret",
                allow_dynamic=True,
            )
        )
        await tunnel_server.start()
        control_port = tunnel_server.control_addresses()[0][1]
        client = Client(
            ClientConfig(
                server_host="127.0.0.1",
                server_port=control_port,
                token="secret",
                reconnect_delay=0.1,
                proxies=(
                    ProxyConfig(
                        name="echo-dynamic",
                        local_host="127.0.0.1",
                        local_port=echo_port,
                        remote_host="127.0.0.1",
                        remote_port=0,
                        token="secret",
                    ),
                ),
            )
        )
        client_task = asyncio.create_task(client.run())
        try:
            public_addr = await _wait_for_service(tunnel_server, "echo-dynamic")
            reader, writer = await asyncio.open_connection(*public_addr)
            writer.write(b"ok")
            await writer.drain()
            self.assertEqual(await reader.read(10), b"dynamic:ok")
            writer.close()
            await writer.wait_closed()
        finally:
            client_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client_task
            await tunnel_server.close()
            echo_server.close()
            await echo_server.wait_closed()

    async def test_half_closed_public_connection_can_receive_response(self) -> None:
        async def eof_response(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            data = await reader.read()
            writer.write(b"after-eof:" + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        local_server = await asyncio.start_server(eof_response, "127.0.0.1", 0)
        local_port = int(local_server.sockets[0].getsockname()[1])
        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                token="secret",
                allow_dynamic=True,
            )
        )
        await tunnel_server.start()
        control_port = tunnel_server.control_addresses()[0][1]
        client = Client(
            ClientConfig(
                server_host="127.0.0.1",
                server_port=control_port,
                token="secret",
                reconnect_delay=0.1,
                proxies=(
                    ProxyConfig(
                        name="eof",
                        local_host="127.0.0.1",
                        local_port=local_port,
                        remote_host="127.0.0.1",
                        remote_port=0,
                        token="secret",
                    ),
                ),
            )
        )
        client_task = asyncio.create_task(client.run())
        try:
            public_addr = await _wait_for_service(tunnel_server, "eof")
            reader, writer = await asyncio.open_connection(*public_addr)
            if not writer.can_write_eof():
                self.skipTest("transport does not support write_eof")
            writer.write(b"hello")
            await writer.drain()
            writer.write_eof()
            self.assertEqual(
                await asyncio.wait_for(reader.readexactly(15), timeout=2),
                b"after-eof:hello",
            )
            writer.close()
            await writer.wait_closed()
        finally:
            client_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client_task
            await tunnel_server.close()
            local_server.close()
            await local_server.wait_closed()

    async def test_bad_dynamic_port_returns_error_and_server_keeps_running(self) -> None:
        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                token="secret",
                allow_dynamic=True,
            )
        )
        await tunnel_server.start()
        control_host, control_port = tunnel_server.control_addresses()[0]
        bad_writer: asyncio.StreamWriter | None = None
        good_writer: asyncio.StreamWriter | None = None
        try:
            bad_reader, bad_writer = await asyncio.open_connection(control_host, control_port)
            await write_message(bad_writer, {"type": "hello", "version": 1})
            self.assertEqual((await read_message(bad_reader) or {}).get("type"), "hello")
            await write_message(
                bad_writer,
                {
                    "type": "register",
                    "services": [
                        {
                            "name": "bad",
                            "token": "secret",
                            "remote_host": "127.0.0.1",
                            "remote_port": "not-a-port",
                        }
                    ],
                },
            )
            error_message = await asyncio.wait_for(read_message(bad_reader), timeout=1)
            self.assertEqual((error_message or {}).get("type"), "error")

            good_reader, good_writer = await asyncio.open_connection(control_host, control_port)
            await write_message(good_writer, {"type": "hello", "version": 1})
            self.assertEqual((await read_message(good_reader) or {}).get("type"), "hello")
            await write_message(
                good_writer,
                {
                    "type": "register",
                    "services": [
                        {
                            "name": "good",
                            "token": "secret",
                            "remote_host": "127.0.0.1",
                            "remote_port": 0,
                        }
                    ],
                },
            )
            registered = await asyncio.wait_for(read_message(good_reader), timeout=1)
            self.assertEqual((registered or {}).get("type"), "registered")
            self.assertIsNotNone(await _wait_for_service(tunnel_server, "good"))
        finally:
            await close_writer(bad_writer)
            await close_writer(good_writer)
            await tunnel_server.close()

    async def test_wrong_tunnel_token_cannot_claim_pending_connection(self) -> None:
        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                token="server-secret",
                allow_dynamic=False,
                services=(
                    ServerServiceConfig(
                        name="secure",
                        bind_host="127.0.0.1",
                        bind_port=0,
                        token="service-secret",
                    ),
                ),
            )
        )
        await tunnel_server.start()
        control_host, control_port = tunnel_server.control_addresses()[0]
        control_reader, control_writer = await asyncio.open_connection(control_host, control_port)
        public_writer: asyncio.StreamWriter | None = None
        bad_tunnel_writer: asyncio.StreamWriter | None = None
        good_tunnel_writer: asyncio.StreamWriter | None = None
        try:
            await write_message(control_writer, {"type": "hello", "version": 1})
            self.assertEqual((await read_message(control_reader) or {}).get("type"), "hello")
            await write_message(
                control_writer,
                {
                    "type": "register",
                    "services": [{"name": "secure", "token": "service-secret"}],
                },
            )
            self.assertEqual((await read_message(control_reader) or {}).get("type"), "registered")
            public_addr = await _wait_for_service(tunnel_server, "secure")
            public_reader, public_writer = await asyncio.open_connection(*public_addr)
            open_message = await asyncio.wait_for(read_message(control_reader), timeout=1)
            self.assertIsNotNone(open_message)
            tunnel_id = str(open_message["id"])

            bad_reader, bad_tunnel_writer = await asyncio.open_connection(control_host, control_port)
            await write_message(
                bad_tunnel_writer,
                {
                    "type": "tunnel",
                    "id": tunnel_id,
                    "service": "secure",
                    "token": "wrong",
                },
            )
            error_message = await asyncio.wait_for(read_message(bad_reader), timeout=1)
            self.assertEqual((error_message or {}).get("type"), "error")

            good_reader, good_tunnel_writer = await asyncio.open_connection(control_host, control_port)
            await write_message(
                good_tunnel_writer,
                {
                    "type": "tunnel",
                    "id": tunnel_id,
                    "service": "secure",
                    "token": "service-secret",
                },
            )
            public_writer.write(b"secret payload")
            await public_writer.drain()
            self.assertEqual(await asyncio.wait_for(good_reader.read(14), timeout=1), b"secret payload")
            good_tunnel_writer.write(b"accepted")
            await good_tunnel_writer.drain()
            self.assertEqual(await asyncio.wait_for(public_reader.read(8), timeout=1), b"accepted")
        finally:
            await close_writer(control_writer)
            await close_writer(public_writer)
            await close_writer(bad_tunnel_writer)
            await close_writer(good_tunnel_writer)
            await tunnel_server.close()


async def _wait_for_service(server: Server, name: str) -> tuple[str, int]:
    for _ in range(100):
        address = server.service_address(name)
        if address is not None:
            return address
        await asyncio.sleep(0.02)
    raise AssertionError(f"service {name!r} was not registered")


if __name__ == "__main__":
    unittest.main()
