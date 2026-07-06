@echo off
rem Windows build. Run from the project root or by double-clicking.
cd /d "%~dp0\.."

echo ==^> Installing dependencies
.venv\Scripts\python -m pip install -e .[dev] || exit /b 1

echo ==^> Cleaning old builds
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo ==^> Building with PyInstaller (from spec)
.venv\Scripts\python -m PyInstaller --noconfirm TeleVault.spec || exit /b 1

echo ==^> Done! Build output is in dist\TeleVault\
echo     The app is portable: config.json and var\ are created next to the exe.
