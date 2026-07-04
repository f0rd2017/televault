#!/usr/bin/env python3
"""
Manage Telegram accounts for uploads from the terminal.
Usage: python scripts/manage_accounts.py
"""

import asyncio
import re
import sys
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from app.db.database import connect_db
from app.db.repo import DbRepo
from app.core.types import TelegramAccount
from app.core.utils import ensure_dir


def print_header():
    print("=" * 60)
    print("📡 Управление Telegram аккаунтами для upload")
    print("=" * 60)
    print()


def print_accounts(accounts: list[TelegramAccount]):
    if not accounts:
        print("Аккаунтов пока нет. Добавьте первый!\n")
        return

    print(
        f"{'ID':<4} {'Основной':<8} {'Активен':<8} {'Метка':<15} {'Телефон':<18} {'Username':<15} {'Канал':<30}"
    )
    print("-" * 100)
    for acc in accounts:
        primary = "⭐ ДА" if acc.is_primary else ""
        active = "✅" if acc.is_active else "❌"
        phone = acc.phone_masked or "—"
        username = acc.username or "—"
        channel = acc.chat_target
        print(
            f"{acc.id:<4} {primary:<8} {active:<8} {acc.label:<15} {phone:<18} {username:<15} {channel:<30}"
        )
    print()


async def authorize_account(
    phone: str, api_id: int, api_hash: str, session_path: str, proxy: str = ""
) -> dict | None:
    """Authorize a new account from the terminal."""
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
    from app.core.utils import build_telethon_proxy

    proxy_obj = build_telethon_proxy(proxy) if proxy else None
    conn_info = f" (через {proxy})" if proxy else " (напрямую)"
    print(f"\n🔗 Подключаюсь к Telegram (API: {api_id}){conn_info}...")
    client = TelegramClient(
        session_path, api_id, api_hash, proxy=proxy_obj if proxy_obj else None
    )

    try:
        await client.connect()

        if await client.is_user_authorized():
            print("✅ Сессия уже авторизована!")
        else:
            print(f"📱 Отправляю код на {phone}...")
            try:
                await client.send_code_request(phone)
                print("✅ Код отправлен! Проверьте приложение Telegram.")
            except FloodWaitError as e:
                print(f"❌ FloodWait: подождите {e.seconds} секунд")
                await client.disconnect()
                return None
            except Exception as e:
                print(f"❌ Ошибка отправки кода: {e}")
                await client.disconnect()
                return None

            code = input("\n🔢 Введите код из Telegram: ").strip()
            if not code:
                print("❌ Код не введён")
                await client.disconnect()
                return None

            try:
                await client.sign_in(phone, code)
            except Exception as e:
                print(f"❌ Ошибка входа: {e}")
                await client.disconnect()
                return None

            if not await client.is_user_authorized():
                print("❌ Авторизация не прошла")
                await client.disconnect()
                return None

        # Fetch info
        me = await client.get_me()
        info = {
            "id": getattr(me, "id", 0),
            "username": getattr(me, "username", "") or "",
            "phone": getattr(me, "phone", phone) or phone,
            "premium": bool(getattr(me, "premium", False)),
        }

        print(f"\n✅ Авторизован: {info['username'] or 'ID:' + str(info['id'])}")
        print(f"   Телефон: {info['phone']}")
        print(f"   Premium: {'Да' if info['premium'] else 'Нет'}")

        await client.disconnect()
        return info

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await client.disconnect()
        return None


async def add_account(repo: DbRepo, default_api_id: int, default_api_hash: str):
    """Add a new account."""
    print("\n➕ Добавление нового аккаунта")
    print("-" * 40)

    label = input("Метка аккаунта (например 'Аккаунт 2'): ").strip()
    if not label:
        print("❌ Метка обязательна")
        return

    # API ID/Hash for this account
    print(f"\n🔑 API ID/Hash (нажми Enter чтобы использовать общие: {default_api_id})")
    api_id_str = input("API ID: ").strip()
    if api_id_str:
        try:
            api_id = int(api_id_str)
            if api_id <= 0:
                print("❌ API ID должен быть положительным числом")
                return
        except ValueError:
            print("❌ API ID должен быть числом")
            return
    else:
        api_id = default_api_id

    api_hash = input("API Hash (пусто = общий): ").strip()
    if not api_hash:
        api_hash = default_api_hash

    if not api_id or not api_hash:
        print("❌ API ID и API Hash обязательны")
        return

    phone = input("Номер телефона (например +79991234567): ").strip()
    if not phone:
        print("❌ Телефон обязателен")
        return

    # Validate phone number format
    phone_pattern = r"^\+?[1-9]\d{1,14}$"
    if not re.match(phone_pattern, phone.replace(" ", "").replace("-", "")):
        print("❌ Неверный формат номера телефона")
        return

    # Check for a duplicate
    phone_clean = re.sub(r"[^0-9]", "", phone)
    for acc in repo.list_accounts():
        acc_phone_clean = re.sub(r"[^0-9]", "", acc.phone_masked or "")
        if acc_phone_clean and acc_phone_clean == phone_clean:
            print(
                f"❌ Аккаунт с телефоном {phone} уже существует: '{acc.label}' (ID: {acc.id})"
            )
            return

    # Authorization
    session_dir = Path("./var/data/account_sessions")
    ensure_dir(session_dir)
    session_path = str(session_dir / f"acc_{phone_clean}.session")

    # Ask for the proxy BEFORE authorizing
    print("\n🌐 Прокси для подключения (пусто = напрямую)")
    print(
        "   Формат: host:port:user:pass | socks5://… | http://… (тип определится автоматически)"
    )
    proxy = input("Прокси: ").strip()
    if proxy:
        # Validate the format (socks5/socks4/http; with scheme or short form)
        from app.core.utils import parse_proxy

        try:
            parse_proxy(proxy)
        except ValueError as exc:
            print(f"❌ Неверный формат прокси: {exc}")
            print(
                "   Примеры: host:port:user:pass | socks5://host:port | http://user:pass@host:port"
            )
            return
        print(f"   ✅ Авторизация и подключение будут через прокси: {proxy}")

    info = await authorize_account(phone, api_id, api_hash, session_path, proxy)
    if not info:
        return

    # Channel
    channel = input(
        "\n💬 Ссылка на канал (https://t.me/+xxxxx или @username): "
    ).strip()
    if not channel:
        print("❌ Канал обязателен")
        return

    # Validate the channel link format
    channel_pattern = r"^(https://t\.me/[\w\d_]+|@[\w\d_]+|[\w\d_]+)$"
    if not re.match(channel_pattern, channel):
        print(
            "❌ Неверный формат ссылки на канал. Допустимые форматы: https://t.me/username, @username, username"
        )
        return

    # The first account becomes the primary one
    is_primary = len(repo.list_accounts()) == 0

    account = TelegramAccount(
        id=0,
        label=label,
        session_path=session_path,
        tg_api_id=api_id,
        tg_api_hash=api_hash,
        chat_target=channel,
        is_active=True,
        is_primary=is_primary,
        proxy=proxy,
        phone_masked=info["phone"][:-4] + "****" if len(info["phone"]) > 4 else "****",
        user_id=info["id"],
        username=info["username"],
        is_premium=info["premium"],
    )

    acc_id = repo.insert_account(account)
    print(f"\n✅ Аккаунт '{label}' добавлен! (ID: {acc_id})")
    if is_primary:
        print("   Это основной аккаунт (без прокси)")


async def set_primary(repo: DbRepo, account_id: int):
    """Make an account the primary one."""
    # Check that the account exists
    acc = repo.get_account(account_id)
    if not acc:
        print(f"❌ Аккаунт ID={account_id} не найден")
        return

    # Clear primary from all and set it for the chosen one in a single transaction
    with repo.conn:
        # First clear primary from all accounts
        repo.conn.execute("UPDATE accounts SET is_primary = 0")
        # Then set it for the chosen account
        repo.update_account(account_id, is_primary=1)
    print(f"✅ Аккаунт ID={account_id} теперь основной")


async def toggle_active(repo: DbRepo, account_id: int):
    """Enable/disable an account."""
    acc = repo.get_account(account_id)
    if not acc:
        print(f"❌ Аккаунт ID={account_id} не найден")
        return

    new_state = not acc.is_active
    repo.update_account(account_id, is_active=1 if new_state else 0)
    print(f"✅ Аккаунт '{acc.label}' {'включён' if new_state else 'выключен'}")


async def set_channel(repo: DbRepo, account_id: int):
    """Change an account's channel."""
    acc = repo.get_account(account_id)
    if not acc:
        print(f"❌ Аккаунт ID={account_id} не найден")
        return

    print(f"Текущий канал: {acc.chat_target}")
    channel = input("Новый канал (https://t.me/+xxxxx или @username): ").strip()
    if channel:
        # Validate the channel link format
        channel_pattern = r"^(https://t\.me/[\w\d_]+|@[\w\d_]+|[\w\d_]+)$"
        if not re.match(channel_pattern, channel):
            print(
                "❌ Неверный формат ссылки на канал. Допустимые форматы: https://t.me/username, @username, username"
            )
            return
        repo.update_account(account_id, chat_target=channel)
        print(f"✅ Канал обновлён: {channel}")


async def set_proxy(repo: DbRepo, account_id: int):
    """Change an account's proxy."""
    acc = repo.get_account(account_id)
    if not acc:
        print(f"❌ Аккаунт ID={account_id} не найден")
        return

    print(f"Текущий прокси: {acc.proxy or 'Без прокси'}")
    proxy = input(
        "Новый прокси (IP:PORT:USER:PASS, или пусто для отключения): "
    ).strip()
    if proxy:
        # Validate the format (socks5/socks4/http; with scheme or short form)
        from app.core.utils import parse_proxy

        try:
            parse_proxy(proxy)
        except ValueError as exc:
            print(f"❌ Неверный формат прокси: {exc}")
            print(
                "   Примеры: host:port:user:pass | socks5://host:port | http://user:pass@host:port"
            )
            return
    repo.update_account(account_id, proxy=proxy)
    print(f"✅ Прокси обновлён: {proxy or 'Без прокси'}")


async def delete_account(repo: DbRepo, account_id: int):
    """Delete an account."""
    acc = repo.get_account(account_id)
    if not acc:
        print(f"❌ Аккаунт ID={account_id} не найден")
        return

    confirm = input(f"Удалить аккаунт '{acc.label}'? (y/n): ").strip().lower()
    if confirm != "y":
        return

    # Remove the session file
    session_path = Path(acc.session_path)
    if session_path.exists():
        try:
            session_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"⚠️ Не удалось удалить файл сессии: {e}")

    repo.delete_account(account_id)
    print(f"✅ Аккаунт '{acc.label}' удалён")


async def main():
    load_dotenv()
    import os

    # Resolve the project root (two levels above this script)
    project_root = Path(__file__).resolve().parent.parent
    os.chdir(project_root)

    # Check and fetch API ID/Hash
    api_id_str = os.getenv("TG_API_ID", "0")
    api_hash = os.getenv("TG_API_HASH", "")

    try:
        api_id = int(api_id_str)
    except ValueError:
        print("❌ TG_API_ID должен быть числом!")
        return

    if not api_id or not api_hash:
        print("❌ TG_API_ID и TG_API_HASH не настроены в .env!")
        return

    # Connect to the DB — use an absolute path
    db_path = project_root / "data" / "index.sqlite3"
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"❌ Не удалось создать директорию для БД: {e}")
        return

    print(f"📁 База данных: {db_path}")

    try:
        conn = connect_db(db_path)
        repo = DbRepo(conn)
        repo.init_schema()  # Ensure the accounts table exists
    except Exception as e:
        print(f"❌ Не удалось подключиться к базе данных: {e}")
        return

    # Verify the table was created
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        if "accounts" not in table_names:
            print("❌ Таблица accounts не создана! Произошла ошибка миграции.")
            conn.close()
            return
    except Exception as e:
        print(f"❌ Ошибка проверки таблиц: {e}")
        conn.close()
        return

    print("✅ Таблица accounts найдена")

    print_header()

    try:
        while True:
            try:
                accounts = repo.list_accounts()
                print("\nТекущие аккаунты:")
                print_accounts(accounts)

                print("Меню:")
                print("  1 - Добавить аккаунт")
                print("  2 - Сделать основным (⭐)")
                print("  3 - Включить/Выключить")
                print("  4 - Изменить канал")
                print("  5 - Изменить прокси")
                print("  6 - Удалить аккаунт")
                print("  0 - Выход")
                print()

                choice = input("Выбор: ").strip()

                if choice == "1":
                    await add_account(repo, api_id, api_hash)
                elif choice == "2":
                    acc_id = input("ID аккаунта: ").strip()
                    if acc_id.isdigit():
                        await set_primary(repo, int(acc_id))
                    else:
                        print("❌ ID аккаунта должен быть числом")
                elif choice == "3":
                    acc_id = input("ID аккаунта: ").strip()
                    if acc_id.isdigit():
                        await toggle_active(repo, int(acc_id))
                    else:
                        print("❌ ID аккаунта должен быть числом")
                elif choice == "4":
                    acc_id = input("ID аккаунта: ").strip()
                    if acc_id.isdigit():
                        await set_channel(repo, int(acc_id))
                    else:
                        print("❌ ID аккаунта должен быть числом")
                elif choice == "5":
                    acc_id = input("ID аккаунта: ").strip()
                    if acc_id.isdigit():
                        await set_proxy(repo, int(acc_id))
                    else:
                        print("❌ ID аккаунта должен быть числом")
                elif choice == "6":
                    acc_id = input("ID аккаунта: ").strip()
                    if acc_id.isdigit():
                        await delete_account(repo, int(acc_id))
                    else:
                        print("❌ ID аккаунта должен быть числом")
                elif choice == "0":
                    print("\n👋 До свидания!")
                    break
                else:
                    print("❌ Неверный выбор")

                input("\nНажмите Enter для продолжения...")
                print("\n" + "=" * 60)
            except KeyboardInterrupt:
                print("\n👋 Прервано пользователем!")
                break
            except Exception as e:
                print(f"❌ Непредвиденная ошибка: {e}")
                input("\nНажмите Enter для продолжения...")
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
