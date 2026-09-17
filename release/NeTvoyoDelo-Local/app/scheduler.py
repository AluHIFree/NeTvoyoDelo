"""
Планировщик уведомлений — единственный источник фоновых проверок сроков.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config_env import get_env, load_project_env

load_project_env()

scheduler = BackgroundScheduler()
is_running = False


def get_notification_hours():
    """Часы отправки из NOTIFICATION_SCHEDULE_HOURS (по умолчанию 9,12,16)."""
    hours_str = get_env("NOTIFICATION_SCHEDULE_HOURS", "9,12,16") or "9,12,16"
    try:
        hours = [int(h.strip()) for h in hours_str.split(",") if h.strip() != ""]
        return hours or [9, 12, 16]
    except Exception:
        return [9, 12, 16]


async def send_notifications_async():
    """Открывает SessionLocal и вызывает check_deadlines_and_notify."""
    global is_running

    if is_running:
        print("[WARN] Уведомления уже отправляются, пропускаем...")
        return {"skipped": True}

    is_running = True
    try:
        print(f"[INFO] Запуск проверки уведомлений в {datetime.now().strftime('%H:%M:%S')}")
        from app.database import SessionLocal
        from app.services.notification_service import check_deadlines_and_notify

        db = SessionLocal()
        try:
            result = await check_deadlines_and_notify(db)
            print(f"[OK] Результат проверки уведомлений: {result}")
            return result
        finally:
            db.close()
    except Exception as e:
        print(f"[ERR] Ошибка при отправке уведомлений: {e}")
        return {"error": str(e)}
    finally:
        is_running = False


def send_notifications_sync():
    """Синхронная обёртка для APScheduler."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(send_notifications_async())
    finally:
        loop.close()


def start_scheduler():
    """Запуск: часы из NOTIFICATION_SCHEDULE_HOURS + ежечасная проверка."""
    scheduler.remove_all_jobs()

    hours = get_notification_hours()
    for hour in hours:
        scheduler.add_job(
            send_notifications_sync,
            trigger=CronTrigger(hour=hour, minute=0),
            id=f"notification_job_{hour}",
            replace_existing=True,
        )
        print(f"[TIME] Запланирована отправка уведомлений в {hour:02d}:00")

    # Ежечасная проверка сроков / авто-просрочки (на случай пропуска cron-часа)
    scheduler.add_job(
        send_notifications_sync,
        trigger=CronTrigger(minute=5),
        id="hourly_deadline_check",
        replace_existing=True,
    )
    print("[TIME] Запланирована ежечасная проверка сроков (минута :05)")

    if not scheduler.running:
        scheduler.start()
        print("[START] Планировщик уведомлений запущен")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        print("[STOP] Планировщик уведомлений остановлен")


def get_next_run_time():
    jobs = scheduler.get_jobs()
    next_times = [job.next_run_time for job in jobs if job.next_run_time]
    if next_times:
        return min(next_times)
    return None
