#!/usr/bin/env bash
# Build Ubuntu .deb package for Labcoding.
# Usage: bash packaging/build_deb.sh [OUTPUT_DIR]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC_DIR="$REPO_ROOT/CCSLab-Code-Lite-1.5.3-Rev12-Windows-Source"
DIST_DIR="${1:-$REPO_ROOT/dist}"
WORK_DIR="$REPO_ROOT/.work/deb-build"

# Read version
APP_VERSION=$(python3 -c "import json; print(json.load(open('$SRC_DIR/app/version.json'))['version'])")
OPENCODE_VERSION="1.17.16"

echo "=== Building Labcoding $APP_VERSION .deb package ==="
echo "  Source: $SRC_DIR"
echo "  Output: $DIST_DIR"

# Clean previous build
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

# Step 1: Build portable version first (if not already present)
PORTABLE="$DIST_DIR/Labcoding-Linux-x64-portable/Labcoding-Linux-x64"
if [ ! -f "$PORTABLE/Labcoding" ]; then
    echo ""
    echo "=== Step 1: Building portable version ==="
    python3 "$SRC_DIR/packaging/build_linux_portable.py" --output "$DIST_DIR/Labcoding-Linux-x64-portable"
else
    echo "=== Step 1: Portable version already built ==="
    echo "  $PORTABLE"
fi

# Step 2: Prepare DEB structure
DEB_ROOT="$WORK_DIR/labcoding-$APP_VERSION"
mkdir -p "$DEB_ROOT/DEBIAN"
mkdir -p "$DEB_ROOT/opt/labcoding"
mkdir -p "$DEB_ROOT/usr/share/applications"
mkdir -p "$DEB_ROOT/usr/share/icons/hicolor/256x256/apps"
mkdir -p "$DEB_ROOT/usr/bin"

# Step 3: Copy portable build to /opt/labcoding
cp -a "$PORTABLE/." "$DEB_ROOT/opt/labcoding/"

# Step 4: Create symlink for executable
cat > "$DEB_ROOT/usr/bin/labcoding" << 'LAUNCHER'
#!/usr/bin/env bash
exec /opt/labcoding/Labcoding "$@"
LAUNCHER
chmod 755 "$DEB_ROOT/usr/bin/labcoding"

# Step 5: Create .desktop file
cat > "$DEB_ROOT/usr/share/applications/ccslab-labcoding.desktop" << DESKTOP
[Desktop Entry]
Type=Application
Name=CCSLab Labcoding
Comment=CCSLab Code Lite — AI-assisted coding assistant
Exec=/opt/labcoding/Labcoding %F
Icon=ccslab-labcoding
Terminal=false
Categories=Development;IDE;
StartupWMClass=Labcoding
DESKTOP

# Step 6: Copy icon
cp "$SRC_DIR/app/assets/app.png" "$DEB_ROOT/usr/share/icons/hicolor/256x256/apps/ccslab-labcoding.png"

# Step 7: Create control file
# Calculate package size (in kilobytes)
PKG_SIZE=$(du -sk "$DEB_ROOT/opt/labcoding" | cut -f1)

cat > "$DEB_ROOT/DEBIAN/control" << CONTROL
Package: ccslab-labcoding
Version: ${APP_VERSION}
Section: devel
Priority: optional
Architecture: amd64
Installed-Size: ${PKG_SIZE}
Maintainer: CCSLab <support@ccslab.dev>
Description: CCSLab Code Lite — AI-assisted coding assistant
 CCSLab Code Lite (Labcoding) is an AI-assisted desktop coding
 assistant that integrates with OpenCode Runtime for local
 intelligent code analysis, refactoring, and project assistance.
 .
 Features:
  - AI-powered code analysis and refactoring
  - Multi-session project management
  - Integrated terminal
  - File browsing and editing
 .
 Requires Python 3.12+ and Tkinter (tk) at runtime.
X-OpenCode-Runtime: ${OPENCODE_VERSION}
CONTROL

# Step 8: Create postinst (no network, no system changes)
cat > "$DEB_ROOT/DEBIAN/postinst" << 'POSTINST'
#!/bin/sh
set -e
# Register desktop file if update-desktop-database is available
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database >/dev/null 2>&1 || true
fi
if command -v update-icon-cache >/dev/null 2>&1; then
    update-icon-cache >/dev/null 2>&1 || true
fi
POSTINST
chmod 755 "$DEB_ROOT/DEBIAN/postinst"

# Step 9: Create postrm (preserve user data)
cat > "$DEB_ROOT/DEBIAN/postrm" << 'POSTRM'
#!/bin/sh
set -e
# Clean up desktop entries; user data in ~/.config/ccslab-labcoding is preserved
if [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database >/dev/null 2>&1 || true
    fi
fi
POSTRM
chmod 755 "$DEB_ROOT/DEBIAN/postrm"

# Step 10: Build the .deb
DEB_FILE="$DIST_DIR/ccslab-labcoding_${APP_VERSION}_amd64.deb"
rm -f "$DEB_FILE"

# Use dpkg-deb to build
dpkg-deb --build --root-owner-group "$DEB_ROOT" "$DEB_FILE"

echo ""
echo "=== DEB package built ==="
echo "  File: $DEB_FILE"
echo "  Size: $(du -h "$DEB_FILE" | cut -f1)"
echo "  SHA-256: $(sha256sum "$DEB_FILE" | cut -d' ' -f1)"

# Clean up
rm -rf "$WORK_DIR"

echo ""
echo "=== Done ==="
