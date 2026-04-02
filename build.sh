#!/bin/bash
# Build shitman into a standalone executable using PyInstaller
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Install PyInstaller if not present
pip install pyinstaller --break-system-packages 2>/dev/null || pip install pyinstaller

pyinstaller \
    --onefile \
    --clean \
    --name shitman \
    --strip \
    shitman.py

echo ""
echo "Build complete: dist/shitman"
