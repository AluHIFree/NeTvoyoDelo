"""Загрузка .env из корня проекта (не зависит от cwd)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# app/config_env.py -> корень репозитория
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

_loaded = False


def load_project_env(*, override: bool = False) -> Path:
    """Загрузить .env из корня проекта. Возвращает путь к файлу."""
    global _loaded
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=override, encoding="utf-8-sig")
    else:
        # fallback: текущая директория
        load_dotenv(override=override, encoding="utf-8-sig")
    _loaded = True
    return ENV_PATH


def get_env(name: str, default: str = "") -> str:
    """Прочитать переменную, подхватив .env при необходимости."""
    if not _loaded:
        load_project_env(override=False)
    value = os.getenv(name)
    if value is None or not str(value).strip():
        # Перечитать с override — на случай, если ключ добавили после старта
        load_project_env(override=True)
        value = os.getenv(name, default)
    raw = (value if value is not None else default) or ""
    raw = str(raw).strip()
    if (raw.startswith('"') and raw.endswith('"')) or (
        raw.startswith("'") and raw.endswith("'")
    ):
        raw = raw[1:-1].strip()
    return raw


def _looks_like_placeholder(value: str) -> bool:
    low = value.lower()
    return low in {
        "",
        "your_key",
        "changeme",
        "xxx",
        "your_sms_api_key",
        "your-api-key",
    }


def is_cloudflare_configured() -> bool:
    """True, если заданы ключ и account id Cloudflare Workers AI."""
    key = get_env("CLOUDFLARE_API_KEY")
    account = get_env("CLOUDFLARE_ACCOUNT_ID")
    if _looks_like_placeholder(key) or _looks_like_placeholder(account):
        return False
    return bool(key and account)


# Обратная совместимость со старым именем
def is_alltoken_configured() -> bool:
    return is_cloudflare_configured()
