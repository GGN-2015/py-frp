from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from py_frp.config import ConfigError, load_client_config, load_server_config


class ConfigTests(unittest.TestCase):
    def test_load_frp_toml_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server_path = root / "frps.toml"
            client_path = root / "frpc.toml"
            server_path.write_text(
                textwrap.dedent(
                    """
                    bindPort = 7000
                    auth.token = "secret"
                    """
                ),
                encoding="utf-8",
            )
            client_path.write_text(
                textwrap.dedent(
                    """
                    serverAddr = "example.com"
                    serverPort = 7000
                    auth.token = "secret"

                    [[proxies]]
                    name = "ssh"
                    type = "tcp"
                    localIP = "127.0.0.1"
                    localPort = 22
                    remotePort = 6000
                    """
                ),
                encoding="utf-8",
            )

            server = load_server_config(server_path)
            client = load_client_config(client_path)

            self.assertEqual(server.bind_port, 7000)
            self.assertEqual(server.token, "secret")
            self.assertTrue(server.allow_dynamic)
            self.assertEqual(client.server_host, "example.com")
            self.assertEqual(client.proxies[0].remote_port, 6000)

    def test_load_rathole_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server_path = root / "server.toml"
            client_path = root / "client.toml"
            server_path.write_text(
                textwrap.dedent(
                    """
                    [server]
                    bind_addr = "0.0.0.0:2333"
                    default_token = "secret"

                    [server.services.ssh]
                    bind_addr = "0.0.0.0:6000"
                    """
                ),
                encoding="utf-8",
            )
            client_path.write_text(
                textwrap.dedent(
                    """
                    [client]
                    remote_addr = "server.example:2333"
                    default_token = "secret"

                    [client.services.ssh]
                    local_addr = "127.0.0.1:22"
                    """
                ),
                encoding="utf-8",
            )

            server = load_server_config(server_path)
            client = load_client_config(client_path)

            self.assertFalse(server.allow_dynamic)
            self.assertEqual(server.services[0].name, "ssh")
            self.assertEqual(server.services[0].token, "secret")
            self.assertEqual(client.server_host, "server.example")
            self.assertIsNone(client.proxies[0].remote_port)

    def test_load_legacy_frp_ini_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frpc.ini"
            path.write_text(
                textwrap.dedent(
                    """
                    [common]
                    server_addr = 127.0.0.1
                    server_port = 7000
                    token = secret

                    [ssh]
                    type = tcp
                    local_ip = 127.0.0.1
                    local_port = 22
                    remote_port = 6000
                    """
                ),
                encoding="utf-8",
            )

            client = load_client_config(path)

            self.assertEqual(client.source_flavor, "frp-ini")
            self.assertEqual(client.token, "secret")
            self.assertEqual(client.proxies[0].name, "ssh")

    def test_rejects_unsupported_proxy_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frpc.toml"
            path.write_text(
                textwrap.dedent(
                    """
                    serverAddr = "127.0.0.1"
                    serverPort = 7000

                    [[proxies]]
                    name = "dns"
                    type = "udp"
                    localIP = "127.0.0.1"
                    localPort = 53
                    remotePort = 5353
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "only tcp proxies"):
                load_client_config(path)

    def test_rejects_invalid_port_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frpc.toml"
            path.write_text(
                textwrap.dedent(
                    """
                    serverAddr = "127.0.0.1"
                    serverPort = 7000

                    [[proxies]]
                    name = "ssh"
                    type = "tcp"
                    localIP = "127.0.0.1"
                    localPort = 22
                    remotePort = 70000
                    """
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "outside the valid range"):
                load_client_config(path)


if __name__ == "__main__":
    unittest.main()
