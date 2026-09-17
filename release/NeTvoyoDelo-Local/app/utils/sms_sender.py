"""
Отправка SMS через SMS.ru-совместимый HTTP API (httpx).
"""
from __future__ import annotations

import httpx

from app.config_env import get_env


def _sms_settings() -> tuple[str, str]:
    url = get_env("SMS_API_URL", "https://sms.ru/sms/send") or "https://sms.ru/sms/send"
    key = get_env("SMS_API_KEY")
    return url, key


def _truncate(message: str, limit: int = 160) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 3] + "..."


def sms_is_configured() -> bool:
    _, key = _sms_settings()
    if not key:
        return False
    low = key.lower()
    return low not in {"your_sms_api_key", "changeme", "xxx", "your_key"}


async def send_sms(phone_number: str, message: str) -> bool:
    """
    Отправка SMS. Возвращает False, если API не настроен или запрос неуспешен.
    Текст обрезается до 160 символов.
    """
    message = _truncate(message or "", 160)
    phone = (phone_number or "").strip()

    if not phone:
        return False

    if not sms_is_configured():
        print(f"[WARN] SMS API не настроен. Пропускаем отправку SMS на {phone}")
        return False

    sms_url, sms_key = _sms_settings()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                sms_url,
                data={
                    "api_id": sms_key,
                    "to": phone,
                    "msg": message,
                    "json": 1,
                },
            )

            if response.status_code != 200:
                print(f"[ERR] Ошибка HTTP SMS: {response.status_code}")
                return False

            result = response.json()
            if result.get("status") == "OK":
                print(f"[OK] SMS отправлено на {phone}")
                return True

            sms_block = result.get("sms") or {}
            phone_block = sms_block.get(phone) or next(iter(sms_block.values()), None)
            if isinstance(phone_block, dict) and phone_block.get("status") == "OK":
                print(f"[OK] SMS отправлено на {phone}")
                return True

            print(f"[ERR] Ошибка SMS API: {result}")
            return False

    except Exception as e:
        print(f"[ERR] Ошибка отправки SMS: {e}")
        return False
