#!/usr/bin/env bash
set -e

cd "$(dirname "$0")/.."

echo "==> Компиляция файлов перевода (.ts -> .qm)..."
./.venv/bin/pyside6-lrelease app/i18n/*.ts

echo "==> Готово. Скомпилированные .qm файлы готовы к использованию."
