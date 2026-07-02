"""
Скрипт для авторизации нового Telegram аккаунта.
Запускается отдельно от основного приложения чтобы не конфликтовать с event loop.
Использование: python auth_session.py <phone> <session_path> <api_id> <api_hash>
"""

import asyncio
import json
import sys
from pathlib import Path


async def authorize(phone: str, session_path: str, api_id: int, api_hash: str) -> bool:
    from getpass import getpass

    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    print(f"Подключаюсь к Telegram (API: {api_id})...")
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        print("✅ Сессия уже авторизована!")
    else:
        print(f"Отправляю код на {phone}...")
        try:
            await client.send_code_request(phone)
        except Exception as e:
            print(f"❌ Ошибка отправки кода: {e}")
            await client.disconnect()
            return False

        print("Проверьте приложение Telegram на вашем устройстве.")
        code = input("Введите код подтверждения: ").strip()
        if not code:
            print("❌ Код не введён")
            await client.disconnect()
            return False

        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            # На аккаунте включена двухфакторная аутентификация (облачный пароль).
            print("Требуется пароль двухфакторной аутентификации (2FA).")
            for _ in range(3):
                password = getpass("Введите пароль 2FA: ").strip()
                if not password:
                    print("❌ Пароль не введён")
                    continue
                try:
                    await client.sign_in(password=password)
                    break
                except Exception as e:
                    print(f"❌ Неверный пароль 2FA: {e}")
            else:
                print("❌ Не удалось пройти 2FA")
                await client.disconnect()
                return False
        except Exception as e:
            print(f"❌ Ошибка входа: {e}")
            await client.disconnect()
            return False

        if not await client.is_user_authorized():
            print("❌ Авторизация не прошла")
            await client.disconnect()
            return False

    # Получаем инфу о пользователе
    me = await client.get_me()
    info = {
        "id": getattr(me, "id", 0),
        "username": getattr(me, "username", "") or "",
        "phone": getattr(me, "phone", phone) or phone,
        "phone_masked": (getattr(me, "phone", phone) or phone)[:-4] + "****",
        "premium": bool(getattr(me, "premium", False)),
    }

    print(f"\n✅ Авторизован: {info['username'] or info['id']}")
    print(f"   Телефон: {info['phone']}")
    print(f"   Premium: {'Да' if info['premium'] else 'Нет'}")

    # Сохраняем инфу
    session_dir = Path(session_path).parent
    phone_clean = info["phone"].replace("+", "").replace(" ", "").replace("-", "")
    info_path = session_dir / f"auth_{phone_clean}_info.json"
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2))

    await client.disconnect()
    return True


def main():
    if len(sys.argv) < 5:
        print(
            "Использование: auth_session.py <phone> <session_path> <api_id> <api_hash>"
        )
        sys.exit(1)

    phone = sys.argv[1]
    session_path = sys.argv[2]
    api_id = int(sys.argv[3])
    api_hash = sys.argv[4]

    # Создаём директорию
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)

    ok = asyncio.run(authorize(phone, session_path, api_id, api_hash))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
