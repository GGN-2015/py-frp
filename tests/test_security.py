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

    def test_ellipsis_matches_whole_sha256_byte_sequences(self) -> None:
        actual = "SHA256:" + ":".join(
            ["12", "34", *("56" for _ in range(29)), "AB"]
        )
        pattern = "sha256:12:34:...:ab"

        self.assertEqual(normalize_fingerprint(pattern), "SHA256:12:34:...:AB")
        self.assertTrue(fingerprints_equal(actual, pattern))
        self.assertTrue(fingerprints_equal(pattern, actual))
        self.assertTrue(fingerprints_equal(actual, "..."))
        self.assertFalse(fingerprints_equal(actual, "SHA256:12:35:...:AB"))
        self.assertFalse(fingerprints_equal(actual, "SHA256:12:34:...:AC"))

    def test_rejects_unsafe_or_malformed_fingerprint_wildcards(self) -> None:
        invalid_patterns = (
            "SHA256:AA:...:BB:...:CC",
            "SHA256:A:...:BB",
            "SHA256:GG:...:BB",
            "SHA256:" + ":".join(["AA"] * 33) + ":...",
        )
        for pattern in invalid_patterns:
            with self.subTest(pattern=pattern), self.assertRaises(SecurityError):
                normalize_fingerprint(pattern)

    def test_bare_wildcard_warns_when_client_starts(self) -> None:
        config = ClientConfig(
            server_host="example.com",
            server_port=7000,
            server_fingerprint="...",
            proxies=(
                ProxyConfig(
                    name="ssh",
                    local_host="127.0.0.1",
                    local_port=22,
                    remote_port=6000,
                ),
            ),
        )

        with self.assertLogs("py_frp.client", level="WARNING") as captured:
            Client(config)

        self.assertIn("every TLS certificate will match", captured.output[0])

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
