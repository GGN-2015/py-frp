from __future__ import annotations

import unittest

from py_frp.cli import build_parser, _load_client_command_config, _load_server_command_config
from py_frp.pool import TOKEN_ALPHABET, token_service_name


class CliTests(unittest.TestCase):
    def test_configless_server_generates_one_token_per_pool_port(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "server",
                "--bind-host",
                "127.0.0.1",
                "--bind-port",
                "7000",
                "--port-pool",
                "6000-6002",
                "--token-length",
                "16",
            ]
        )

        config = _load_server_command_config(args)

        self.assertEqual(config.source_flavor, "token-pool")
        self.assertEqual(config.bind_host, "127.0.0.1")
        self.assertEqual(config.bind_port, 7000)
        self.assertEqual(config.port_pool, (6000, 6001, 6002))
        self.assertEqual(len(config.pool_tokens), 3)
        for token in config.pool_tokens:
            self.assertEqual(len(token), 16)
            self.assertTrue(set(token) <= set(TOKEN_ALPHABET))

    def test_configless_server_merges_repeated_port_pools(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "server",
                "--port-pool",
                "6000-6002",
                "--port-pool",
                "6002,6004",
                "--port-pool",
                "6003",
            ]
        )

        config = _load_server_command_config(args)

        self.assertEqual(config.port_pool, (6000, 6001, 6002, 6004, 6003))
        self.assertEqual(len(config.pool_tokens), 5)

    def test_configless_client_uses_token_service_name(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "client",
                "--server",
                "example.com:7000",
                "--token",
                "secret",
                "--local",
                "127.0.0.1:8080",
            ]
        )

        config = _load_client_command_config(args)

        self.assertEqual(config.source_flavor, "token-pool")
        self.assertEqual(config.server_host, "example.com")
        self.assertEqual(config.server_port, 7000)
        self.assertEqual(config.token, "secret")
        self.assertEqual(config.proxies[0].name, token_service_name("secret"))
        self.assertEqual(config.proxies[0].local_port, 8080)


if __name__ == "__main__":
    unittest.main()
