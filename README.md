# Telegram Cloud Cache Manager

Desktop-приложение на `PySide6`, которое использует Telegram user accounts как облачное хранилище файлов с локальным SQLite-индексом и многоканальной передачей.

## Что умеет

- Загружать файлы в Telegram через несколько user accounts
- Скачивать файлы обратно в локальный кэш
- Индексировать каналы и восстанавливать локальное состояние через `reconcile`
- Удалять и переименовывать удалённые объекты
- Работать без основной main-session, если доступны user accounts из базы

## Стек

- `Python 3.11+`
- `PySide6`
- `Telethon`
- `SQLite`
- `tenacity`
- `cryptography`
- `python-dotenv`
- `psutil`

## Структура

```text
app/
  config/   конфигурация и валидация
  core/     worker, jobs, accounts, cache, utils
  db/       схема SQLite и репозиторий
  tg/       Telegram client/upload/download/scan/delete
  ui/       главное окно, диалоги и модели Qt
scripts/
  auth_session.py
  manage_accounts.py
tests/
  unit/
  integration_mock/
```

## Запуск

```bash
pip install -e ".[dev]"
python run.py
```

Для отладки:

```bash
TGCCM_DEBUG=1 python run.py
```

## Сборка (переносимое приложение, любой ПК)

Собранное приложение не требует Python на целевой машине. Все данные
(`config.json`, `var/` — БД, кэш, логи) создаются **рядом с исполняемым
файлом** — папку `dist/TG_Cloud_Cache_Manager/` можно перенести куда угодно.

```bash
# Linux (собирает под Linux)
./scripts/build.sh
```

```bat
:: Windows (собирает под Windows; нужен Python 3.11+ и venv в .venv)
scripts\build.bat
```

PyInstaller собирает только под ту ОС, на которой запущен: Windows-сборка
делается на Windows, Linux-сборка — на Linux. API ID/Hash можно задать прямо
в диалоге первого запуска — ручной `.env` не обязателен.

## Настройка

1. Укажи `TG_API_ID` и `TG_API_HASH` — в диалоге первого запуска **или** в
   `.env` на основе `.env.example` (`.env` имеет приоритет)
2. Добавь аккаунты через:

```bash
python scripts/manage_accounts.py
```

Первый аккаунт становится основным. Каналы задаются на уровне аккаунтов и хранятся в базе `accounts`, а не в `config.json`.

## Основные файлы

- `run.py` — удобная точка запуска
- `app/main.py` — инициализация Qt, БД и worker
- `scripts/manage_accounts.py` — управление аккаунтами
- `scripts/auth_session.py` — авторизация основной Telegram session

## Тесты

```bash
./.venv/bin/pytest tests -q
```

## Текущее состояние проекта

- Bot-ветка удалена из пользовательского UI и тестового контура
- Проект ориентирован только на Telegram user accounts
- Rename теперь сразу синхронизирует имя в локальном индексе
- Multipart upload через пул клиентов корректно стартует уже для файлов от `1 MB`, если включается pool striping
