import base64
import binascii
import datetime as dt
import hashlib
import hmac
import ipaddress
import os
import re
import ssl
import sys
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

if sys.version_info < (3, 7):
    warnings.filterwarnings(
        "ignore",
        message="Python 3.6 is no longer supported by the Python core team.*",
    )

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from .compat import require_tls12


class SecurityError(RuntimeError):
    """Raised when TLS setup or server identity verification fails."""


RESTART_CERT_ENV = "PY_FRP_RESTART_TLS_CERT"
RESTART_KEY_ENV = "PY_FRP_RESTART_TLS_KEY"
RESTART_SERVER_FINGERPRINT_ENV = "PY_FRP_RESTART_SERVER_FINGERPRINT"


@dataclass
class ServerTLS:
    context: ssl.SSLContext
    fingerprint: str
    _directory: tempfile.TemporaryDirectory
    _certificate_pem: bytes
    _private_key_pem: bytes

    def close(self) -> None:
        self._directory.cleanup()

    def preserve_for_restart(self) -> None:
        os.environ[RESTART_CERT_ENV] = base64.b64encode(self._certificate_pem).decode("ascii")
        os.environ[RESTART_KEY_ENV] = base64.b64encode(self._private_key_pem).decode("ascii")


def create_server_tls(bind_host: str) -> ServerTLS:
    restored = _restored_tls_material()
    if restored is None:
        certificate_pem, private_key_pem = _generate_tls_material(bind_host)
    else:
        certificate_pem, private_key_pem = restored
    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem)
    except ValueError as exc:
        raise SecurityError("preserved TLS certificate is invalid") from exc

    directory = tempfile.TemporaryDirectory(prefix="py-frp-tls-")
    root = Path(directory.name)
    cert_path = root / "server-cert.pem"
    key_path = root / "server-key.pem"
    cert_path.write_bytes(certificate_pem)
    key_path.write_bytes(private_key_pem)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    require_tls12(context)
    try:
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    except (OSError, ssl.SSLError) as exc:
        directory.cleanup()
        raise SecurityError("preserved TLS certificate and private key are invalid") from exc
    fingerprint = format_fingerprint(certificate.fingerprint(hashes.SHA256()))
    return ServerTLS(
        context=context,
        fingerprint=fingerprint,
        _directory=directory,
        _certificate_pem=certificate_pem,
        _private_key_pem=private_key_pem,
    )


def _generate_tls_material(bind_host: str) -> Tuple[bytes, bytes]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "py-frp ephemeral server")]
    )
    now = dt.datetime.now(dt.timezone.utc)
    alternative_names: List[x509.GeneralName] = [x509.DNSName("localhost")]
    try:
        if bind_host not in {"0.0.0.0", "::", ""}:
            alternative_names.append(x509.IPAddress(ipaddress.ip_address(bind_host)))
    except ValueError:
        alternative_names.append(x509.DNSName(bind_host))

    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName(alternative_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _restored_tls_material() -> Optional[Tuple[bytes, bytes]]:
    certificate = os.environ.get(RESTART_CERT_ENV)
    private_key = os.environ.get(RESTART_KEY_ENV)
    if not certificate and not private_key:
        return None
    if not certificate or not private_key:
        raise SecurityError("preserved TLS restart state is incomplete")
    try:
        return (
            base64.b64decode(certificate, validate=True),
            base64.b64decode(private_key, validate=True),
        )
    except (ValueError, binascii.Error) as exc:
        raise SecurityError("preserved TLS restart state is invalid") from exc


def create_client_tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    require_tls12(context)
    # The ephemeral certificate is authenticated by an explicit SHA-256 pin.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def peer_fingerprint(writer: object) -> str:
    get_extra_info = getattr(writer, "get_extra_info", None)
    if get_extra_info is None:
        raise SecurityError("connection does not expose TLS information")
    ssl_object = get_extra_info("ssl_object")
    if ssl_object is None:
        raise SecurityError("server connection is not encrypted with TLS")
    certificate = ssl_object.getpeercert(binary_form=True)
    if not certificate:
        raise SecurityError("server did not provide a TLS certificate")
    return format_fingerprint(hashlib.sha256(certificate).digest())


def normalize_fingerprint(value: str) -> str:
    prefix, suffix, wildcard = _parse_fingerprint(value)
    parts = [f"{byte:02X}" for byte in prefix]
    if wildcard:
        parts.append("...")
        parts.extend(f"{byte:02X}" for byte in suffix)
    return "SHA256:" + ":".join(parts)


def fingerprints_equal(left: str, right: str) -> bool:
    try:
        left_prefix, left_suffix, left_wildcard = _parse_fingerprint(left)
        right_prefix, right_suffix, right_wildcard = _parse_fingerprint(right)
    except SecurityError:
        return False
    if left_wildcard and right_wildcard:
        return hmac.compare_digest(
            normalize_fingerprint(left),
            normalize_fingerprint(right),
        )
    if left_wildcard:
        return _wildcard_fingerprint_matches(
            right_prefix,
            left_prefix,
            left_suffix,
        )
    if right_wildcard:
        return _wildcard_fingerprint_matches(
            left_prefix,
            right_prefix,
            right_suffix,
        )
    return hmac.compare_digest(left_prefix, right_prefix)


def _parse_fingerprint(value: str) -> Tuple[bytes, bytes, bool]:
    text = value.strip().upper()
    if text.startswith("SHA256:"):
        text = text[7:]
    wildcard_count = text.count("...")
    if wildcard_count > 1:
        raise SecurityError("server fingerprint may contain at most one '...' wildcard")
    if wildcard_count == 0:
        digest = _parse_fingerprint_bytes(text)
        if len(digest) != 32:
            raise SecurityError(
                "server fingerprint must contain exactly 32 SHA-256 bytes"
            )
        return digest, b"", False

    raw_prefix, raw_suffix = text.split("...", 1)
    prefix = _parse_fingerprint_bytes(raw_prefix)
    suffix = _parse_fingerprint_bytes(raw_suffix)
    if len(prefix) + len(suffix) > 32:
        raise SecurityError(
            "server fingerprint wildcard contains more than 32 fixed bytes"
        )
    return prefix, suffix, True


def _parse_fingerprint_bytes(value: str) -> bytes:
    if re.search(r"[^0-9A-F:\-\s]", value):
        raise SecurityError("server fingerprint contains invalid characters")
    hex_value = re.sub(r"[:\-\s]", "", value)
    if len(hex_value) % 2:
        raise SecurityError("server fingerprint bytes must use two hexadecimal digits")
    return bytes.fromhex(hex_value)


def _wildcard_fingerprint_matches(
    actual: bytes,
    prefix: bytes,
    suffix: bytes,
) -> bool:
    if len(actual) != 32 or len(prefix) + len(suffix) > len(actual):
        return False
    actual_suffix = actual[len(actual) - len(suffix) :] if suffix else b""
    return hmac.compare_digest(actual[: len(prefix)], prefix) & hmac.compare_digest(
        actual_suffix,
        suffix,
    )


def format_fingerprint(digest: bytes) -> str:
    return "SHA256:" + ":".join(f"{byte:02X}" for byte in digest)
