#!/usr/bin/env bash
set -e

# Переходим в корень проекта
cd "$(dirname "$0")/.."

echo "==> Установка зависимостей (опционально, если не установлены)"
./.venv/bin/pip install -e .[dev]

echo "==> Очистка старых билдов"
rm -rf build/ dist/

echo "==> Сборка PyInstaller"
./.venv/bin/pyinstaller \
    --noconfirm \
    --onedir \
    --windowed \
    --name "TG_Cloud_Cache_Manager" \
    --add-data "app/assets:app/assets" \
    --add-data "app/i18n:app/i18n" \
    run.py

echo "==> Готово! Сборка находится в dist/TG_Cloud_Cache_Manager/"
