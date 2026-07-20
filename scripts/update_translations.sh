#!/usr/bin/env bash
set -e

cd "$(dirname "$0")/.."

echo "==> Extracting translatable strings from source into .ts files..."
# Source strings (self.tr(...) in the code) are English — there is no
# en_US.ts, English needs no translation. Update the real translations here.
./.venv/bin/pyside6-lupdate -extensions py src/televault/ -ts src/televault/i18n/ru_RU.ts src/televault/i18n/uk_UA.ts

echo "==> Done. Open src/televault/i18n/ru_RU.ts / src/televault/i18n/uk_UA.ts in Qt Linguist to translate any new strings."
