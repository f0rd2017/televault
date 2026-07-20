#!/usr/bin/env bash
set -e

cd "$(dirname "$0")/.."

echo "==> Compiling translation files (.ts -> .qm)..."
./.venv/bin/pyside6-lrelease src/televault/i18n/*.ts

echo "==> Done. Compiled .qm files are ready to use."
