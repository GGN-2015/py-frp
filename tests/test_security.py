from __future__ import annotations

import unittest

from py_frp.security import SecurityError, fingerprints_equal, normalize_fingerprint


class SecurityTests(unittest.TestCase):
    def test_normalizes_sha256_fingerprint(self) -> None:
        compact = "ab" * 32
        expected = "SHA256:" + ":".join(["AB"] * 32)

        self.assertEqual(normalize_fingerprint(compact), expected)
        self.assertTrue(fingerprints_equal(compact, expected))

    def test_rejects_wrong_fingerprint_length(self) -> None:
        with self.assertRaisesRegex(SecurityError, "exactly 32"):
            normalize_fingerprint("SHA256:AA:BB")


if __name__ == "__main__":
    unittest.main()
