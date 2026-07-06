#!/usr/bin/env bash
set -e

# Move to the project root
cd "$(dirname "$0")/.."

echo "==> Installing dependencies (optional, if not already installed)"
./.venv/bin/pip install -e .[dev]

echo "==> Cleaning old builds"
rm -rf build/ dist/

echo "==> Building with PyInstaller (spec is the single source of truth)"
./.venv/bin/python -m PyInstaller --noconfirm TeleVault.spec

echo "==> Done! The build is in dist/TeleVault/"
echo "    The app is portable: config.json and var/ (data, cache, logs)"
echo "    are created next to the executable."
