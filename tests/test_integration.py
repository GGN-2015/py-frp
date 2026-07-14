from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import socket
import struct
import unittest

from py_frp.client import Client
from py_frp.cli import _load_client_command_config, build_parser
from py_frp.config import ClientConfig, ProxyConfig, ServerConfig, ServerServiceConfig
from py_frp.pool import token_service_name
from py_frp.protocol import close_writer, read_message, write_message
from py_frp.security import (
    SecurityError,
    create_client_tls_context,
    fingerprints_equal,
    peer_fingerprint,
)
from py_frp.server import Server


class TunnelIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await asyncio.sleep(0)

    async def test_tls_fingerprint_is_printed_confirmed_and_pinned(self) -> None:
        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                token="secret",
                allow_dynamic=True,
            )
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await tunnel_server.start()
        control_port = tunnel_server.control_addresses()[0][1]
        confirmed: list[str] = []

        def confirm(value: str) -> bool:
            confirmed.append(value)
            return True

        client = Client(
            ClientConfig(
                server_host="127.0.0.1",
                server_port=control_port,
                token="secret",
                proxies=(
                    ProxyConfig(
                        name="unused",
                        local_host="127.0.0.1",
                        local_port=1,
                        remote_host="127.0.0.1",
                        remote_port=0,
                    ),
                ),
            ),
            confirm_fingerprint=confirm,
        )
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer = await client._open_server_connection()
            self.assertEqual(confirmed, [tunnel_server.tls_fingerprint])
            self.assertIn(
                f"tls_fingerprint {tunnel_server.tls_fingerprint}",
                output.getvalue(),
            )
            self.assertIsNotNone(writer.get_extra_info("ssl_object"))

            wrong_client = Client(
                ClientConfig(
                    server_host="127.0.0.1",
                    server_port=control_port,
                    token="secret",
                    server_fingerprint="SHA256:" + ":".join(["00"] * 32),
                    proxies=(
                        ProxyConfig(
                            name="unused",
                            local_host="127.0.0.1",
                            local_port=1,
                        ),
                    ),
                )
            )
            with self.assertRaisesRegex(SecurityError, "fingerprint changed"):
                await wrong_client._open_server_connection()

            rejected_client = Client(
                ClientConfig(
                    server_host="127.0.0.1",
                    server_port=control_port,
                    token="secret",
                    proxies=(
                        ProxyConfig(
                            name="unused",
                            local_host="127.0.0.1",
                            local_port=1,
                        ),
                    ),
                ),
                confirm_fingerprint=lambda _: False,
            )
            with self.assertRaisesRegex(SecurityError, "fingerprint was rejected"):
                await rejected_client._open_server_connection()
        finally:
            await close_writer(writer)
            await tunnel_server.close()

    async def test_plaintext_control_connection_is_rejected(self) -> None:
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
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_connection(control_host, control_port)
            writer.write(b'{"type":"hello","version":1}\n')
            await writer.drain()
            try:
                response = await asyncio.wait_for(reader.read(1), timeout=1)
            except (ConnectionError, OSError):
                response = b""
            self.assertEqual(response, b"")
        finally:
            await close_writer(writer)
            await tunnel_server.close()

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
            ),
            confirm_fingerprint=_confirm_fingerprint,
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

    async def test_configless_client_tunnels_to_lan_address(self) -> None:
        lan_host = _non_loopback_ipv4()
        if lan_host is None:
            self.skipTest("no non-loopback IPv4 address is available")

        async def echo(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            data = await reader.read(1024)
            writer.write(b"lan:" + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        try:
            echo_server = await asyncio.start_server(echo, lan_host, 0)
        except OSError as exc:
            self.skipTest(f"cannot bind test service to LAN address {lan_host}: {exc}")
        echo_port = int(echo_server.sockets[0].getsockname()[1])
        token = "lan-secret"
        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                allow_dynamic=False,
                port_pool=(0,),
                pool_tokens=(token,),
                pool_bind_host="127.0.0.1",
            )
        )
        await tunnel_server.start()
        control_port = tunnel_server.control_addresses()[0][1]
        args = build_parser().parse_args(
            [
                "client",
                "--server",
                f"127.0.0.1:{control_port}",
                "--token",
                token,
                "--server-fingerprint",
                tunnel_server.tls_fingerprint,
                "--local",
                f"{lan_host}:{echo_port}",
                "--reconnect-delay",
                "0.1",
            ]
        )
        client_config = _load_client_command_config(args)
        self.assertEqual(client_config.source_flavor, "token-pool")
        self.assertEqual(client_config.proxies[0].local_host, lan_host)
        assigned: list[dict[str, object]] = []
        client = Client(
            client_config,
            on_registered=_capture_registered_services(assigned),
            confirm_fingerprint=_confirm_fingerprint,
        )
        client_task = asyncio.create_task(client.run())
        try:
            public_addr = await _wait_for_assigned_address(assigned)
            reader, writer = await asyncio.open_connection(*public_addr)
            writer.write(b"hello")
            await writer.drain()
            self.assertEqual(await reader.read(9), b"lan:hello")
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
            ),
            confirm_fingerprint=_confirm_fingerprint,
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
            ),
            confirm_fingerprint=_confirm_fingerprint,
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
            bad_reader, bad_writer = await _open_tls_connection(
                control_host,
                control_port,
                tunnel_server.tls_fingerprint,
            )
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

            good_reader, good_writer = await _open_tls_connection(
                control_host,
                control_port,
                tunnel_server.tls_fingerprint,
            )
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
        control_reader, control_writer = await _open_tls_connection(
            control_host,
            control_port,
            tunnel_server.tls_fingerprint,
        )
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

            bad_reader, bad_tunnel_writer = await _open_tls_connection(
                control_host,
                control_port,
                tunnel_server.tls_fingerprint,
            )
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

            good_reader, good_tunnel_writer = await _open_tls_connection(
                control_host,
                control_port,
                tunnel_server.tls_fingerprint,
            )
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
            self.assertEqual(
                await asyncio.wait_for(_read_tunnel_data(good_reader), timeout=1),
                b"secret payload",
            )
            await _write_tunnel_data(good_tunnel_writer, b"accepted")
            self.assertEqual(await asyncio.wait_for(public_reader.read(8), timeout=1), b"accepted")
        finally:
            await close_writer(control_writer)
            await close_writer(public_writer)
            await close_writer(bad_tunnel_writer)
            await close_writer(good_tunnel_writer)
            await tunnel_server.close()

    async def test_token_pool_assigns_remote_port_and_tunnels(self) -> None:
        async def echo(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            data = await reader.read(1024)
            writer.write(b"pool:" + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        token = "pool-secret"
        service_name = token_service_name(token)
        echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        echo_port = int(echo_server.sockets[0].getsockname()[1])
        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                allow_dynamic=False,
                port_pool=(0,),
                pool_tokens=(token,),
                pool_bind_host="127.0.0.1",
            )
        )
        await tunnel_server.start()
        control_port = tunnel_server.control_addresses()[0][1]
        assigned: list[dict[str, object]] = []
        client = Client(
            ClientConfig(
                server_host="127.0.0.1",
                server_port=control_port,
                token=token,
                reconnect_delay=0.1,
                proxies=(
                    ProxyConfig(
                        name=service_name,
                        local_host="127.0.0.1",
                        local_port=echo_port,
                        token=token,
                    ),
                ),
            ),
            on_registered=_capture_registered_services(assigned),
            confirm_fingerprint=_confirm_fingerprint,
        )
        client_task = asyncio.create_task(client.run())
        try:
            public_addr = await _wait_for_assigned_address(assigned)
            online = await tunnel_server.online_pool_clients()
            self.assertIn(public_addr, online.values())
            reader, writer = await asyncio.open_connection(*public_addr)
            writer.write(b"hello")
            await writer.drain()
            self.assertEqual(await reader.read(10), b"pool:hello")
            writer.close()
            await writer.wait_closed()
        finally:
            client_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client_task
            await tunnel_server.close()
            echo_server.close()
            await echo_server.wait_closed()

    async def test_shared_token_allows_multiple_pool_clients(self) -> None:
        async def echo_first(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await reader.read(1024)
            writer.write(b"first")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async def echo_second(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await reader.read(1024)
            writer.write(b"second")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        token = "shared-secret"
        service_name = token_service_name(token)
        first_public_port = await _unused_tcp_port()
        second_public_port = await _unused_tcp_port()
        while second_public_port == first_public_port:
            second_public_port = await _unused_tcp_port()
        first_local = await asyncio.start_server(echo_first, "127.0.0.1", 0)
        second_local = await asyncio.start_server(echo_second, "127.0.0.1", 0)
        first_port = int(first_local.sockets[0].getsockname()[1])
        second_port = int(second_local.sockets[0].getsockname()[1])
        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                allow_dynamic=False,
                port_pool=(first_public_port, second_public_port),
                pool_tokens=(token,),
                pool_bind_host="127.0.0.1",
            )
        )
        await tunnel_server.start()
        control_port = tunnel_server.control_addresses()[0][1]
        first_assigned: list[dict[str, object]] = []
        second_assigned: list[dict[str, object]] = []

        first_client = Client(
            ClientConfig(
                server_host="127.0.0.1",
                server_port=control_port,
                token=token,
                reconnect_delay=0.1,
                proxies=(
                    ProxyConfig(
                        name=service_name,
                        local_host="127.0.0.1",
                        local_port=first_port,
                        token=token,
                    ),
                ),
            ),
            on_registered=_capture_registered_services(first_assigned),
            confirm_fingerprint=_confirm_fingerprint,
        )
        second_client = Client(
            ClientConfig(
                server_host="127.0.0.1",
                server_port=control_port,
                token=token,
                reconnect_delay=0.1,
                proxies=(
                    ProxyConfig(
                        name=service_name,
                        local_host="127.0.0.1",
                        local_port=second_port,
                        token=token,
                    ),
                ),
            ),
            on_registered=_capture_registered_services(second_assigned),
            confirm_fingerprint=_confirm_fingerprint,
        )
        first_task = asyncio.create_task(first_client.run())
        second_task: asyncio.Task[None] | None = None
        try:
            first_addr = await _wait_for_assigned_address(first_assigned)

            second_task = asyncio.create_task(second_client.run())
            second_addr = await _wait_for_assigned_address(second_assigned)
            self.assertFalse(first_task.done())
            self.assertFalse(second_task.done())
            expected_ports = sorted((first_public_port, second_public_port))
            self.assertEqual(first_addr[1], expected_ports[0])
            self.assertEqual(second_addr[1], expected_ports[1])

            reader, writer = await asyncio.open_connection(*first_addr)
            writer.write(b"x")
            await writer.drain()
            self.assertEqual(await reader.read(5), b"first")
            writer.close()
            await writer.wait_closed()

            reader, writer = await asyncio.open_connection(*second_addr)
            writer.write(b"x")
            await writer.drain()
            self.assertEqual(await reader.read(6), b"second")
            writer.close()
            await writer.wait_closed()
        finally:
            for task in (first_task, second_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            await tunnel_server.close()
            first_local.close()
            second_local.close()
            await first_local.wait_closed()
            await second_local.wait_closed()

    async def test_pool_exhaustion_rejects_new_client_without_disconnecting_existing_client(self) -> None:
        async def echo_first(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await reader.read(1024)
            writer.write(b"first")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async def echo_second(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await reader.read(1024)
            writer.write(b"second")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        token = "shared-secret"
        service_name = token_service_name(token)
        public_port = await _unused_tcp_port()
        first_local = await asyncio.start_server(echo_first, "127.0.0.1", 0)
        second_local = await asyncio.start_server(echo_second, "127.0.0.1", 0)
        first_port = int(first_local.sockets[0].getsockname()[1])
        second_port = int(second_local.sockets[0].getsockname()[1])
        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                allow_dynamic=False,
                port_pool=(public_port,),
                pool_tokens=(token,),
                pool_bind_host="127.0.0.1",
            )
        )
        await tunnel_server.start()
        control_port = tunnel_server.control_addresses()[0][1]
        assigned: list[dict[str, object]] = []

        first_client = Client(
            ClientConfig(
                server_host="127.0.0.1",
                server_port=control_port,
                token=token,
                reconnect_delay=0.1,
                proxies=(
                    ProxyConfig(
                        name=service_name,
                        local_host="127.0.0.1",
                        local_port=first_port,
                        token=token,
                    ),
                ),
            ),
            on_registered=_capture_registered_services(assigned),
            confirm_fingerprint=_confirm_fingerprint,
        )
        second_client = Client(
            ClientConfig(
                server_host="127.0.0.1",
                server_port=control_port,
                token=token,
                reconnect_delay=0.1,
                proxies=(
                    ProxyConfig(
                        name=service_name,
                        local_host="127.0.0.1",
                        local_port=second_port,
                        token=token,
                    ),
                ),
            ),
            confirm_fingerprint=_confirm_fingerprint,
        )

        first_task = asyncio.create_task(first_client.run())
        try:
            public_addr = await _wait_for_assigned_address(assigned)
            with self.assertLogs("py_frp.client", level="ERROR") as logs:
                await asyncio.wait_for(second_client.run(), timeout=2)
            self.assertTrue(any("resource insufficient" in message for message in logs.output))
            self.assertFalse(first_task.done())

            reader, writer = await asyncio.open_connection(*public_addr)
            writer.write(b"x")
            await writer.drain()
            self.assertEqual(await reader.read(5), b"first")
            writer.close()
            await writer.wait_closed()
        finally:
            if not first_task.done():
                first_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await first_task
            await tunnel_server.close()
            first_local.close()
            second_local.close()
            await first_local.wait_closed()
            await second_local.wait_closed()


async def _wait_for_service(server: Server, name: str) -> tuple[str, int]:
    for _ in range(100):
        address = server.service_address(name)
        if address is not None:
            return address
        await asyncio.sleep(0.02)
    raise AssertionError(f"service {name!r} was not registered")


def _capture_registered_services(target: list[dict[str, object]]):
    def capture(message: dict[str, object]) -> None:
        services = message.get("services")
        if not isinstance(services, list):
            return
        target.extend(service for service in services if isinstance(service, dict))

    return capture


def _confirm_fingerprint(fingerprint: str) -> bool:
    return fingerprint.startswith("SHA256:")


async def _open_tls_connection(
    host: str,
    port: int,
    expected_fingerprint: str,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection(
        host,
        port,
        ssl=create_client_tls_context(),
    )
    if not fingerprints_equal(peer_fingerprint(writer), expected_fingerprint):
        await close_writer(writer)
        raise AssertionError("test TLS fingerprint mismatch")
    return reader, writer


async def _read_tunnel_data(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(5)
    if header[:1] != b"D":
        raise AssertionError(f"expected tunnel data frame, got {header[:1]!r}")
    return await reader.readexactly(struct.unpack("!I", header[1:])[0])


async def _write_tunnel_data(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(b"D" + struct.pack("!I", len(payload)) + payload)
    await writer.drain()


async def _wait_for_assigned_address(services: list[dict[str, object]]) -> tuple[str, int]:
    for _ in range(100):
        if services:
            service = services[0]
            return str(service["bind_host"]), int(service["bind_port"])
        await asyncio.sleep(0.02)
    raise AssertionError("client was not assigned a pool port")


async def _unused_tcp_port() -> int:
    async def close_immediately(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(close_immediately, "127.0.0.1", 0)
    try:
        return int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


def _non_loopback_ipv4() -> str | None:
    candidates: set[str] = set()
    try:
        candidates.update(
            address[4][0]
            for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        )
    except socket.gaierror:
        pass

    # A UDP connect selects the interface route without sending any packets.
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
            candidates.add(str(probe.getsockname()[0]))
        except OSError:
            pass

    for candidate in sorted(candidates):
        address = ipaddress.ip_address(candidate)
        if not address.is_loopback and not address.is_unspecified and not address.is_link_local:
            return candidate
    return None


if __name__ == "__main__":
    unittest.main()
