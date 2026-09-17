"""
Сервис уведомлений: сроки, просрочка, «зависшие» письма, передачи.
Отправка напрямую (await), без BackgroundTasks.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy.orm import Session

from app import models
from app.config_env import get_env
from app.utils.email_sender import send_email
from app.utils.sms_sender import send_sms


def _stale_letter_days() -> int:
    raw = get_env("STALE_LETTER_DAYS", "7") or "7"
    try:
        return max(1, int(raw))
    except ValueError:
        return 7


def _public_base_url() -> str:
    return get_env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def is_quiet_hours(user: models.User, now: Optional[datetime] = None) -> bool:
    """True, если текущий час попадает в тихий интервал пользователя."""
    start = getattr(user, "quiet_hours_start", None)
    end = getattr(user, "quiet_hours_end", None)
    if start is None or end is None:
        return False
    try:
        start_h = int(start)
        end_h = int(end)
    except (TypeError, ValueError):
        return False
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
        return False
    if start_h == end_h:
        return False

    hour = (now or datetime.now()).hour
    if start_h < end_h:
        return start_h <= hour < end_h
    # Через полночь, например 22–7
    return hour >= start_h or hour < end_h


def _pref(user: models.User, name: str, default: bool = True) -> bool:
    val = getattr(user, name, default)
    return True if val is None else bool(val)


def _reminder_days(user: models.User) -> int:
    days = getattr(user, "reminder_days_before", None)
    try:
        days = int(days) if days is not None else 5
    except (TypeError, ValueError):
        days = 5
    return max(1, days)


def _stage_for_days(days_left: int, reminder_days: int, today: date) -> Optional[str]:
    if days_left < 0:
        return f"overdue_{today.isoformat()}"
    if days_left == 0:
        return "d0"
    if days_left == 1:
        return "d1"
    if 1 < days_left <= reminder_days:
        return "d5"
    return None


def _stage_label(stage: str, days_left: int) -> str:
    if stage.startswith("overdue"):
        return f"просрочено на {abs(days_left)} дн."
    if stage == "d0":
        return "срок сегодня"
    if stage == "d1":
        return "остался 1 день"
    return f"осталось {days_left} дн."


def _letter_link(correspondence_id: int) -> str:
    return f"{_public_base_url()}/api/correspondences/{correspondence_id}"


def _build_deadline_message(corr: models.Correspondence, days_left: int, stage: str) -> tuple[str, str]:
    label = _stage_label(stage, days_left)
    deadline_str = corr.deadline.strftime("%d.%m.%Y") if corr.deadline else "—"
    content_preview = (corr.content or "")[:200]
    if len(corr.content or "") > 200:
        content_preview += "..."

    if stage.startswith("overdue"):
        advice = "Рекомендуем срочно взять в работу или уточнить статус исполнения."
        subject = f"Просрочено: письмо №{corr.incoming_number} — {label}"
    elif stage == "d0":
        advice = "Срок сегодня — рекомендуем взять в работу и закрыть письмо."
        subject = f"Срок сегодня: письмо №{corr.incoming_number}"
    elif stage == "d1":
        advice = "Завтра срок — рекомендуем взять в работу сегодня."
        subject = f"Критично: письмо №{corr.incoming_number} — завтра срок"
    else:
        advice = "Пора начинать — рекомендуем взять письмо в работу."
        subject = f"Пора начинать: письмо №{corr.incoming_number} — {label}"

    message = (
        f"Письмо №{corr.incoming_number} от {corr.sender}.\n"
        f"Срок исполнения: {deadline_str} ({label}).\n"
        f"{advice}\n"
        f"Содержание: {content_preview}"
    )
    return subject, message


def _log_delivery(
    db: Session,
    user_id: int,
    correspondence_id: Optional[int],
    channel: str,
    status: str,
    detail: Optional[str] = None,
) -> None:
    db.add(
        models.NotificationDeliveryLog(
            user_id=user_id,
            correspondence_id=correspondence_id,
            channel=channel,
            status=status,
            detail=detail,
        )
    )


def _reminder_exists(
    db: Session,
    correspondence_id: int,
    user_id: int,
    stage: str,
) -> bool:
    return (
        db.query(models.NotificationReminder)
        .filter(
            models.NotificationReminder.correspondence_id == correspondence_id,
            models.NotificationReminder.user_id == user_id,
            models.NotificationReminder.stage == stage,
        )
        .first()
        is not None
    )


def _add_history(
    db: Session,
    correspondence_id: int,
    action: str,
    note: Optional[str] = None,
    user_id: Optional[int] = None,
    field_name: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
) -> None:
    db.add(
        models.CorrespondenceHistory(
            correspondence_id=correspondence_id,
            user_id=user_id,
            action=action,
            field_name=field_name,
            old_value=old_value,
            new_value=new_value,
            note=note,
        )
    )


def get_recipient_ids(db: Session, corr: models.Correspondence) -> Set[int]:
    """Исполнитель, создатель и активные получатели передач."""
    ids: Set[int] = set()
    if corr.executor_id:
        ids.add(corr.executor_id)
    if corr.created_by_id:
        ids.add(corr.created_by_id)

    transfers = (
        db.query(models.CorrespondenceTransfer)
        .filter(
            models.CorrespondenceTransfer.correspondence_id == corr.id,
            models.CorrespondenceTransfer.is_active == True,  # noqa: E712
        )
        .all()
    )
    for t in transfers:
        if t.transferred_to_id:
            ids.add(t.transferred_to_id)
    return ids


async def _deliver_to_user(
    db: Session,
    user: models.User,
    correspondence: Optional[models.Correspondence],
    subject: str,
    message: str,
    notification_type: str = "info",
    *,
    respect_quiet_hours: bool = True,
) -> Dict[str, Any]:
    """
    Отправка по каналам с учётом prefs и quiet hours.
    Browser всегда можно создать; email/sms пропускаются в тихие часы.
    """
    corr_id = correspondence.id if correspondence else None
    link = _letter_link(corr_id) if corr_id else _public_base_url()
    quiet = respect_quiet_hours and is_quiet_hours(user)
    result = {"email": None, "sms": None, "browser": None}

    # --- browser ---
    if _pref(user, "notify_browser", True):
        db.add(
            models.Notification(
                user_id=user.id,
                correspondence_id=corr_id,
                notification_type=notification_type or "browser",
                message=message,
                is_read=False,
            )
        )
        _log_delivery(db, user.id, corr_id, "browser", "sent", "inbox")
        result["browser"] = "sent"
    else:
        _log_delivery(db, user.id, corr_id, "browser", "skipped", "pref_disabled")
        result["browser"] = "skipped"

    # --- email ---
    if not _pref(user, "notify_email", True):
        _log_delivery(db, user.id, corr_id, "email", "skipped", "pref_disabled")
        result["email"] = "skipped"
    elif quiet:
        _log_delivery(db, user.id, corr_id, "email", "skipped", "quiet_hours")
        result["email"] = "skipped"
    elif not user.email:
        _log_delivery(db, user.id, corr_id, "email", "skipped", "no_email")
        result["email"] = "skipped"
    else:
        ok = await send_email(user.email, subject, message, link_url=link)
        _log_delivery(
            db,
            user.id,
            corr_id,
            "email",
            "sent" if ok else "failed",
            None if ok else "send_failed",
        )
        result["email"] = "sent" if ok else "failed"

    # --- sms ---
    if not _pref(user, "notify_sms", True):
        _log_delivery(db, user.id, corr_id, "sms", "skipped", "pref_disabled")
        result["sms"] = "skipped"
    elif quiet:
        _log_delivery(db, user.id, corr_id, "sms", "skipped", "quiet_hours")
        result["sms"] = "skipped"
    elif not user.phone_number:
        _log_delivery(db, user.id, corr_id, "sms", "skipped", "no_phone")
        result["sms"] = "skipped"
    else:
        ok = await send_sms(user.phone_number, message)
        _log_delivery(
            db,
            user.id,
            corr_id,
            "sms",
            "sent" if ok else "failed",
            None if ok else "send_failed",
        )
        result["sms"] = "sent" if ok else "failed"

    return result


async def notify_users_about_letter(
    db: Session,
    user_ids: Iterable[int],
    correspondence: models.Correspondence,
    subject: str,
    message: str,
    notification_type: str = "info",
) -> int:
    """Уведомить список пользователей о письме. Возвращает число обработанных пользователей."""
    count = 0
    for uid in set(user_ids):
        user = db.query(models.User).filter(models.User.id == uid, models.User.is_active == True).first()  # noqa: E712
        if not user:
            continue
        await _deliver_to_user(
            db,
            user,
            correspondence,
            subject,
            message,
            notification_type=notification_type,
        )
        count += 1
    db.commit()
    return count


async def notify_transfer(
    db: Session,
    correspondence: models.Correspondence,
    from_user: models.User,
    to_user: models.User,
    note: Optional[str] = None,
) -> None:
    """Уведомление о передаче письма."""
    note_part = f"\nПримечание: {note}" if note else ""
    subject = f"Вам передано письмо №{correspondence.incoming_number}"
    message = (
        f"Сотрудник {from_user.full_name} передал(а) вам письмо "
        f"№{correspondence.incoming_number} от {correspondence.sender}.{note_part}"
    )
    await _deliver_to_user(
        db,
        to_user,
        correspondence,
        subject,
        message,
        notification_type="transfer",
        respect_quiet_hours=True,
    )
    _add_history(
        db,
        correspondence.id,
        action="transferred",
        user_id=from_user.id,
        note=f"Передано пользователю {to_user.full_name}" + (f": {note}" if note else ""),
        new_value=str(to_user.id),
    )
    db.commit()


def _expire_overdue_letters(db: Session, today: date) -> int:
    """Статус COMPLETED не трогаем; waiting_external пропускаем; остальные с deadline < today → EXPIRED."""
    letters = (
        db.query(models.Correspondence)
        .filter(
            models.Correspondence.deadline < today,
            models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
            models.Correspondence.status != models.CorrespondenceStatus.EXPIRED,
            models.Correspondence.waiting_external == False,  # noqa: E712
        )
        .all()
    )
    expired = 0
    for corr in letters:
        old = corr.status.value if corr.status else None
        corr.status = models.CorrespondenceStatus.EXPIRED
        _add_history(
            db,
            corr.id,
            action="expired",
            field_name="status",
            old_value=old,
            new_value=models.CorrespondenceStatus.EXPIRED.value,
            note="Автоматически просрочено по истечении срока",
        )
        expired += 1
    if expired:
        db.commit()
    return expired


async def _notify_controllers_stale(db: Session, today: date) -> int:
    """Контролёрам — о письмах без обновлений N дней."""
    stale_days = _stale_letter_days()
    threshold = today - timedelta(days=stale_days)
    stale = (
        db.query(models.Correspondence)
        .filter(
            models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
            models.Correspondence.status != models.CorrespondenceStatus.EXPIRED,
            models.Correspondence.waiting_external == False,  # noqa: E712
        )
        .all()
    )
    stale_list = [
        c
        for c in stale
        if (c.updated_at.date() if c.updated_at else c.created_at.date() if c.created_at else today)
        <= threshold
    ]
    if not stale_list:
        return 0

    controllers = (
        db.query(models.User)
        .filter(
            models.User.is_active == True,  # noqa: E712
            models.User.role == models.UserRole.CONTROLLER,
        )
        .all()
    )
    if not controllers:
        # fallback: админы
        controllers = (
            db.query(models.User)
            .filter(models.User.is_active == True, models.User.is_admin == True)  # noqa: E712
            .all()
        )

    notified = 0
    stage = f"stale_{today.isoformat()}"
    for corr in stale_list:
        for user in controllers:
            if _reminder_exists(db, corr.id, user.id, stage):
                continue
            subject = f"Без движения: письмо №{corr.incoming_number}"
            updated = (
                corr.updated_at.strftime("%d.%m.%Y")
                if corr.updated_at
                else "неизвестно"
            )
            message = (
                f"Письмо №{corr.incoming_number} от {corr.sender} не обновлялось "
                f"более {stale_days} дн. (последнее изменение: {updated})."            )
            await _deliver_to_user(
                db,
                user,
                corr,
                subject,
                message,
                notification_type="stale",
            )
            db.add(
                models.NotificationReminder(
                    correspondence_id=corr.id,
                    user_id=user.id,
                    stage=stage,
                )
            )
            notified += 1
    if notified:
        db.commit()
    return notified


async def check_deadlines_and_notify(db: Session) -> dict:
    """
    Проверка сроков, авто-просрочка, напоминания, stale для контролёров.
    Возвращает словарь статистики. Не использует BackgroundTasks.
    """
    today = date.today()
    stats: Dict[str, Any] = {
        "expired": 0,
        "reminders_sent": 0,
        "reminders_skipped": 0,
        "stale_notified": 0,
        "errors": 0,
    }

    try:
        stats["expired"] = _expire_overdue_letters(db, today)
    except Exception as e:
        print(f"[ERR] Ошибка авто-просрочки: {e}")
        stats["errors"] += 1

    incomplete = (
        db.query(models.Correspondence)
        .filter(models.Correspondence.status != models.CorrespondenceStatus.COMPLETED)
        .all()
    )

    for corr in incomplete:
        if not corr.deadline:
            continue
        # Ждём внешний ответ — не давим напоминаниями о сроке
        if getattr(corr, "waiting_external", False):
            continue
        days_left = (corr.deadline - today).days
        recipient_ids = get_recipient_ids(db, corr)

        for user_id in recipient_ids:
            user = (
                db.query(models.User)
                .filter(models.User.id == user_id, models.User.is_active == True)  # noqa: E712
                .first()
            )
            if not user:
                continue

            stage = _stage_for_days(days_left, _reminder_days(user), today)
            if not stage:
                continue

            if _reminder_exists(db, corr.id, user.id, stage):
                stats["reminders_skipped"] += 1
                continue

            subject, message = _build_deadline_message(corr, days_left, stage)
            notif_type = "overdue" if stage.startswith("overdue") else stage

            try:
                await _deliver_to_user(
                    db,
                    user,
                    corr,
                    subject,
                    message,
                    notification_type=notif_type,
                )
                db.add(
                    models.NotificationReminder(
                        correspondence_id=corr.id,
                        user_id=user.id,
                        stage=stage,
                    )
                )
                # back-compat флаг
                corr.notification_sent = True
                db.commit()
                stats["reminders_sent"] += 1
            except Exception as e:
                db.rollback()
                print(f"[ERR] Ошибка уведомления user={user.id} corr={corr.id}: {e}")
                stats["errors"] += 1

    try:
        stats["stale_notified"] = await _notify_controllers_stale(db, today)
    except Exception as e:
        print(f"[ERR] Ошибка stale-уведомлений: {e}")
        stats["errors"] += 1

    return stats
