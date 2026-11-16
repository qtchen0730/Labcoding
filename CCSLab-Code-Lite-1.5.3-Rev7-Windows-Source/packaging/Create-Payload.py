"""Create the installer payload from the frozen program directory."""
import json
from pathlib import Path
import shutil
import sys
import zipfile


def copy_conda_licenses(directory):
    """Collect runtime license files from the active conda distribution, when present."""
    metadata = Path(sys.base_prefix) / "conda-meta"
    license_directory = directory / "vendor" / "licenses"
    for package in ("tk", "openssl", "bzip2", "libffi", "xz", "zlib"):
        for record in metadata.glob(f"{package}-[0-9]*.json"):
            data = json.loads(record.read_text(encoding="utf-8"))
            source = data.get("link", {}).get("source")
            if not source:
                continue
            root = Path(source) / "info" / "licenses"
            for item in root.rglob("*"):
                if item.is_file():
                    destination = license_directory / package / item.relative_to(root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)


def main():
    directory, destination = map(Path, sys.argv[1:])
    copy_conda_licenses(directory)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for item in sorted(directory.rglob("*")):
            if item.is_file():
                bundle.write(item, item.relative_to(directory).as_posix())


if __name__ == "__main__":
    main()
