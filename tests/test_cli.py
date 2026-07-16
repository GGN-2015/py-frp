from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

from py_frp.cli import (
    POOL_TOKEN_ENV,
    _load_client_command_config,
    _load_server_command_config,
    _print_token_pool,
    build_parser,
)
from py_frp.config import ConfigError
from py_frp.pool import TOKEN_ALPHABET, token_service_name


class CliTests(unittest.TestCase):
    def test_runtime_update_monitor_defaults_and_overrides(self) -> None:
        parser = build_parser()
        defaults = parser.parse_args(
            ["client", "--server", "example.com:7000", "--token", "secret"]
        )
        disabled = parser.parse_args(
            [
                "server",
                "--port-pool",
                "6000",
                "--no-auto-restart",
                "--update-check-interval",
                "1.5",
            ]
        )

        self.assertTrue(defaults.auto_restart)
        self.assertEqual(defaults.update_check_interval, 5.0)
        self.assertFalse(disabled.auto_restart)
        self.assertEqual(disabled.update_check_interval, 1.5)

    def test_configless_server_generates_one_shared_token_for_pool(self) -> None:
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
        self.assertEqual(len(config.pool_tokens), 1)
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
        self.assertEqual(len(config.pool_tokens), 1)

    def test_configless_server_prints_one_shared_token(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["server", "--port-pool", "6000-6002"])
        config = _load_server_command_config(args)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            _print_token_pool(config)

        token_lines = [line for line in output.getvalue().splitlines() if line.startswith("token ")]
        self.assertEqual(token_lines, [f"token {config.pool_tokens[0]}"])

    def test_configless_server_reuses_token_preserved_for_restart(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["server", "--port-pool", "6000-6002", "--token-length", "8"]
        )

        with mock.patch.dict(os.environ, {POOL_TOKEN_ENV: "preserved-token"}):
            config = _load_server_command_config(args)

        self.assertEqual(config.pool_tokens, ("preserved-token",))

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

    def test_configless_client_accepts_lan_target(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "client",
                "--server",
                "example.com:7000",
                "--token",
                "secret",
                "--server-fingerprint",
                "SHA256:" + ":".join(["AA"] * 32),
                "--local",
                "192.168.1.50:8080",
            ]
        )

        config = _load_client_command_config(args)

        self.assertEqual(config.server_fingerprint, "SHA256:" + ":".join(["AA"] * 32))
        self.assertEqual(config.proxies[0].local_host, "192.168.1.50")
        self.assertEqual(config.proxies[0].local_port, 8080)

    def test_configless_client_accepts_force_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "client",
                "--server",
                "example.com:7000",
                "--token",
                "secret",
                "--force",
                "--priority",
                "-2",
            ]
        )

        self.assertTrue(args.force)
        self.assertEqual(args.priority, -2)
        self.assertEqual(_load_client_command_config(args).source_flavor, "token-pool")

    def test_configless_client_priority_defaults_to_zero(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["client", "--server", "example.com:7000", "--token", "secret"]
        )

        self.assertEqual(args.priority, 0)

    def test_force_flag_is_rejected_with_config_file(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["client", "--config", "frpc.toml", "--force"])

        with self.assertRaisesRegex(ConfigError, "configless client mode"):
            _load_client_command_config(args)

    def test_priority_is_rejected_with_config_file(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["client", "--config", "frpc.toml", "--priority", "1"]
        )

        with self.assertRaisesRegex(ConfigError, "configless client mode"):
            _load_client_command_config(args)


if __name__ == "__main__":
    unittest.main()
