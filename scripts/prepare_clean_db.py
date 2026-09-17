"""
Очистка БД до состояния «только админ, без отделов и писем».

  python scripts/prepare_clean_db.py

Для портативной сборки задайте APP_ROOT на папку дистрибутива.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_root() -> Path:
    env = os.getenv("APP_ROOT")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd()
    if (cwd / "app" / "main.py").exists():
        return cwd
    return Path(__file__).resolve().parent.parent


ROOT = resolve_root()
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ["SEED_DEFAULT_DEPARTMENTS"] = "false"

from app.config_env import load_project_env, get_env  # noqa: E402
from app.database import engine, SessionLocal, DB_PATH, Base  # noqa: E402
from app import models  # noqa: E402
from app.auth import get_password_hash  # noqa: E402
from app.migrate import run_migrations  # noqa: E402

_CLEAR_ORDER = [
    "correspondence_links",
    "correspondence_attachments",
    "correspondence_history",
    "correspondence_transfers",
    "notification_delivery_logs",
    "notification_reminders",
    "notifications",
    "saved_filters",
    "correspondences",
    "departments",
    "users",
]


def main() -> None:
    load_project_env(override=True)
    os.environ["SEED_DEFAULT_DEPARTMENTS"] = "false"

    print(f"ROOT: {ROOT}")
    print(f"DB:   {DB_PATH}")
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)

    db = SessionLocal()
    try:
        from sqlalchemy import text, inspect

        tables = set(inspect(engine).get_table_names())
        for name in _CLEAR_ORDER:
            if name in tables:
                db.execute(text(f"DELETE FROM {name}"))
        db.commit()

        username = get_env("ADMIN_USERNAME", "admin") or "admin"
        password = get_env("ADMIN_PASSWORD", "admin123") or "admin123"
        email = get_env("ADMIN_EMAIL", "admin@example.com") or "admin@example.com"

        admin = models.User(
            email=email,
            username=username,
            full_name="System Administrator",
            hashed_password=get_password_hash(password),
            department="Администрация",
            role=models.UserRole.CONTROLLER,
            is_active=True,
            is_admin=True,
            phone_number=None,
            notify_email=True,
            notify_sms=False,
            notify_browser=True,
            reminder_days_before=5,
        )
        db.add(admin)
        db.commit()

        users = db.query(models.User).count()
        depts = db.query(models.DepartmentEntity).count()
        letters = db.query(models.Correspondence).count()
        print(f"Готово: users={users}, departments={depts}, correspondences={letters}")
        print(f"Админ: {username}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
