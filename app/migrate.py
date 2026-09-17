"""
SQLite-совместимые миграции схемы.

Вызывать после Base.metadata.create_all(bind=engine), например в main.py:

    from app.migrate import run_migrations
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
"""

from datetime import datetime

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app import models
from app.database import SessionLocal


# Колонки, которые должны быть у users / correspondences (имя -> SQL-тип для ADD COLUMN)
_USER_COLUMNS = {
    "role": "VARCHAR DEFAULT 'executor'",
    "notify_email": "BOOLEAN DEFAULT 1",
    "notify_sms": "BOOLEAN DEFAULT 1",
    "notify_browser": "BOOLEAN DEFAULT 1",
    "quiet_hours_start": "INTEGER",
    "quiet_hours_end": "INTEGER",
    "reminder_days_before": "INTEGER DEFAULT 5",
    "avatar_path": "VARCHAR",
}

_CORRESPONDENCE_COLUMNS = {
    "completion_note": "TEXT",
    "notification_sent": "BOOLEAN DEFAULT 0",
    "flow": "VARCHAR",
    "request_type": "VARCHAR",
    "request_note": "TEXT",
    "sent_info": "VARCHAR",
    "to_whom": "VARCHAR",
    "control": "VARCHAR",
    "report": "VARCHAR",
    "report_date": "DATE",
    "completed_at": "DATETIME",
    "waiting_external": "BOOLEAN DEFAULT 0",
    "waiting_external_note": "VARCHAR",
}

# Имена enum-членов SQLAlchemy → русские значения
_DEPARTMENT_NAME_TO_VALUE = {
    "SCHOOL_DEPARTMENT": models.Department.SCHOOL_DEPARTMENT.value,
    "GIA_DEPARTMENT": models.Department.GIA_DEPARTMENT.value,
    "DEPARTEMENT": models.Department.DEPARTEMENT.value,
    # на случай, если хранилось как Department.NAME
    "Department.SCHOOL_DEPARTMENT": models.Department.SCHOOL_DEPARTMENT.value,
    "Department.GIA_DEPARTMENT": models.Department.GIA_DEPARTMENT.value,
    "Department.DEPARTEMENT": models.Department.DEPARTEMENT.value,
}


def _existing_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
    return {row[1] for row in rows}


def _add_missing_columns(conn, table_name: str, columns: dict[str, str]) -> None:
    existing = _existing_columns(conn, table_name)
    for col_name, col_def in columns.items():
        if col_name not in existing:
            conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")
            )


def _seed_departments(db: Session) -> None:
    """Сид стандартных отделов. Отключается через SEED_DEFAULT_DEPARTMENTS=false."""
    from app.config_env import get_env

    flag = (get_env("SEED_DEFAULT_DEPARTMENTS", "true") or "true").lower()
    if flag in ("0", "false", "no", "off"):
        return
    if db.query(models.DepartmentEntity).count() > 0:
        return
    for dept in models.Department:
        db.add(
            models.DepartmentEntity(
                name=dept.value,
                is_active=True,
                created_at=datetime.utcnow(),
            )
        )
    db.commit()


def _convert_user_departments(conn) -> None:
    """Если department хранится как имя enum-члена — заменить на русское значение."""
    if "users" not in inspect(conn).get_table_names():
        return
    rows = conn.execute(text("SELECT id, department FROM users")).fetchall()
    for user_id, department in rows:
        if department is None:
            continue
        # Уже русское значение — не трогаем
        if department in {d.value for d in models.Department}:
            continue
        russian = _DEPARTMENT_NAME_TO_VALUE.get(department)
        if russian:
            conn.execute(
                text("UPDATE users SET department = :dept WHERE id = :id"),
                {"dept": russian, "id": user_id},
            )


def run_migrations(engine: Engine) -> None:
    """
    1) create_all для новых таблиц
    2) ADD COLUMN для недостающих полей users / correspondences (SQLite)
    3) сид отделов
    4) конвертация department из имён enum в русские строки (если нужно)
    """
    # Новые таблицы / индексы по моделям
    models.Base.metadata.create_all(bind=engine)

    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "sqlite":
            tables = set(inspect(conn).get_table_names())
            if "users" in tables:
                _add_missing_columns(conn, "users", _USER_COLUMNS)
                _convert_user_departments(conn)
            if "correspondences" in tables:
                _add_missing_columns(conn, "correspondences", _CORRESPONDENCE_COLUMNS)

    db = SessionLocal()
    try:
        _seed_departments(db)
    finally:
        db.close()
