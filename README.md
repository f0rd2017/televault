# TG Cloud Cache Manager

> **Unlimited personal cloud storage on top of Telegram.** Desktop app (PySide6 + Telethon) that turns your own Telegram accounts and private channels into a file storage with a local index, multi-account parallel transfers, encryption, streaming and a REST API.

Десктоп-приложение, которое превращает ваши Telegram-аккаунты и приватные каналы в личное облачное хранилище: файловый проводник, параллельная загрузка через несколько аккаунтов, шифрование, стриминг видео и REST API.

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Qt](https://img.shields.io/badge/UI-PySide6-41cd52)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow)
![Tests](https://img.shields.io/badge/tests-474%20passed-brightgreen)

---

## Возможности

**Хранилище**
- 📁 Файловый проводник с папками, поиском (в т.ч. рекурсивным), корзиной и заметками к файлам
- ✂️ Файлы любого размера — прозрачное дробление на части и склейка при скачивании
- 📦 Батчинг мелких файлов в блобы (тысячи мелких файлов не засоряют канал)
- 🔐 Опциональное AES-GCM шифрование содержимого
- ♻️ Дедупликация по SHA-256, replace-by-name
- 🗃️ Локальный SQLite-индекс + полное восстановление индекса сканом канала (`reconcile`)

**Скорость**
- 🚀 Мультиаккаунт-страйпинг: параллельная заливка/выгрузка через несколько аккаунтов и каналов
- 📈 Адаптивный параллелизм и rate-limiter (подстраивается под FloodWait)
- ⏯️ Докачка и дозаливка после обрыва (resume upload/download)
- 🌐 SOCKS5/HTTP/MTProto прокси с резервной цепочкой и авто-эскалацией на лету

**Медиа и интеграции**
- 🖼️ Превью изображений, постеры видео, встроенный медиа-вьювер
- 🎬 Стриминг видео из облака без полного скачивания, на лету перекодируется в fMP4
- 📝 Встроенный текстовый редактор с подсветкой синтаксиса
- 🔗 Шеринг файлов по ссылке, локальный REST API ([docs/REST_API.md](docs/REST_API.md))

## Скриншоты

<!-- TODO: добавить скриншоты -->
| Проводник | Медиа-вьювер |
|---|---|
| *скоро* | *скоро* |

## Быстрый старт

Требуется Python **3.11+**.

```bash
git clone https://github.com/YOUR_NAME/tg-cloud-cache-manager
cd tg-cloud-cache-manager

# через uv (рекомендуется)
uv sync
uv run python run.py

# или классически
python -m venv .venv && source .venv/bin/activate
pip install -e .
python run.py
```

Подробная инструкция по установке (Linux/macOS/Windows): [SETUP.txt](SETUP.txt).

### Настройка

1. Получите `TG_API_ID` и `TG_API_HASH` на [my.telegram.org](https://my.telegram.org) и укажите их в диалоге первого запуска **или** в `.env` (см. [.env.example](.env.example)).
2. Создайте приватный канал под хранилище (по одному на каждый аккаунт).
3. Добавьте аккаунты:

```bash
python scripts/manage_accounts.py
```

Первый аккаунт становится основным. Каждому аккаунту назначается свой канал — по ним и идёт параллельная передача.

## Сборка переносимого приложения

Не требует Python на целевой машине; все данные (`config.json`, `var/`) создаются рядом с исполняемым файлом — папку можно перенести куда угодно.

```bash
./scripts/build.sh      # Linux
scripts\build.bat       # Windows
```

## Архитектура

```text
app/
  config/   конфигурация и валидация
  core/     worker, jobs, accounts, cache, rate limiter
  db/       SQLite-схема и репозиторий
  tg/       Telegram: upload/download/scan/delete, адаптив, прокси
  ui/       PySide6: проводник, диалоги, медиа-вьювер, редактор
  api/      локальный REST API и шеринг
```

Подробнее: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Тесты

```bash
uv run pytest          # 474 теста
uv run ruff check .    # линтер
```

## ⚠️ Дисклеймер

Приложение работает через обычные user-аккаунты Telegram (MTProto API). Интенсивная автоматизация может нарушать [условия использования Telegram](https://core.telegram.org/api/terms) и теоретически привести к ограничению аккаунта. Используйте на свой риск, не храните единственную копию важных данных и не используйте основной аккаунт.

## 💖 Поддержать проект

Если приложение вам полезно — можно поддержать разработку:

| Способ | Реквизиты |
|---|---|
| USDT (TRC-20) | `TODO_ВАШ_АДРЕС` |
| TON | `TODO_ВАШ_АДРЕС` |
| BTC | `TODO_ВАШ_АДРЕС` |
| PayPal | `TODO_ссылка paypal.me` |

Также помогают ⭐ звезда на GitHub, багрепорты и pull request'ы.

## Лицензия

[MIT](LICENSE)
