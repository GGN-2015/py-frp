from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import hmac
import ipaddress
import os
import re
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


class SecurityError(RuntimeError):
    """Raised when TLS setup or server identity verification fails."""


RESTART_CERT_ENV = "PY_FRP_RESTART_TLS_CERT"
RESTART_KEY_ENV = "PY_FRP_RESTART_TLS_KEY"
RESTART_SERVER_FINGERPRINT_ENV = "PY_FRP_RESTART_SERVER_FINGERPRINT"


@dataclass
class ServerTLS:
    context: ssl.SSLContext
    fingerprint: str
    _directory: tempfile.TemporaryDirectory[str]
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
    context.minimum_version = ssl.TLSVersion.TLSv1_2
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


def _generate_tls_material(bind_host: str) -> tuple[bytes, bytes]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "py-frp ephemeral server")]
    )
    now = dt.datetime.now(dt.timezone.utc)
    alternative_names: list[x509.GeneralName] = [x509.DNSName("localhost")]
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


def _restored_tls_material() -> tuple[bytes, bytes] | None:
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
    context.minimum_version = ssl.TLSVersion.TLSv1_2
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
    text = value.strip().upper()
    if text.startswith("SHA256:"):
        text = text[7:]
    hex_value = re.sub(r"[^0-9A-F]", "", text)
    if len(hex_value) != 64:
        raise SecurityError("server fingerprint must contain exactly 32 SHA-256 bytes")
    return "SHA256:" + ":".join(
        hex_value[index : index + 2] for index in range(0, len(hex_value), 2)
    )


def fingerprints_equal(left: str, right: str) -> bool:
    try:
        normalized_left = normalize_fingerprint(left)
        normalized_right = normalize_fingerprint(right)
    except SecurityError:
        return False
    return hmac.compare_digest(normalized_left, normalized_right)


def format_fingerprint(digest: bytes) -> str:
    return "SHA256:" + ":".join(f"{byte:02X}" for byte in digest)
