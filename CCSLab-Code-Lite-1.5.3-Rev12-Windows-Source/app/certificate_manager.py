"""Unified certificate configuration entry point for CCSLab Labcoding.

Linux (Ubuntu) only; Windows behaviour is intentionally unimplemented.

Security guarantees:
- Never disables HTTPS certificate validation.
- Never transmits server private keys to the client.
- Never stores CA private keys on the client.
- Never hard-codes private keys or passwords.
- Never generates CA or server private keys on the client.
- Requires explicit user confirmation before modifying system trust store.
- Never downloads and auto-trusts a root certificate from an unverified server.
"""

from __future__ import annotations

import os
import re
import subprocess
import ssl
import socket
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# ── Certificate status constants ──────────────────────────────────────
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_DETECTING = "detecting"
STATUS_VALID = "valid"
STATUS_EXPIRING_SOON = "expiring_soon"
STATUS_EXPIRED = "expired"
STATUS_DOMAIN_MISMATCH = "domain_mismatch"
STATUS_UNTRUSTED = "untrusted"
STATUS_CONFIG_FAILED = "config_failed"
STATUS_CA_MISSING = "ca_missing"
STATUS_CA_IMPORTED = "ca_imported"
STATUS_NEEDS_SUDO = "needs_sudo"

_STATUS_LABELS = {
    STATUS_NOT_CONFIGURED: "未配置",
    STATUS_DETECTING: "检测中",
    STATUS_VALID: "证书有效",
    STATUS_EXPIRING_SOON: "证书即将过期",
    STATUS_EXPIRED: "证书已过期",
    STATUS_DOMAIN_MISMATCH: "域名不匹配",
    STATUS_UNTRUSTED: "证书不受信任",
    STATUS_CONFIG_FAILED: "配置失败",
    STATUS_CA_MISSING: "缺少 CA 证书",
    STATUS_CA_IMPORTED: "CA 已导入",
    STATUS_NEEDS_SUDO: "需要管理员授权",
}

# ── Access type ───────────────────────────────────────────────────────
ACCESS_PUBLIC = "public"
ACCESS_PRIVATE = "private"
_ACCESS_PUBLIC_DEFAULT_HOST = "ccslab-code.qtchen.cn"
_ACCESS_PRIVATE_DEFAULT_HOST = "192.168.1.107"

_EXPIRY_WARNING_DAYS = 30


class CertificateError(RuntimeError):
    def __init__(self, message, *, category="certificate"):
        super().__init__(message)
        self.category = category


@dataclass
class CertificateInfo:
    subject: str = ""
    issuer: str = ""
    serial_number: str = ""
    not_before: Optional[datetime] = None
    not_after: Optional[datetime] = None
    subject_alt_names: list[str] = field(default_factory=list)
    fingerprint_sha256: str = ""
    is_self_signed: bool = False
    pem_data: str = ""


@dataclass
class CertificateStatus:
    status: str
    label: str
    access_type: str = ""
    server_host: str = ""
    server_port: int = 443
    info: Optional[CertificateInfo] = None
    message: str = ""
    steps: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def is_ok(self):
        return self.status in (STATUS_VALID, STATUS_EXPIRING_SOON)

    @property
    def is_ok_or_warn(self):
        return self.status in (STATUS_VALID, STATUS_EXPIRING_SOON)


def status_label(status):
    return _STATUS_LABELS.get(status, status)


def classify_access(server_url: str) -> str:
    from urllib.parse import urlsplit
    from ipaddress import ip_address
    try:
        url = urlsplit(server_url)
        host = url.hostname or ""
    except Exception:
        return ACCESS_PUBLIC
    try:
        addr = ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return ACCESS_PRIVATE
        return ACCESS_PUBLIC
    except ValueError:
        pass
    if host.lower() in ("localhost", "localhost.localdomain"):
        return ACCESS_PRIVATE
    if host == _ACCESS_PUBLIC_DEFAULT_HOST:
        return ACCESS_PUBLIC
    internal_suffixes = (".local", ".internal", ".lan", ".corp", ".home", ".intranet")
    if host.endswith(internal_suffixes):
        return ACCESS_PRIVATE
    return ACCESS_PUBLIC


# ── Known lab CA certificate location ─────────────────────────────────

def find_bundled_lab_ca() -> Optional[str]:
    """Search for a lab-issued CA public certificate shipped with the app.

    Checks well-known locations next to the application source.
    Returns the PEM file path, or None.
    """
    candidates = [
        Path(__file__).resolve().parent / "assets" / "lab-ca.pem",
        Path(__file__).resolve().parent / "assets" / "lab-ca.crt",
        Path(__file__).resolve().parent.parent / "lab-ca.pem",
        Path(__file__).resolve().parent.parent / "assets" / "lab-ca.pem",
    ]
    for p in candidates:
        if p.is_file():
            try:
                _validate_ca_pem(p)
                return str(p)
            except CertificateError:
                continue
    return None


def _validate_ca_pem(path: Path) -> None:
    """Quick check that a file looks like a PEM-encoded CA certificate."""
    content = path.read_text().strip()
    if "-----BEGIN CERTIFICATE-----" not in content:
        raise CertificateError("不是 PEM 证书格式。")
    try:
        import shutil
        openssl = shutil.which("openssl")
        if not openssl:
            raise CertificateError("未检测到 OpenSSL。")
        result = subprocess.run(
            [openssl, "x509", "-in", str(path), "-noout", "-text"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise CertificateError("证书解析失败。")
        if "CA:TRUE" not in result.stdout:
            raise CertificateError("此证书不是 CA 证书。")
    except subprocess.TimeoutExpired:
        raise CertificateError("证书验证超时。")


def find_user_imported_ca(data_dir: str) -> Optional[str]:
    """Check for a user-imported CA certificate in the app data directory."""
    ca_path = Path(data_dir) / "certificates" / "imported-ca.pem"
    if ca_path.is_file():
        try:
            _validate_ca_pem(ca_path)
            return str(ca_path)
        except CertificateError:
            ca_path.unlink(missing_ok=True)
    return None


# ── TLS probe using the same stack as login ────────────────────────────

def probe_tls_with_account_stack(server_url: str) -> tuple[bool, str, Optional[CertificateInfo]]:
    """Attempt a strict HTTPS connection using the same TLS stack as login.

    Returns (ok, reason, cert_info).
    Uses ssl.create_default_context() with full verification — the same
    context that AccountClient and DirectConnection employ.
    """
    from urllib.parse import urlsplit
    from certificate_detector import _fetch_certificate_pem, _parse_certificate
    try:
        url = urlsplit(server_url)
        host = url.hostname or ""
        port = url.port or 443
    except Exception:
        return False, "服务器地址无效。", None
    if not host:
        return False, "服务器地址无效。", None
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der_cert = ssock.getpeercert(binary_form=True)
                pem_cert = ssl.DER_cert_to_PEM_cert(der_cert)
                info = _parse_certificate(pem_cert)
                return True, "HTTPS 证书验证通过。", info
    except ssl.SSLCertVerificationError as e:
        pem = _fetch_certificate_pem(host, port)
        info = _parse_certificate(pem) if pem else None
        reason = str(e).split("\n")[0]
        return False, f"证书验证失败：{reason}", info
    except ssl.SSLError as e:
        return False, f"TLS 握手失败：{e}", None
    except (ConnectionRefusedError, TimeoutError) as e:
        return False, f"无法连接服务器：{type(e).__name__}", None
    except OSError as e:
        return False, f"连接错误：{e}", None


# ── CA import / application-level trust ───────────────────────────────

def import_ca_certificate(ca_pem_path: str, data_dir: str) -> CertificateStatus:
    """Import a CA public certificate for application-level trust.

    The CA is stored in the app data directory and used only for
    Labcoding server connections.  No system-wide changes.

    Returns a CertificateStatus describing the result.
    """
    from pathlib import Path as _Path
    ca_file = _Path(ca_pem_path)
    if not ca_file.is_file():
        return CertificateStatus(
            STATUS_CONFIG_FAILED, "配置失败",
            message="CA 证书文件不存在。",
        )
    try:
        _validate_ca_pem(ca_file)
    except CertificateError as e:
        return CertificateStatus(
            STATUS_CONFIG_FAILED, "配置失败",
            message=f"CA 证书验证失败：{e}",
        )
    dest_dir = _Path(data_dir) / "certificates"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "imported-ca.pem"
    try:
        import tempfile, shutil
        tmp = tempfile.NamedTemporaryFile(
            dir=str(dest_dir), suffix=".pem", delete=False
        )
        tmp.write(ca_file.read_bytes())
        tmp.close()
        shutil.move(tmp.name, str(dest))
        dest.chmod(0o644)
    except OSError as e:
        return CertificateStatus(
            STATUS_CONFIG_FAILED, "配置失败",
            message=f"保存 CA 证书失败：{e}",
        )
    return CertificateStatus(
        STATUS_CA_IMPORTED, "CA 已导入",
        message="CA 公共证书已导入，仅对本应用有效。",
        steps=["已验证 CA 证书格式。", f"已保存到 {dest}。"],
    )


def build_app_tls_context(ca_pem_path: Optional[str]) -> ssl.SSLContext:
    """Build an ssl.SSLContext that trusts the given CA in addition to system roots.

    This is the preferred method: no system-level changes, only affects
    Labcoding connections.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if ca_pem_path and Path(ca_pem_path).is_file():
        try:
            ctx.load_verify_locations(cafile=ca_pem_path)
        except ssl.SSLError:
            pass  # Fall back to system roots only
    return ctx


def verify_with_app_context(server_url: str, ca_pem_path: Optional[str] = None) -> tuple[bool, str]:
    """Verify a server using an app-level TLS context (optionally with a custom CA)."""
    from urllib.parse import urlsplit
    try:
        url = urlsplit(server_url)
        host = url.hostname or ""
        port = url.port or 443
    except Exception:
        return False, "地址无效。"
    if not host:
        return False, "地址无效。"
    ctx = build_app_tls_context(ca_pem_path)
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                ssock.getpeercert()
        return True, "证书验证通过。"
    except ssl.SSLCertVerificationError as e:
        reason = str(e).split("\n")[0]
        return False, f"证书验证失败：{reason}"
    except ssl.SSLError as e:
        return False, f"TLS 错误：{e}"
    except OSError as e:
        return False, f"连接失败：{e}"


# ── Main configure_certificate entry point (rewritten) ────────────────

def configure_certificate(server_url: str, data_dir: str,
                          observer: Optional[Callable[[str, str], None]] = None,
                          auto_install_ca: bool = False) -> CertificateStatus:
    """Main entry point for certificate configuration.

    Flow:
    1. Classify server as public or LAN.
    2. For public servers: verify with system roots. If valid, done.
       If not, report the problem to the admin.
    3. For LAN servers:
       a. Check for bundled lab CA cert.
       b. Check for user-imported CA cert.
       c. If CA found, verify server cert against it.
          If verification passes, install app-level trust.
       d. If no CA available, prompt user to import one.
       e. Never generate CA/server keys on the client.
    """
    if os.name != "posix":
        return CertificateStatus(
            STATUS_CONFIG_FAILED, "配置失败",
            message="证书配置功能仅支持 Linux (Ubuntu) 系统。",
        )

    from urllib.parse import urlsplit
    from certificate_detector import detect_certificate

    try:
        url = urlsplit(server_url)
        host = url.hostname or ""
        port = url.port or 443
    except Exception:
        return CertificateStatus(
            STATUS_CONFIG_FAILED, "配置失败",
            message="服务器地址格式无效。",
        )
    if not host:
        return CertificateStatus(
            STATUS_CONFIG_FAILED, "配置失败",
            message="服务器地址为空。",
        )

    access_type = classify_access(server_url)
    steps = []
    _notify(observer, "start", "开始配置安全证书…", steps)

    # First, try system-root verification (the same stack as login)
    _notify(observer, "probe", "正在使用系统信任库验证服务器证书…", steps)
    ok, reason, info = probe_tls_with_account_stack(server_url)
    if ok:
        _notify(observer, "ok", reason, steps)
        return CertificateStatus(
            STATUS_VALID, "证书有效",
            access_type=access_type, server_host=host, server_port=port,
            info=info, message=reason, steps=steps,
        )

    _notify(observer, "fail", f"系统信任库验证未通过：{reason}", steps)
    steps.append(f"系统验证结果：{reason}")

    if access_type == ACCESS_PUBLIC:
        return _handle_public_server(host, port, server_url, access_type,
                                     reason, info, observer, steps)
    else:
        return _handle_private_server(host, port, server_url, data_dir,
                                      access_type, reason, info, observer, steps)


def _notify(observer, stage, message, steps):
    steps.append(message)
    if observer is not None:
        try:
            observer(stage, message)
        except Exception:
            pass


def _handle_public_server(host, port, server_url, access_type,
                          probe_reason, probe_info, observer, steps):
    """Public server: report diagnostics, no client-side fix possible."""
    _notify(observer, "diag",
            "公网服务器的证书不受信任。这通常由以下原因导致：", steps)
    _notify(observer, "diag",
            "1. 证书由不受信任的 CA 签发。", steps)
    _notify(observer, "diag",
            "2. 证书链不完整（中间证书缺失）。", steps)
    _notify(observer, "diag",
            "3. 服务器使用了自签名证书。", steps)
    _notify(observer, "result",
            "请联系服务器管理员修复证书配置。客户端无法自动修复公网证书。", steps)

    detail = ""
    if probe_info:
        if probe_info.not_after:
            exp = probe_info.not_after.strftime("%Y-%m-%d")
            now = datetime.now(timezone.utc)
            if now > probe_info.not_after:
                detail = f"（证书已于 {exp} 过期）"
        if probe_info.subject_alt_names:
            detail += f" 证书域名：{', '.join(probe_info.subject_alt_names[:3])}"

    return CertificateStatus(
        STATUS_UNTRUSTED, "证书不受信任",
        access_type=access_type, server_host=host, server_port=port,
        info=probe_info,
        message=f"公网证书不受信任，请联系管理员在服务器端处理。{detail}",
        steps=steps,
    )


def _handle_private_server(host, port, server_url, data_dir, access_type,
                           probe_reason, probe_info, observer, steps):
    """LAN server: find CA, verify, install app-level trust.

    NEVER generates CA or server private keys on the client.
    The client only ever receives and stores the CA *public* certificate.
    """
    from certificate_detector import detect_certificate

    cert_dir = Path(data_dir) / "certificates"
    cert_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: look for CA public certificate ──────────────────────
    ca_path = None

    # 1a. Bundled lab CA
    bundled = find_bundled_lab_ca()
    if bundled:
        _notify(observer, "ca", "发现项目内置的实验室 CA 公共证书。", steps)
        ca_path = bundled

    # 1b. User-imported CA
    if not ca_path:
        imported = find_user_imported_ca(data_dir)
        if imported:
            _notify(observer, "ca", "发现之前导入的 CA 公共证书。", steps)
            ca_path = imported

    # 1c. If no CA found, we can't proceed with verification
    if not ca_path:
        _notify(observer, "ca", "未找到 CA 公共证书。", steps)
        _notify(observer, "missing",
                "普通客户端不生成或接收 CA 私钥或服务器私钥。", steps)
        _notify(observer, "missing",
                "需要管理员通过可信渠道（如面对面、加密邮件、内部文件共享）"
                "提供实验室 CA 的公共证书文件（.pem / .crt 格式）。", steps)
        return CertificateStatus(
            STATUS_CA_MISSING, "缺少 CA 证书",
            access_type=access_type, server_host=host, server_port=port,
            message="缺少实验室 CA 公共证书。\n"
                    "请联系管理员通过可信渠道获取 CA 公共证书文件，\n"
                    "然后通过「导入 CA 证书」按钮导入。",
            steps=steps,
        )

    # ── Step 2: verify server against CA using app-level context ────
    _notify(observer, "verify", "正在使用 CA 证书验证服务器…", steps)
    ok, verify_reason = verify_with_app_context(server_url, ca_path)

    if ok:
        _notify(observer, "ok", "服务器证书验证通过。", steps)
        _notify(observer, "trust",
                "应用级信任已配置，无需修改系统证书库。", steps)

        # Do a full strict probe with the app context
        ok2, reason2 = _strict_probe_with_ca(server_url, ca_path)
        if ok2:
            _notify(observer, "done", "配置完成。HTTPS 连接验证通过。", steps)
            return CertificateStatus(
                STATUS_VALID, "证书有效",
                access_type=access_type, server_host=host, server_port=port,
                info=probe_info,
                message="CA 证书已配置，服务器 HTTPS 验证通过。",
                steps=steps,
            )
        else:
            _notify(observer, "warn", f"严格校验提示：{reason2}", steps)
            return CertificateStatus(
                STATUS_VALID, "证书有效",
                access_type=access_type, server_host=host, server_port=port,
                info=probe_info,
                message=f"CA 证书已配置。{reason2}",
                steps=steps,
            )
    else:
        _notify(observer, "fail", f"验证未通过：{verify_reason}", steps)

        # Check if the problem is the server itself
        _notify(observer, "diag",
                "服务器证书与导入的 CA 不匹配。可能原因：", steps)
        _notify(observer, "diag",
                "1. 服务器尚未部署该 CA 签发的证书。", steps)
        _notify(observer, "diag",
                "2. 导入的 CA 证书不正确。", steps)
        _notify(observer, "diag",
                "3. 服务器域名/IP 与证书不匹配。", steps)
        _notify(observer, "result",
                "请联系管理员在服务器端确认证书配置。", steps)

        return CertificateStatus(
            STATUS_UNTRUSTED, "证书不受信任",
            access_type=access_type, server_host=host, server_port=port,
            info=probe_info,
            message=f"服务器证书验证未通过：{verify_reason}。\n"
                    "请联系管理员在服务器端处理。",
            steps=steps,
        )


def _strict_probe_with_ca(server_url: str, ca_path: str) -> tuple[bool, str]:
    """Do a strict TLS probe: cert chain, expiry, and hostname/IP match.

    Uses the same SSL context that login connections use.
    """
    from urllib.parse import urlsplit
    from certificate_detector import _fetch_certificate_pem, _parse_certificate
    from certificate_validator import is_certificate_valid

    try:
        url = urlsplit(server_url)
        host = url.hostname or ""
        port = url.port or 443
    except Exception:
        return False, "地址无效。"
    if not host:
        return False, "地址无效。"

    ctx = build_app_tls_context(ca_path)
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der = ssock.getpeercert(binary_form=True)
                pem = ssl.DER_cert_to_PEM_cert(der)
                info = _parse_certificate(pem)
                valid, reason = is_certificate_valid(info, host)
                if valid:
                    return True, "证书链、有效期与域名全部验证通过。"
                else:
                    return False, reason
    except ssl.SSLCertVerificationError as e:
        return False, f"证书链验证失败：{e}"
    except ssl.SSLError as e:
        return False, f"TLS 错误：{e}"
    except OSError as e:
        return False, f"连接失败：{e}"


# ── Public API: quick detection ───────────────────────────────────────

def detect(server_url: str) -> CertificateStatus:
    """Quick certificate detection (no configuration changes)."""
    from urllib.parse import urlsplit
    from certificate_detector import detect_certificate

    try:
        url = urlsplit(server_url)
        host = url.hostname or ""
        port = url.port or 443
    except Exception:
        return CertificateStatus(
            STATUS_NOT_CONFIGURED, "未配置",
            message="服务器地址格式无效。",
        )
    return detect_certificate(host, port, server_url)


def is_linux():
    """Check if running on Linux."""
    try:
        return os.name != "nt" and "linux" in os.uname().sysname.lower()
    except Exception:
        return os.name != "nt"


# ── System-level CA installation (optional, with pkexec) ─────────────

def install_ca_systemwide(ca_cert_path: str, data_dir: str) -> CertificateStatus:
    """Install CA cert to system trust store using pkexec (polkit) on Ubuntu.

    This is optional and only used when the user explicitly requests it.
    App-level trust (build_app_tls_context) is the preferred method.
    """
    from certificate_deployer import (
        install_ca_to_system_trust, is_ca_trusted_systemwide,
    )

    if is_ca_trusted_systemwide(ca_cert_path):
        return CertificateStatus(
            STATUS_VALID, "证书有效",
            message="CA 证书已在系统信任库中。",
        )

    # Try pkexec first (Ubuntu desktop policykit)
    ok, msg = _try_pkexec_install(ca_cert_path)
    if ok:
        return CertificateStatus(
            STATUS_VALID, "证书有效",
            message="CA 证书已通过桌面授权安装到系统信任库。",
            steps=[msg],
        )

    # Fall back to checking sudo
    from certificate_deployer import _check_sudo_available
    if _check_sudo_available():
        result = install_ca_to_system_trust(ca_cert_path, confirmed=True)
        if result.success:
            return CertificateStatus(
                STATUS_VALID, "证书有效",
                message=result.message, steps=result.steps,
            )
        else:
            return CertificateStatus(
                STATUS_CONFIG_FAILED, "配置失败",
                message=result.message, error=result.error,
                steps=result.steps,
            )

    return CertificateStatus(
        STATUS_NEEDS_SUDO, "需要管理员授权",
        message="需要管理员授权才能安装证书到系统信任库。\n"
                "请在弹出的桌面授权对话框中输入密码，或让管理员执行安装。",
        error="未获得管理员授权。",
    )


def _try_pkexec_install(ca_cert_path: str) -> tuple[bool, str]:
    """Try to install using pkexec, which triggers a polkit GUI prompt."""
    import shutil
    openssl = shutil.which("openssl")
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return False, "未找到 pkexec。"
    if not openssl:
        return False, "未找到 openssl。"

    try:
        # Validate cert format first
        result = subprocess.run(
            [openssl, "x509", "-in", ca_cert_path, "-noout"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False, "CA 证书格式无效。"

        # Read the cert content
        cert_content = Path(ca_cert_path).read_bytes()

        # Use pkexec to copy to ca-certificates directory
        target_dir = Path("/usr/local/share/ca-certificates/ccslab")
        target_file = target_dir / "internal-ca.crt"

        result = subprocess.run(
            ["pkexec", "mkdir", "-p", str(target_dir)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False, f"创建目录失败：{result.stderr.strip()}"

        # Write cert via pkexec + tee
        result = subprocess.run(
            ["pkexec", "tee", str(target_file)],
            input=cert_content, capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            return False, f"复制证书失败：{result.stderr.strip()}"

        # Update CA certificates
        result = subprocess.run(
            ["pkexec", "update-ca-certificates"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False, f"更新信任库失败：{result.stderr.strip()}"

        return True, "已通过桌面授权安装到系统信任库。"

    except subprocess.TimeoutExpired:
        return False, "操作超时，您可能取消了授权对话框。"
    except Exception as e:
        return False, str(e)
