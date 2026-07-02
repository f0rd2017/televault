#!/usr/bin/env bash
set -e

cd "$(dirname "$0")/.."

echo "==> Обновление файлов перевода (.ts) из исходного кода..."
./.venv/bin/pyside6-lupdate -extensions py app/ -ts app/i18n/ru_RU.ts app/i18n/en_US.ts

echo "==> Готово. Теперь можно открыть app/i18n/en_US.ts в Qt Linguist для перевода."
