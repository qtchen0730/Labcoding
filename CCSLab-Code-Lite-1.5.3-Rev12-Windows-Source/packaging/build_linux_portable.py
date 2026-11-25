#!/usr/bin/env python3
"""Build a Linux x86_64 portable directory for Labcoding.

Usage:
    python3 packaging/build_linux_portable.py [--output DIR]
"""
import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
APP = SRC / "app"
WORK = SRC.parent / ".work"
VENDOR = WORK / "vendor"


def ensure_opencode():
    """Make sure the Linux OpenCode runtime binary is present."""
    target = VENDOR / "opencode"
    if not target.is_file():
        print("Fetching Linux OpenCode runtime...")
        fetch = SRC / "packaging" / "Fetch-OpenCode.py"
        subprocess.check_call([sys.executable, str(fetch)])
    else:
        print(f"OpenCode runtime present: {target.stat().st_size} bytes")


def run_pyinstaller(output_dir):
    """Run PyInstaller to produce the portable directory."""
    spec = SRC / "packaging" / "Labcoding-Linux.spec"
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         str(spec),
         "--distpath", str(output_dir),
         "--workpath", str(WORK / "pyinstaller-cache"),
         "--noconfirm"],
        cwd=str(SRC.parent),
    )
    if result.returncode != 0:
        sys.exit(result.returncode)


def add_opencode_to_vendor(build_dir):
    """Copy OpenCode runtime into the built app's vendor directory.

    In a PyInstaller --onedir build, runtime data lives under _internal/.
    The frozen app resolves paths relative to sys.executable's parent,
    then into _internal/. Place the binary in both locations so the
    _binary() lookup in engine.py finds it in both frozen and dev modes.
    """
    src_binary = VENDOR / "opencode"
    if not src_binary.is_file():
        raise SystemExit("OpenCode binary missing after fetch")

    # Primary location: _internal/app/vendor/ (PyInstaller runtime)
    dest_vendor = build_dir / "_internal" / "app" / "vendor"
    dest_vendor.mkdir(parents=True, exist_ok=True)
    dest_binary = dest_vendor / "opencode"
    shutil.copy2(str(src_binary), str(dest_binary))
    dest_binary.chmod(dest_binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"OpenCode runtime placed (internal): {dest_binary} ({dest_binary.stat().st_size} bytes)")

    # Fallback location: app/vendor/ (for direct invocation)
    dest_vendor2 = build_dir / "app" / "vendor"
    dest_vendor2.mkdir(parents=True, exist_ok=True)
    dest_binary2 = dest_vendor2 / "opencode"
    shutil.copy2(str(src_binary), str(dest_binary2))
    dest_binary2.chmod(dest_binary2.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def create_launcher(build_dir):
    """Create a shell-script launcher that runs the frozen app."""
    launcher = build_dir / "labcoding"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'export FONTCONFIG_FILE="$SCRIPT_DIR/linux-fonts.conf"\n'
        'SYSTEM_XFT=/usr/lib/x86_64-linux-gnu/libXft.so.2\n'
        'if [[ -r "$SYSTEM_XFT" ]]; then\n'
        '    export LD_PRELOAD="$SYSTEM_XFT${LD_PRELOAD:+:$LD_PRELOAD}"\n'
        'fi\n'
        "exec \"$SCRIPT_DIR/Labcoding\" \"$@\"\n"
    )
    shutil.copy2(Path(__file__).with_name("linux-fonts.conf"), build_dir / "linux-fonts.conf")
    (build_dir / "linux-fonts.conf").chmod(0o644)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def add_readme(build_dir):
    readme = build_dir / "README.txt"
    version = json.loads((APP / "version.json").read_text())["version"]
    readme.write_text(
        f"CCSLab Code Lite — Labcoding v{version}\n"
        f"Ubuntu 24.04 x86_64 Portable Build\n"
        f"\n"
        f"Launch:\n"
        f"  ./labcoding\n"
        f"\n"
        f"User data is stored in ~/.config/ccslab-labcoding by default.\n"
    )


def clean_artifacts(build_dir):
    """Remove developer-only artifacts from the portable build."""
    for pattern in ("__pycache__", "*.pyc", "*.pyo", "verify_*.py"):
        for item in build_dir.rglob(pattern):
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=SRC.parent / "dist" / "Labcoding-Linux-x64-portable")
    args = parser.parse_args()

    output_dir = args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=== Step 1: Ensure OpenCode runtime ===")
    ensure_opencode()

    print("\n=== Step 2: PyInstaller build ===")
    run_pyinstaller(output_dir)

    # Find the built directory (PyInstaller creates Labcoding-Linux-x64/)
    build_candidates = list(output_dir.glob("Labcoding-Linux-x64*"))
    if not build_candidates:
        sys.exit("ERROR: PyInstaller did not produce a build directory")
    build_dir = build_candidates[0]
    print(f"Build directory: {build_dir}")

    print("\n=== Step 3: Add OpenCode runtime to vendor ===")
    add_opencode_to_vendor(build_dir)

    print("\n=== Step 4: Create launcher ===")
    create_launcher(build_dir)

    print("\n=== Step 5: Add README ===")
    add_readme(build_dir)

    print("\n=== Step 6: Clean developer artifacts ===")
    clean_artifacts(build_dir)

    # Report
    total = sum(f.stat().st_size for f in build_dir.rglob("*") if f.is_file())
    file_count = sum(1 for f in build_dir.rglob("*") if f.is_file())
    print(f"\n=== Portable build complete ===")
    print(f"  Directory: {build_dir}")
    print(f"  Files: {file_count}")
    print(f"  Total size: {total / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
