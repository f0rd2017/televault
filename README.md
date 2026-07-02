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

## Настройка

1. Создай `.env` на основе `.env.example`
2. Укажи `TG_API_ID` и `TG_API_HASH`
3. Добавь аккаунты через:

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
