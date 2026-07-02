#!/usr/bin/env bash
set -e

# Переходим в корень проекта
cd "$(dirname "$0")/.."

echo "==> Установка зависимостей (опционально, если не установлены)"
./.venv/bin/pip install -e .[dev]

echo "==> Очистка старых билдов"
rm -rf build/ dist/

echo "==> Сборка PyInstaller (по spec — единый источник правды)"
./.venv/bin/python -m PyInstaller --noconfirm TG_Cloud_Cache_Manager.spec

echo "==> Готово! Сборка находится в dist/TG_Cloud_Cache_Manager/"
echo "    Приложение переносимо: config.json, var/ (данные, кэш, логи)"
echo "    создаются рядом с исполняемым файлом."
