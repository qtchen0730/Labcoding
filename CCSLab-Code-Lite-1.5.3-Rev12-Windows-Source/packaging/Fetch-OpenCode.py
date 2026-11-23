"""Download the pinned OpenCode release into .work/vendor."""
import base64
import hashlib
import io
import os
import stat
import sys
from pathlib import Path
import tarfile
import urllib.request


VERSION = "1.17.16"
LICENSE = f"https://raw.githubusercontent.com/anomalyco/opencode/v{VERSION}/LICENSE"

_PACKAGES = {
    "windows": {
        "name": "opencode-windows-x64-baseline",
        "binary": "package/bin/opencode.exe",
        "integrity": (
            "Sg/CkgjoP4Rx9zryJNc7PMG/3cyugLmSbge3kdHqoXwkAiQQzA42ZTRHA904dL0avwma"
            "FWr1d+hb3Mjn8uBqYg=="
        ),
    },
    "linux": {
        "name": "opencode-linux-x64-baseline",
        "binary": "package/bin/opencode",
        "integrity": (
            "bE1USSZwsPWEoe7MVzTL4VTQcKg5RrLH7PnNmgXD0kyhm/gLxZytzWQBsTSd0LKKyHg9uNYSLy"
            "J0GAcP6VLyuQ=="
        ),
    },
}


def _platform_key():
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "linux":
        return "linux"
    raise SystemExit(f"Unsupported platform for OpenCode fetch: {sys.platform}")


def download(url):
    with urllib.request.urlopen(url, timeout=75) as response:
        return response.read()


def main():
    key = _platform_key()
    pkg = _PACKAGES[key]
    tarball_url = f"https://registry.npmjs.org/{pkg['name']}/-/{pkg['name']}-{VERSION}.tgz"
    binary_path = pkg["binary"]
    integrity_b64 = pkg["integrity"]

    vendor = Path(__file__).resolve().parents[2] / ".work" / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)

    if key == "windows":
        dest = vendor / "opencode.exe"
    else:
        dest = vendor / "opencode"

    # Skip download if already valid
    if dest.is_file():
        current_sha = dest.read_bytes()
        if key == "windows":
            print(f"OpenCode {VERSION} already present / {hashlib.sha256(current_sha).hexdigest()}")
        else:
            print(f"OpenCode {VERSION} already present / {hashlib.sha256(current_sha).hexdigest()}")
        return

    data = download(tarball_url)
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if actual != integrity_b64:
        raise ValueError(
            f"OpenCode package failed SHA-512 verification\n"
            f"  expected: {integrity_b64}\n"
            f"  actual:   {actual}"
        )

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        with archive.extractfile(binary_path) as binary:
            executable = binary.read()

    license_text = download(LICENSE)
    if not license_text.startswith(b"MIT License"):
        raise ValueError("Unexpected OpenCode license response")

    dest.write_bytes(executable)
    if key != "windows":
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    (vendor / "LICENSE-OpenCode.txt").write_bytes(license_text)
    print(f"OpenCode {VERSION} / {hashlib.sha256(executable).hexdigest()}")


if __name__ == "__main__":
    main()
