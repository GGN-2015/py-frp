from __future__ import annotations

import os
import unittest
from unittest import mock

from py_frp.client import Client
from py_frp.config import ClientConfig, ProxyConfig
from py_frp.security import (
    RESTART_CERT_ENV,
    RESTART_KEY_ENV,
    RESTART_SERVER_FINGERPRINT_ENV,
    SecurityError,
    create_server_tls,
    fingerprints_equal,
    normalize_fingerprint,
)


class SecurityTests(unittest.TestCase):
    def test_normalizes_sha256_fingerprint(self) -> None:
        compact = "ab" * 32
        expected = "SHA256:" + ":".join(["AB"] * 32)

        self.assertEqual(normalize_fingerprint(compact), expected)
        self.assertTrue(fingerprints_equal(compact, expected))

    def test_rejects_wrong_fingerprint_length(self) -> None:
        with self.assertRaisesRegex(SecurityError, "exactly 32"):
            normalize_fingerprint("SHA256:AA:BB")

    def test_server_tls_fingerprint_survives_restart_state_round_trip(self) -> None:
        with mock.patch.dict(
            os.environ,
            {RESTART_CERT_ENV: "", RESTART_KEY_ENV: ""},
        ):
            first = create_server_tls("127.0.0.1")
            try:
                first_fingerprint = first.fingerprint
                first.preserve_for_restart()
            finally:
                first.close()

            second = create_server_tls("127.0.0.1")
            try:
                self.assertEqual(second.fingerprint, first_fingerprint)
            finally:
                second.close()

    def test_client_inherits_confirmed_fingerprint_after_restart(self) -> None:
        fingerprint = "SHA256:" + ":".join(["AB"] * 32)
        config = ClientConfig(
            server_host="example.com",
            server_port=7000,
            proxies=(
                ProxyConfig(
                    name="ssh",
                    local_host="127.0.0.1",
                    local_port=22,
                    remote_port=6000,
                ),
            ),
        )

        with mock.patch.dict(
            os.environ,
            {RESTART_SERVER_FINGERPRINT_ENV: fingerprint},
        ):
            client = Client(config)
            client.preserve_fingerprint_for_restart()
            self.assertEqual(
                os.environ[RESTART_SERVER_FINGERPRINT_ENV],
                fingerprint,
            )


if __name__ == "__main__":
    unittest.main()
