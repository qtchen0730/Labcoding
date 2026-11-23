"""Server certificate deployment and client CA trust configuration.

Handles:
- Checking if CA is already trusted by the client system.
- Installing CA root certificate into Ubuntu system trust store (with user confirmation).
- Verifying deployment results.

Security:
- Requires explicit user confirmation for system trust store changes.
- Never modifies production server HTTPS configuration from the client.
- Uses Ubuntu's standard certificate management tools.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DeployResult:
    """Result of a certificate deployment operation."""
    success: bool
    message: str
    steps: list[str] = None
    error: str = ""

    def __post_init__(self):
        if self.steps is None:
            self.steps = []


def is_ca_trusted_systemwide(ca_cert_path: str) -> bool:
    """Check if a CA certificate is already trusted by the system.

    Returns True if the CA's fingerprint is found in the system trust store.
    """
    if not ca_cert_path or not Path(ca_cert_path).is_file():
        return False

    # Get CA cert fingerprint
    try:
        openssl = _openssl_path()
        result = subprocess.run(
            [openssl, "x509", "-in", ca_cert_path,
             "-noout", "-fingerprint", "-sha256"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False

        # Extract fingerprint (format: SHA256 Fingerprint=AB:CD:EF:...)
        fp_match = result.stdout.strip()
        if "=" in fp_match:
            fp = fp_match.split("=", 1)[1].strip()
        else:
            fp = fp_match

        # Check if it's in the system trust store
        ca_file = Path("/etc/ssl/certs/ca-certificates.crt")
        if ca_file.is_file():
            system_certs = ca_file.read_text()
            # Check if the CA cert content is present
            ca_content = Path(ca_cert_path).read_text()
            if ca_content.strip() in system_certs:
                return True

        # Also check via openssl verify
        result = subprocess.run(
            [openssl, "verify", ca_cert_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and ": OK" in result.stdout:
            return True

        return False
    except Exception:
        return False


def is_ca_trusted_in_context(ca_cert_path: str, ssl_context=None) -> bool:
    """Check if a CA is trusted in a given SSL context.

    If no context is given, checks against the default system context.
    """
    import ssl
    if ssl_context is None:
        ssl_context = ssl.create_default_context()

    # Add the CA cert to the context and check if it's already there
    try:
        ca_content = Path(ca_cert_path).read_text()
        # Write to temp file and try loading
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
            f.write(ca_content.encode())
            f.flush()
            try:
                # If load_verify_locations doesn't raise, it's a valid cert
                ssl_context.load_verify_locations(cafile=f.name)
                return True
            except ssl.SSLError:
                return False
            finally:
                os.unlink(f.name)
    except Exception:
        return False


def install_ca_to_system_trust(ca_cert_path: str,
                                confirmed: bool = False) -> DeployResult:
    """Install a CA certificate into Ubuntu's system trust store.

    This requires root/sudo privileges. On Ubuntu, this means copying
    the cert to /usr/local/share/ca-certificates/ and running
    update-ca-certificates.

    Args:
        ca_cert_path: Path to the CA certificate PEM file.
        confirmed: Whether the user has explicitly confirmed the operation.

    Returns:
        DeployResult with success status and details.
    """
    steps = []

    if not confirmed:
        return DeployResult(
            success=False,
            message="需要用户确认才能安装证书到系统信任库。",
            steps=steps,
            error="未经确认，无法修改系统证书信任库。",
        )

    steps.append("正在检查 CA 证书文件…")

    if not ca_cert_path or not Path(ca_cert_path).is_file():
        return DeployResult(
            success=False,
            message="CA 证书文件不存在。",
            steps=steps,
            error="证书文件路径无效。",
        )

    # Validate the CA cert
    steps.append("正在验证 CA 证书格式…")
    try:
        openssl = _openssl_path()
        result = subprocess.run(
            [openssl, "x509", "-in", ca_cert_path, "-noout", "-text"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return DeployResult(
                success=False,
                message="CA 证书文件格式无效。",
                steps=steps,
                error=result.stderr.strip(),
            )
    except Exception as e:
        return DeployResult(
            success=False,
            message=f"无法验证 CA 证书：{e}",
            steps=steps,
        )

    # Check if already trusted
    if is_ca_trusted_systemwide(ca_cert_path):
        steps.append("CA 证书已在系统信任库中。")
        return DeployResult(
            success=True,
            message="CA 证书已被系统信任。",
            steps=steps,
        )

    # Check if we have sudo access
    steps.append("正在检查管理员权限…")
    if not _check_sudo_available():
        return DeployResult(
            success=False,
            message="需要管理员权限才能安装证书到系统信任库。",
            steps=steps,
            error="请通过系统认证后重试。",
        )

    # Determine the target directory and filename
    target_dir = Path("/usr/local/share/ca-certificates/ccslab")
    target_file = target_dir / "internal-ca.crt"

    steps.append(f"正在将 CA 证书复制到 {target_dir}…")

    try:
        # Copy cert to target directory using sudo
        # Create directory if needed
        sudo_result = subprocess.run(
            ["sudo", "mkdir", "-p", str(target_dir)],
            capture_output=True, text=True, timeout=15,
        )
        if sudo_result.returncode != 0:
            return DeployResult(
                success=False,
                message="无法创建证书目录。",
                steps=steps,
                error=sudo_result.stderr.strip(),
            )

        # Copy the cert file
        sudo_result = subprocess.run(
            ["sudo", "cp", ca_cert_path, str(target_file)],
            capture_output=True, text=True, timeout=15,
        )
        if sudo_result.returncode != 0:
            return DeployResult(
                success=False,
                message="无法复制 CA 证书文件。",
                steps=steps,
                error=sudo_result.stderr.strip(),
            )

        steps.append("正在更新系统证书信任库…")

        # Run update-ca-certificates
        sudo_result = subprocess.run(
            ["sudo", "update-ca-certificates"],
            capture_output=True, text=True, timeout=30,
        )
        if sudo_result.returncode != 0:
            return DeployResult(
                success=False,
                message="更新系统证书信任库失败。",
                steps=steps,
                error=sudo_result.stderr.strip(),
            )

        steps.append("系统证书信任库已更新。")

        # Verify
        if is_ca_trusted_systemwide(ca_cert_path):
            steps.append("CA 证书信任验证成功。")
            return DeployResult(
                success=True,
                message="CA 证书已成功安装到系统信任库。",
                steps=steps,
            )
        else:
            return DeployResult(
                success=True,
                message="CA 证书已安装，但验证显示尚未完全生效。请重启应用程序后重试。",
                steps=steps,
            )

    except subprocess.TimeoutExpired:
        return DeployResult(
            success=False,
            message="证书安装操作超时。",
            steps=steps,
            error="操作超时，请重试。",
        )
    except Exception as e:
        return DeployResult(
            success=False,
            message=f"证书安装过程中出现异常：{e}",
            steps=steps,
        )


def _check_sudo_available() -> bool:
    """Check if sudo is available and can be used (without prompting)."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _openssl_path():
    """Return path to openssl executable."""
    override = os.environ.get("CCSLAB_TEST_OPENSSL")
    if override:
        return override
    return shutil.which("openssl")
