"""Download the pinned OpenCode Windows AMD64 release into app/vendor."""
import base64
import hashlib
import io
from pathlib import Path
import tarfile
import urllib.request


VERSION = "1.17.16"
PACKAGE = "opencode-windows-x64-baseline"
TARBALL = f"https://registry.npmjs.org/{PACKAGE}/-/{PACKAGE}-{VERSION}.tgz"
INTEGRITY = (
    "Sg/CkgjoP4Rx9zryJNc7PMG/3cyugLmSbge3kdHqoXwkAiQQzA42ZTRHA904dL0avwma"
    "FWr1d+hb3Mjn8uBqYg=="
)
LICENSE = f"https://raw.githubusercontent.com/anomalyco/opencode/v{VERSION}/LICENSE"


def download(url):
    with urllib.request.urlopen(url, timeout=75) as response:
        return response.read()


def main():
    data = download(TARBALL)
    if base64.b64encode(hashlib.sha512(data).digest()).decode("ascii") != INTEGRITY:
        raise ValueError("OpenCode package failed SHA-512 verification")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        with archive.extractfile("package/bin/opencode.exe") as binary:
            executable = binary.read()
    license_text = download(LICENSE)
    if not license_text.startswith(b"MIT License"):
        raise ValueError("Unexpected OpenCode license response")
    vendor = Path(__file__).resolve().parent.parent / "app" / "vendor"
    vendor.mkdir(exist_ok=True)
    (vendor / "opencode.exe").write_bytes(executable)
    (vendor / "LICENSE-OpenCode.txt").write_bytes(license_text)
    print(f"OpenCode {VERSION} / {hashlib.sha256(executable).hexdigest()}")


if __name__ == "__main__":
    main()
