"""Certificate status detection via OpenSSL CLI and Python ssl module.

Detects certificate validity, expiry, SAN, trust chain and more without
modifying any configuration.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from certificate_manager import (
    CertificateError, CertificateInfo, CertificateStatus,
    ACCESS_PUBLIC, ACCESS_PRIVATE,
    STATUS_NOT_CONFIGURED, STATUS_DETECTING, STATUS_VALID,
    STATUS_EXPIRING_SOON, STATUS_EXPIRED, STATUS_DOMAIN_MISMATCH,
    STATUS_UNTRUSTED, STATUS_CONFIG_FAILED,
    _EXPIRY_WARNING_DAYS, classify_access,
)


def _openssl_path():
    """Return path to openssl executable, or None."""
    override = os.environ.get("CCSLAB_TEST_OPENSSL")
    if override:
        return override
    return shutil.which("openssl")


def _parse_date(text):
    """Parse OpenSSL date string like 'Nov 23 12:00:00 2026 GMT'."""
    text = text.strip()
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b  %d %H:%M:%S %Y %Z"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _run_openssl(args, *, timeout=15):
    """Run an openssl command and return stdout."""
    openssl = _openssl_path()
    if not openssl:
        raise CertificateError("未检测到 OpenSSL，无法检查证书。")
    try:
        result = subprocess.run(
            [openssl] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout, result.stderr, result.returncode
    except FileNotFoundError:
        raise CertificateError(f"OpenSSL 未找到：{openssl}")
    except subprocess.TimeoutExpired:
        raise CertificateError("OpenSSL 命令执行超时。")


def detect_certificate(host: str, port: int = 443,
                       server_url: str = "") -> CertificateStatus:
    """Detect the certificate status of a server without modifying anything.

    Returns a CertificateStatus with full diagnostic details.
    """
    steps = []
    steps.append("正在检测服务器证书状态…")

    if os.name != "nt":
        pass  # Linux path

    try:
        # Step 1: connect and fetch certificate
        steps.append("正在连接服务器并获取证书…")
        cert_pem = _fetch_certificate_pem(host, port)
        if not cert_pem:
            return CertificateStatus(
                STATUS_NOT_CONFIGURED, "未配置",
                access_type=classify_access(server_url or f"https://{host}:{port}"),
                server_host=host, server_port=port,
                message="无法连接到服务器，请检查网络与服务器地址。",
                steps=steps,
            )

        # Step 2: parse certificate info
        steps.append("正在解析证书信息…")
        info = _parse_certificate(cert_pem)

        # Step 3: verify trust chain
        steps.append("正在验证证书信任链…")
        trust_ok, trust_error = _verify_trust_chain(host, port)

        # Step 4: check domain match
        steps.append("正在检查域名匹配…")
        domain_ok, domain_error = _check_domain_match(host, info)

        # Step 5: check expiry
        steps.append("正在检查证书有效期…")
        expiry_status, expiry_error = _check_expiry(info)

        # Determine overall status
        if not trust_ok:
            return CertificateStatus(
                STATUS_UNTRUSTED, "证书不受信任",
                access_type=classify_access(server_url or f"https://{host}:{port}"),
                server_host=host, server_port=port,
                info=info, message=trust_error or "证书不受信任。",
                steps=steps,
            )
        if not domain_ok:
            return CertificateStatus(
                STATUS_DOMAIN_MISMATCH, "域名不匹配",
                access_type=classify_access(server_url or f"https://{host}:{port}"),
                server_host=host, server_port=port,
                info=info, message=domain_error,
                steps=steps,
            )
        if expiry_status:
            return CertificateStatus(
                expiry_status, status_label(expiry_status),
                access_type=classify_access(server_url or f"https://{host}:{port}"),
                server_host=host, server_port=port,
                info=info, message=expiry_error,
                steps=steps,
            )

        # All checks passed
        steps.append("证书验证通过。")
        return CertificateStatus(
            STATUS_VALID, "证书有效",
            access_type=classify_access(server_url or f"https://{host}:{port}"),
            server_host=host, server_port=port,
            info=info, message="证书链、域名和有效期验证通过。",
            steps=steps,
        )

    except CertificateError as e:
        return CertificateStatus(
            STATUS_NOT_CONFIGURED, "未配置",
            access_type=classify_access(server_url or f"https://{host}:{port}"),
            server_host=host, server_port=port,
            message=str(e),
            steps=steps,
        )
    except Exception as e:
        return CertificateStatus(
            STATUS_NOT_CONFIGURED, "未配置",
            access_type=classify_access(server_url or f"https://{host}:{port}"),
            server_host=host, server_port=port,
            message=f"检测异常：{e}",
            steps=steps,
        )


def _fetch_certificate_pem(host: str, port: int) -> str:
    """Fetch server certificate via OpenSSL s_client and return PEM text."""
    out, err, rc = _run_openssl([
        "s_client", "-connect", f"{host}:{port}",
        "-servername", host,
    ], timeout=10)
    # s_client may return non-zero even with a valid connection when cert
    # verification fails. We still try to extract the certificate.

    # Extract the first cert block from combined output (some openssl versions
    # write certs to stdout, others to stderr)
    combined = out + err
    pem_match = re.search(
        r"(-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----)",
        combined, re.DOTALL
    )
    return pem_match.group(1) if pem_match else ""


def _parse_certificate(cert_pem: str) -> CertificateInfo:
    """Parse a PEM certificate into a CertificateInfo object using OpenSSL."""
    openssl = _openssl_path()
    if not openssl:
        raise CertificateError("未检测到 OpenSSL，无法解析证书。")
    import tempfile as _tf
    with _tf.NamedTemporaryFile(suffix=".pem", delete=False, mode="w", encoding="ascii") as _tmp:
        _tmp.write(cert_pem)
        _tmp_path = _tmp.name
    try:
        result = subprocess.run(
            [openssl, "x509", "-in", _tmp_path, "-noout",
             "-subject", "-issuer", "-serial",
             "-dates", "-fingerprint", "-sha256",
             "-ext", "subjectAltName"],
            capture_output=True, text=True, timeout=15,
        )
        out, err, rc = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        raise CertificateError("OpenSSL 命令执行超时。")
    finally:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass

    subject = _extract_field(out, "subject=")
    issuer = _extract_field(out, "issuer=")
    serial = _extract_field(out, "serial=")
    not_before = _extract_date(out, "notBefore=")
    not_after = _extract_date(out, "notAfter=")
    raw_fp = _extract_field(out, "SHA256 Fingerprint=")
    if not raw_fp:
        raw_fp = _extract_field(out, "sha256 Fingerprint=")
    fingerprint = raw_fp.replace("=", ":", 1).strip()

    # Check self-signed
    is_self_signed = subject.strip() == issuer.strip()

    # Parse SANs
    sans = _extract_sans(out)

    return CertificateInfo(
        subject=subject, issuer=issuer, serial_number=serial,
        not_before=not_before, not_after=not_after,
        subject_alt_names=sans, fingerprint_sha256=fingerprint,
        is_self_signed=is_self_signed, pem_data=cert_pem,
    )


def _extract_field(text: str, prefix: str) -> str:
    """Extract a field value from OpenSSL text output."""
    for line in text.split("\n"):
        if prefix in line:
            idx = line.index(prefix) + len(prefix)
            return line[idx:].strip()
    return ""


def _extract_date(text: str, prefix: str) -> datetime | None:
    """Extract a date field from OpenSSL text output."""
    raw = _extract_field(text, prefix)
    if raw:
        return _parse_date(raw)
    return None


def _extract_sans(text: str) -> list[str]:
    """Extract Subject Alternative Names from OpenSSL text output."""
    sans = []
    in_san = False
    for line in text.split("\n"):
        stripped = line.strip()
        if "X509v3 Subject Alternative Name" in stripped:
            in_san = True
            continue
        if in_san:
            in_san = False
            for entry in stripped.split(","):
                entry = entry.strip()
                if entry.startswith("DNS:") or entry.startswith("IP Address:"):
                    sans.append(entry.split(":", 1)[1].strip())
    return sans


def _verify_trust_chain(host: str, port: int) -> tuple[bool, str]:
    """Verify the server's certificate trust chain via OpenSSL s_client."""
    out, err, rc = _run_openssl([
        "s_client", "-connect", f"{host}:{port}",
        "-verify", "2",
        "-servername", host,
    ], timeout=10)

    # Check verify return code in the output
    verify_match = re.search(r"Verify return code:\s*(\d+)\s*\((.+?)\)", err + out)
    if verify_match:
        code = int(verify_match.group(1))
        reason = verify_match.group(2)
        if code == 0:
            return True, ""
        return False, f"证书验证失败（代码 {code}：{reason}）"

    # Check for common verification failure patterns
    if "verify error" in (err + out).lower():
        return False, "证书链验证出错。"

    if rc != 0:
        # Connection-level failure
        return False, "无法完成证书链验证。"

    # If we got here without explicit verification, assume OK
    return True, ""


def _check_domain_match(host: str, info: CertificateInfo) -> tuple[bool, str]:
    """Check whether the server hostname matches the certificate."""
    host_lower = host.lower()

    # Check SANs first (RFC 6125)
    for san in info.subject_alt_names:
        san_lower = san.lower()
        if san_lower == host_lower:
            return True, ""
        # Wildcard matching
        if san_lower.startswith("*."):
            wildcard_domain = san_lower[2:]
            if host_lower.endswith("." + wildcard_domain):
                # Wildcard must match exactly one label
                if host_lower.count(".") == wildcard_domain.count(".") + 1:
                    return True, ""

    # Fallback to subject CN
    cn_match = re.search(r"CN\s*=\s*([^/,]+)", info.subject)
    if cn_match:
        cn = cn_match.group(1).strip().lower()
        if cn == host_lower:
            return True, ""
        if cn.startswith("*."):
            wildcard_domain = cn[2:]
            if host_lower.endswith("." + wildcard_domain):
                if host_lower.count(".") == wildcard_domain.count(".") + 1:
                    return True, ""

    return False, f"服务器证书不包含匹配 {host} 的域名。"


def _check_expiry(info: CertificateInfo) -> tuple[str | None, str]:
    """Check if a certificate is expired or expiring soon.

    Returns (status_code_or_None, message). None means no expiry issue.
    """
    now = datetime.now(timezone.utc)

    if info.not_after:
        delta = (info.not_after - now).total_seconds() / 86400  # days
        if delta < 0:
            return STATUS_EXPIRED, f"证书已过期（{info.not_after.strftime('%Y-%m-%d')}）"
        if delta <= _EXPIRY_WARNING_DAYS:
            return STATUS_EXPIRING_SOON, f"证书将于 {delta:.0f} 天后过期"

    if info.not_before:
        if now < info.not_before:
            return STATUS_NOT_CONFIGURED, f"证书尚未生效（{info.not_before.strftime('%Y-%m-%d')}）"

    return None, ""


def status_label(status):
    """Human-readable label for a status code."""
    from certificate_manager import status_label as _label
    return _label(status)
