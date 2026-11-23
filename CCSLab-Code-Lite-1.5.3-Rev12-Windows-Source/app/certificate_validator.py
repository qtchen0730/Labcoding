"""Certificate chain, expiry, SAN and trust validation.

Provides validation logic that can be used both during detection and
after configuration to confirm certificate correctness.
"""

from __future__ import annotations

import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlsplit

from certificate_manager import (
    CertificateError, CertificateInfo,
    STATUS_VALID, STATUS_EXPIRING_SOON, STATUS_EXPIRED,
    STATUS_DOMAIN_MISMATCH, STATUS_UNTRUSTED, STATUS_NOT_CONFIGURED,
    _EXPIRY_WARNING_DAYS,
)
from certificate_detector import (
    _fetch_certificate_pem, _parse_certificate,
    _verify_trust_chain, _check_domain_match, _check_expiry,
    _openssl_path,
)


def validate_server_certificate(host: str, port: int = 443) -> CertificateInfo | None:
    """Validate a server's certificate using the system trust store.

    Returns a CertificateInfo if the certificate is valid, or None if
    validation fails. Raises CertificateError on connection issues.
    """
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED

        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der_cert = ssock.getpeercert(binary_form=True)
                pem_cert = ssl.DER_cert_to_PEM_cert(der_cert)
                return _parse_certificate(pem_cert)
    except ssl.SSLCertVerificationError:
        # Still try to fetch and parse for diagnostics
        pem = _fetch_certificate_pem(host, port)
        if pem:
            return _parse_certificate(pem)
        return None
    except (ssl.SSLError, OSError, ConnectionRefusedError):
        return None


def validate_with_custom_ca(host: str, port: int, ca_path: str) -> CertificateInfo | None:
    """Validate a server's certificate against a custom CA bundle.

    Used for internal CA validation in LAN scenarios.
    """
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=ca_path)

        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der_cert = ssock.getpeercert(binary_form=True)
                pem_cert = ssl.DER_cert_to_PEM_cert(der_cert)
                return _parse_certificate(pem_cert)
    except (ssl.SSLError, OSError, ConnectionRefusedError, FileNotFoundError):
        return None


def validate_certificate_for_url(server_url: str) -> CertificateInfo | None:
    """Validate certificate for a full server URL."""
    try:
        url = urlsplit(server_url)
        host = url.hostname or ""
        port = url.port or 443
    except Exception:
        return None
    if not host:
        return None
    return validate_server_certificate(host, port)


def validate_certificate_for_url_with_ca(server_url: str, ca_path: str) -> CertificateInfo | None:
    """Validate certificate for a full server URL against a custom CA."""
    try:
        url = urlsplit(server_url)
        host = url.hostname or ""
        port = url.port or 443
    except Exception:
        return None
    if not host:
        return None
    return validate_with_custom_ca(host, port, ca_path)


# ── Quick validation helpers ──────────────────────────────────────────

def is_certificate_valid(info: CertificateInfo, host: str) -> tuple[bool, str]:
    """Quick validation of a parsed certificate against a hostname.

    Returns (is_valid, reason).
    """
    # Check expiry
    expiry_status, expiry_msg = _check_expiry(info)
    if expiry_status:
        return False, expiry_msg

    # Check domain match
    domain_ok, domain_msg = _check_domain_match(host, info)
    if not domain_ok:
        return False, domain_msg

    return True, ""


def certificate_days_remaining(info: CertificateInfo) -> float:
    """Return the number of days until the certificate expires."""
    if not info.not_after:
        return -1
    now = datetime.now(timezone.utc)
    delta = (info.not_after - now).total_seconds() / 86400
    return delta
