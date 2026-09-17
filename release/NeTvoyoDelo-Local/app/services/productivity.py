"""
Продуктивность: стадии срока, план дня, панель успеха, streak.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app import models


# --- Стадии срока ---
STAGE_EARLY = "early"          # ещё рано
STAGE_START = "start"          # пора начинать
STAGE_CRITICAL = "critical"    # критично
STAGE_OVERDUE = "overdue"      # просрочено
STAGE_WAITING = "waiting"      # жду внешнего ответа
STAGE_DONE = "done"

STAGE_LABELS = {
    STAGE_EARLY: "Ещё рано",
    STAGE_START: "Пора начинать",
    STAGE_CRITICAL: "Критично",
    STAGE_OVERDUE: "Просрочено",
    STAGE_WAITING: "Жду ответа",
    STAGE_DONE: "Исполнено",
}


def days_until(deadline: Optional[date], today: Optional[date] = None) -> Optional[int]:
    if not deadline:
        return None
    return (deadline - (today or date.today())).days


def deadline_stage(
    deadline: Optional[date],
    *,
    today: Optional[date] = None,
    reminder_days: int = 5,
    status: Optional[models.CorrespondenceStatus] = None,
    waiting_external: bool = False,
) -> str:
    """Визуальная стадия срока."""
    if status == models.CorrespondenceStatus.COMPLETED:
        return STAGE_DONE
    if waiting_external:
        return STAGE_WAITING
    if not deadline:
        return STAGE_EARLY

    left = (deadline - (today or date.today())).days
    if left < 0:
        return STAGE_OVERDUE
    if left <= 1:
        return STAGE_CRITICAL
    if left <= max(2, min(reminder_days, 5)):
        return STAGE_START
    return STAGE_EARLY


def stage_hint(stage: str, days_left: Optional[int] = None) -> str:
    if stage == STAGE_WAITING:
        return "Ожидаем внешний ответ — не входит в личную нагрузку"
    if stage == STAGE_OVERDUE:
        overdue = abs(days_left) if days_left is not None else 0
        return f"Просрочено на {overdue} дн. — закройте или уточните статус"
    if stage == STAGE_CRITICAL:
        if days_left == 0:
            return "Срок сегодня — рекомендуем взять в работу"
        return "Срок завтра — рекомендуем взять в работу"
    if stage == STAGE_START:
        return "Пора начинать — рекомендуем взять в работу"
    if stage == STAGE_EARLY:
        return "Срок ещё не близко"
    return ""


def is_active_workload(corr: models.Correspondence) -> bool:
    """Письмо учитывается в личной нагрузке/просрочке."""
    if corr.status == models.CorrespondenceStatus.COMPLETED:
        return False
    if getattr(corr, "waiting_external", False):
        return False
    return True


def is_hot(corr: models.Correspondence, today: Optional[date] = None, reminder_days: int = 5) -> bool:
    if not is_active_workload(corr) or not corr.deadline:
        return False
    stage = deadline_stage(
        corr.deadline,
        today=today,
        reminder_days=reminder_days,
        status=corr.status,
        waiting_external=bool(getattr(corr, "waiting_external", False)),
    )
    return stage in (STAGE_CRITICAL, STAGE_OVERDUE)


def sort_key_urgency(corr: models.Correspondence, today: Optional[date] = None) -> tuple:
    today = today or date.today()
    left = days_until(corr.deadline, today)
    if left is None:
        left = 9999
    waiting = 1 if getattr(corr, "waiting_external", False) else 0
    # pending раньше in_progress при равном сроке — сначала «не начатые»
    status_rank = 0
    if corr.status == models.CorrespondenceStatus.PENDING:
        status_rank = 0
    elif corr.status == models.CorrespondenceStatus.TRANSFERRED:
        status_rank = 1
    elif corr.status == models.CorrespondenceStatus.IN_PROGRESS:
        status_rank = 2
    else:
        status_rank = 3
    return (waiting, left, status_rank, corr.incoming_number or "")


def _corr_item(
    corr: models.Correspondence,
    *,
    today: date,
    reminder_days: int,
    note: Optional[str] = None,
    bucket: str,
    can_take: bool = False,
) -> Dict[str, Any]:
    waiting = bool(getattr(corr, "waiting_external", False))
    stage = deadline_stage(
        corr.deadline,
        today=today,
        reminder_days=reminder_days,
        status=corr.status,
        waiting_external=waiting,
    )
    left = days_until(corr.deadline, today)
    return {
        "id": corr.id,
        "incoming_number": corr.incoming_number,
        "sender": corr.sender or "",
        "deadline": corr.deadline,
        "status": corr.status.value if corr.status else "",
        "days_left": left,
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, stage),
        "hint": stage_hint(stage, left),
        "note": note or "",
        "bucket": bucket,
        "can_take": can_take,
        "waiting_external": waiting,
        "link": f"/api/correspondences/{corr.id}",
    }


def build_work_day_plan(
    db: Session,
    user: models.User,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """
    План дня:
    - today: сделать сегодня (срок сегодня или просрочено, не waiting)
    - week: на этой неделе
    - waiting: жду ответа
    - unaccepted: передано мне — не принято
    + нагрузка и рекомендуемый порядок
    """
    today = today or date.today()
    reminder_days = getattr(user, "reminder_days_before", None) or 5
    try:
        reminder_days = max(1, int(reminder_days))
    except (TypeError, ValueError):
        reminder_days = 5

    week_end = today + timedelta(days=(6 - today.weekday()))  # воскресенье текущей недели
    if week_end < today:
        week_end = today

    # Заметки пользователя
    notes_rows = (
        db.query(models.WorkPlanNote)
        .filter(models.WorkPlanNote.user_id == user.id)
        .all()
    )
    notes_map = {n.correspondence_id: (n.text or "") for n in notes_rows}

    # Активные передачи мне
    transfers = (
        db.query(models.CorrespondenceTransfer)
        .filter(
            models.CorrespondenceTransfer.transferred_to_id == user.id,
            models.CorrespondenceTransfer.is_active == True,  # noqa: E712
        )
        .all()
    )
    transfer_corr_ids = {t.correspondence_id for t in transfers}

    # Мои письма как исполнитель + переданные мне
    q = db.query(models.Correspondence).filter(
        models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
    )
    if transfer_corr_ids:
        q = q.filter(
            (models.Correspondence.executor_id == user.id)
            | (models.Correspondence.id.in_(transfer_corr_ids))
        )
    else:
        q = q.filter(models.Correspondence.executor_id == user.id)
    my_letters = q.all()

    today_items: List[Dict[str, Any]] = []
    week_items: List[Dict[str, Any]] = []
    waiting_items: List[Dict[str, Any]] = []
    unaccepted_items: List[Dict[str, Any]] = []
    seen_unaccepted: set = set()

    for corr in my_letters:
        note = notes_map.get(corr.id, "")
        waiting = bool(getattr(corr, "waiting_external", False))
        can_take = corr.status in (
            models.CorrespondenceStatus.PENDING,
            models.CorrespondenceStatus.TRANSFERRED,
            models.CorrespondenceStatus.EXPIRED,
        )
        is_unaccepted = (
            corr.id in transfer_corr_ids
            and corr.status == models.CorrespondenceStatus.TRANSFERRED
        )

        # Непринятые передачи
        if is_unaccepted and corr.id not in seen_unaccepted:
            seen_unaccepted.add(corr.id)
            unaccepted_items.append(
                _corr_item(
                    corr,
                    today=today,
                    reminder_days=reminder_days,
                    note=note,
                    bucket="unaccepted",
                    can_take=True,
                )
            )

        if waiting:
            waiting_items.append(
                _corr_item(
                    corr,
                    today=today,
                    reminder_days=reminder_days,
                    note=note,
                    bucket="waiting",
                    can_take=can_take,
                )
            )
            continue

        # Непринятые — только в своей секции
        if is_unaccepted:
            continue

        if not corr.deadline:
            continue

        left = (corr.deadline - today).days
        if left <= 0:
            today_items.append(
                _corr_item(
                    corr,
                    today=today,
                    reminder_days=reminder_days,
                    note=note,
                    bucket="today",
                    can_take=can_take,
                )
            )
        elif corr.deadline <= week_end:
            week_items.append(
                _corr_item(
                    corr,
                    today=today,
                    reminder_days=reminder_days,
                    note=note,
                    bucket="week",
                    can_take=can_take,
                )
            )

    today_items.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else 9999, x["incoming_number"]))
    week_items.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else 9999, x["incoming_number"]))
    waiting_items.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else 9999, x["incoming_number"]))
    unaccepted_items.sort(key=lambda x: x["incoming_number"])

    # Нагрузка: активные без waiting_external
    workload_corrs = [c for c in my_letters if is_active_workload(c)]
    hot_corrs = [c for c in workload_corrs if is_hot(c, today, reminder_days)]

    # Рекомендуемый порядок: сегодня + неделя + непринятые, по срочности
    recommend_src = [c for c in my_letters if is_active_workload(c) or c.status == models.CorrespondenceStatus.TRANSFERRED]
    recommend_src = sorted(recommend_src, key=lambda c: sort_key_urgency(c, today))
    recommended = [
        _corr_item(
            c,
            today=today,
            reminder_days=reminder_days,
            note=notes_map.get(c.id, ""),
            bucket="recommended",
            can_take=c.status in (
                models.CorrespondenceStatus.PENDING,
                models.CorrespondenceStatus.TRANSFERRED,
                models.CorrespondenceStatus.EXPIRED,
            ),
        )
        for c in recommend_src[:8]
    ]

    load_total = len(workload_corrs)
    load_hot = len(hot_corrs)
    if load_total == 0:
        load_summary = "Сейчас нет активных писем в личной нагрузке"
    elif load_hot == 0:
        load_summary = f"У вас {load_total} {_plural_letters(load_total)} — горящих нет"
    else:
        load_summary = (
            f"У вас {load_total} {_plural_letters(load_total)}, "
            f"из них {load_hot} горящ{_plural_hot(load_hot)}"
        )

    return {
        "today": today_items,
        "week": week_items,
        "waiting": waiting_items,
        "unaccepted": unaccepted_items,
        "recommended": recommended,
        "load_total": load_total,
        "load_hot": load_hot,
        "load_summary": load_summary,
        "reminder_days": reminder_days,
    }


def _plural_letters(n: int) -> str:
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 11 <= n_abs <= 19:
        return "писем"
    if n1 == 1:
        return "письмо"
    if 2 <= n1 <= 4:
        return "письма"
    return "писем"


def _plural_hot(n: int) -> str:
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 11 <= n_abs <= 19:
        return "их"
    if n1 == 1:
        return "ее"
    return "их"


def _month_bounds(d: date) -> tuple[date, date]:
    start = d.replace(day=1)
    end = d.replace(day=monthrange(d.year, d.month)[1])
    return start, end


def _prev_month_day(d: date) -> date:
    if d.month == 1:
        return date(d.year - 1, 12, 1)
    return date(d.year, d.month - 1, 1)


def _on_time_stats(letters: Sequence[models.Correspondence]) -> Dict[str, Any]:
    completed = [
        c
        for c in letters
        if c.status == models.CorrespondenceStatus.COMPLETED and c.deadline and c.completed_at
    ]
    if not completed:
        return {"total": 0, "on_time": 0, "rate": None, "avg_days": None}

    on_time = 0
    durations = []
    for c in completed:
        done = c.completed_at.date() if hasattr(c.completed_at, "date") else c.completed_at
        if done <= c.deadline:
            on_time += 1
        # дни от поступления/создания до закрытия — используем received_date если есть
        start = c.received_date or c.incoming_date
        if start:
            durations.append((done - start).days)

    rate = round(on_time / len(completed) * 100, 1)
    avg_days = round(sum(durations) / len(durations), 1) if durations else None
    return {"total": len(completed), "on_time": on_time, "rate": rate, "avg_days": avg_days}


def _working_days_between(start: date, end: date) -> int:
    """Число рабочих дней (пн–пт) от start (не включая) до end (включая)."""
    if end <= start:
        return 0
    count = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def compute_no_overdue_streak(db: Session, user: models.User, today: Optional[date] = None) -> int:
    """
    Мягкий streak: рабочие дни без активной просрочки (waiting_external не считается).
    Если сейчас есть просрочка — 0. Иначе считаем от последней «плохой» даты.
    """
    today = today or date.today()

    active_overdue = (
        db.query(models.Correspondence)
        .filter(
            models.Correspondence.executor_id == user.id,
            models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
            models.Correspondence.deadline < today,
            models.Correspondence.waiting_external == False,  # noqa: E712
        )
        .count()
    )
    if active_overdue:
        return 0

    # Последнее позднее исполнение
    late_completed = (
        db.query(models.Correspondence)
        .filter(
            models.Correspondence.executor_id == user.id,
            models.Correspondence.status == models.CorrespondenceStatus.COMPLETED,
            models.Correspondence.completed_at.isnot(None),
            models.Correspondence.deadline.isnot(None),
        )
        .all()
    )
    last_bad: Optional[date] = None
    for c in late_completed:
        done = c.completed_at.date() if hasattr(c.completed_at, "date") else c.completed_at
        if done and c.deadline and done > c.deadline:
            if last_bad is None or done > last_bad:
                last_bad = done

    # Авто-просрочки по истории
    expired_hist = (
        db.query(models.CorrespondenceHistory)
        .join(
            models.Correspondence,
            models.Correspondence.id == models.CorrespondenceHistory.correspondence_id,
        )
        .filter(
            models.Correspondence.executor_id == user.id,
            models.CorrespondenceHistory.action == "expired",
        )
        .order_by(models.CorrespondenceHistory.created_at.desc())
        .limit(50)
        .all()
    )
    for h in expired_hist:
        d = h.created_at.date() if h.created_at else None
        if d and (last_bad is None or d > last_bad):
            last_bad = d

    if last_bad is None:
        # Нет плохих событий — от даты регистрации / 30 раб. дней макс
        created = user.created_at.date() if user.created_at else today - timedelta(days=60)
        return min(_working_days_between(created - timedelta(days=1), today), 30)

    return _working_days_between(last_bad, today)


def build_success_panel(db: Session, user: models.User, today: Optional[date] = None) -> Dict[str, Any]:
    """Панель успеха для профиля."""
    today = today or date.today()
    month_start, month_end = _month_bounds(today)
    prev_start, prev_end = _month_bounds(_prev_month_day(today))

    my_completed = (
        db.query(models.Correspondence)
        .filter(
            models.Correspondence.executor_id == user.id,
            models.Correspondence.status == models.CorrespondenceStatus.COMPLETED,
        )
        .all()
    )

    def in_period(c: models.Correspondence, start: date, end: date) -> bool:
        if not c.completed_at:
            return False
        d = c.completed_at.date() if hasattr(c.completed_at, "date") else c.completed_at
        return start <= d <= end

    this_month = [c for c in my_completed if in_period(c, month_start, month_end)]
    prev_month = [c for c in my_completed if in_period(c, prev_start, prev_end)]

    this_stats = _on_time_stats(this_month)
    prev_stats = _on_time_stats(prev_month)
    all_stats = _on_time_stats(my_completed)

    # Delta vs прошлый месяц
    delta = None
    delta_label = None
    if this_stats["rate"] is not None and prev_stats["rate"] is not None:
        delta = round(this_stats["rate"] - prev_stats["rate"], 1)
        if delta > 0:
            delta_label = f"+{delta}% вовремя к прошлому месяцу"
        elif delta < 0:
            delta_label = f"{delta}% вовремя к прошлому месяцу"
        else:
            delta_label = "как в прошлом месяце"
    elif this_stats["rate"] is not None and prev_stats["rate"] is None:
        delta_label = "первый месяц с закрытиями"
        delta = None

    # Среднее по отделу
    dept_avg = None
    if user.department:
        dept_users = (
            db.query(models.User)
            .filter(
                models.User.department == user.department,
                models.User.is_active == True,  # noqa: E712
            )
            .all()
        )
        dept_ids = [u.id for u in dept_users]
        if dept_ids:
            dept_completed = (
                db.query(models.Correspondence)
                .filter(
                    models.Correspondence.executor_id.in_(dept_ids),
                    models.Correspondence.status == models.CorrespondenceStatus.COMPLETED,
                    models.Correspondence.completed_at.isnot(None),
                )
                .all()
            )
            dept_stats = _on_time_stats(dept_completed)
            dept_avg = dept_stats["avg_days"]

    my_avg = all_stats["avg_days"]
    vs_dept = None
    if my_avg is not None and dept_avg is not None:
        diff = round(my_avg - dept_avg, 1)
        if diff < 0:
            vs_dept = f"на {abs(diff)} дн. быстрее отдела"
        elif diff > 0:
            vs_dept = f"на {diff} дн. дольше отдела"
        else:
            vs_dept = "как в среднем по отделу"

    streak = compute_no_overdue_streak(db, user, today)

    on_time_rate = this_stats["rate"] if this_stats["rate"] is not None else all_stats["rate"]

    return {
        "on_time_rate": on_time_rate,
        "on_time_count": this_stats["on_time"] if this_stats["total"] else all_stats["on_time"],
        "on_time_total": this_stats["total"] if this_stats["total"] else all_stats["total"],
        "on_time_period": "этот месяц" if this_stats["total"] else "за всё время",
        "month_delta": delta,
        "month_delta_label": delta_label,
        "avg_days": my_avg,
        "dept_avg_days": dept_avg,
        "vs_dept_label": vs_dept,
        "streak_days": streak,
        "streak_label": (
            f"{streak} рабоч{_plural_work(streak)} без просрочек"
            if streak > 0
            else "Сейчас есть просроченные — закройте их, чтобы начать серию"
        ),
    }


def _plural_work(n: int) -> str:
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 11 <= n_abs <= 19:
        return "их дней"
    if n1 == 1:
        return "ий день"
    if 2 <= n1 <= 4:
        return "их дня"
    return "их дней"


def completion_reward_message(deadline: Optional[date], completed_at: Optional[datetime] = None) -> Optional[str]:
    """Короткое сообщение после закрытия письма."""
    if not deadline:
        return "Письмо закрыто"
    done = (completed_at or datetime.utcnow()).date()
    delta = (deadline - done).days
    if delta > 0:
        return f"Закрыто за {delta} дн. до срока"
    if delta == 0:
        return "Закрыто в срок"
    return f"Закрыто с опозданием на {abs(delta)} дн."
