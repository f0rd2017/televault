@echo off
rem Сборка под Windows. Запускать из корня проекта или двойным кликом.
cd /d "%~dp0\.."

echo ==^> Установка зависимостей
.venv\Scripts\python -m pip install -e .[dev] || exit /b 1

echo ==^> Очистка старых билдов
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo ==^> Сборка PyInstaller (по spec)
.venv\Scripts\python -m PyInstaller --noconfirm TG_Cloud_Cache_Manager.spec || exit /b 1

echo ==^> Готово! Сборка в dist\TG_Cloud_Cache_Manager\
echo     Приложение переносимо: config.json и var\ создаются рядом с exe.
