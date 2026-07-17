import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import unittest

from py_frp import __version__
from py_frp.client import Client
from py_frp.compat import create_task, run
from py_frp.config import ClientConfig, ProxyConfig, ServerConfig
from py_frp.protocol import close_writer
from py_frp.server import Server
from py_frp.update import installed_version


class Python36RuntimeTests(unittest.TestCase):
    def test_end_to_end_pool_tunnel(self):
        run(self._exercise_end_to_end_pool_tunnel())

    async def _exercise_end_to_end_pool_tunnel(self):
        echo_server = await asyncio.start_server(
            _echo_connection,
            "127.0.0.1",
            0,
        )
        echo_port = echo_server.sockets[0].getsockname()[1]
        pool_port = _unused_tcp_port()
        tunnel_server = Server(
            ServerConfig(
                bind_host="127.0.0.1",
                bind_port=0,
                services=(),
                allow_dynamic=False,
                source_flavor="token-pool",
                port_pool=(pool_port,),
                pool_tokens=("python36-token",),
                pool_bind_host="127.0.0.1",
            )
        )
        await tunnel_server.start()
        control_port = tunnel_server.control_addresses()[0][1]
        registered = asyncio.Event()
        client = Client(
            ClientConfig(
                server_host="127.0.0.1",
                server_port=control_port,
                proxies=(
                    ProxyConfig(
                        name="python36",
                        local_host="127.0.0.1",
                        local_port=echo_port,
                        token="python36-token",
                    ),
                ),
                token="python36-token",
                source_flavor="token-pool",
                reconnect_delay=0.05,
                connect_timeout=2.0,
                heartbeat_interval=1.0,
                server_fingerprint=tunnel_server.tls_fingerprint,
            ),
            on_registered=lambda message: registered.set(),
        )
        client_task = create_task(client.run())
        public_writer = None
        try:
            await asyncio.wait_for(registered.wait(), timeout=5)
            public_reader, public_writer = await asyncio.open_connection(
                "127.0.0.1",
                pool_port,
            )
            public_writer.write(b"python-3.6-round-trip")
            await public_writer.drain()
            response = await asyncio.wait_for(
                public_reader.readexactly(len(b"python-3.6-round-trip")),
                timeout=5,
            )
            self.assertEqual(response, b"python-3.6-round-trip")
            self.assertEqual(installed_version(), __version__)
        finally:
            await close_writer(public_writer)
            client_task.cancel()
            await asyncio.gather(client_task, return_exceptions=True)
            await tunnel_server.close()
            echo_server.close()
            await echo_server.wait_closed()

    def test_supervisor_stops_child_and_listener_on_console_interrupt(self):
        control_port = _unused_tcp_port()
        pool_port = _unused_tcp_port()
        options = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "py_frp",
                "server",
                "--bind-host",
                "127.0.0.1",
                "--bind-port",
                str(control_port),
                "--port-pool",
                str(pool_port),
                "--no-auto-elevate",
                "--no-auto-restart",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            **options
        )
        try:
            _wait_for_listener(control_port, process)
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGINT)
            returncode = process.wait(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            stdout, stderr = process.communicate()

        self.assertEqual(
            returncode,
            130,
            "stdout:\n{}\nstderr:\n{}".format(stdout, stderr),
        )
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", control_port), timeout=0.2)


async def _echo_connection(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            writer.write(data)
            await writer.drain()
    finally:
        await close_writer(writer)


def _unused_tcp_port():
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]
    finally:
        listener.close()


def _wait_for_listener(port, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "server exited before listening ({})\nstdout:\n{}\nstderr:\n{}".format(
                    process.returncode,
                    stdout,
                    stderr,
                )
            )
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.1)
            connection.close()
            return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("server did not listen on 127.0.0.1:{}".format(port))


if __name__ == "__main__":
    unittest.main()
