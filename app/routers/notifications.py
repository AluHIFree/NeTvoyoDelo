"""
Тонкий роутер уведомлений: страницы и API.
Логика отправки — в app.services.notification_service.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.dependencies import get_current_active_user
from app.services.notification_service import check_deadlines_and_notify

router = APIRouter(prefix="/notifications", tags=["notifications"])
templates = Jinja2Templates(directory="app/templates")


def _notification_link(n: models.Notification) -> Optional[str]:
    if n.correspondence_id:
        return f"/api/correspondences/{n.correspondence_id}"
    return None


def _serialize_notification(n: models.Notification) -> dict:
    return {
        "id": n.id,
        "message": n.message,
        "sent_at": n.sent_at.isoformat() if n.sent_at else None,
        "type": n.notification_type,
        "correspondence_id": n.correspondence_id,
        "link": _notification_link(n),
        "is_read": n.is_read,
    }


@router.get("/check", response_class=HTMLResponse)
async def check_notifications_page(
    request: Request,
    urgent: Optional[bool] = Query(None, description="Только срочные (≤1 дня)"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
):
    """Страница центра уведомлений (без автоотправки при заходе)."""
    unread_notifications = (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == current_user.id,
            models.Notification.is_read == False,  # noqa: E712
        )
        .order_by(models.Notification.sent_at.desc())
        .all()
    )

    today = date.today()
    near_deadline_date = today + timedelta(days=5)

    query = db.query(models.Correspondence).filter(
        models.Correspondence.deadline <= near_deadline_date,
        models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
        models.Correspondence.waiting_external == False,  # noqa: E712
    )
    if urgent:
        query = query.filter(models.Correspondence.deadline <= today + timedelta(days=1))
    else:
        query = query.filter(models.Correspondence.deadline >= today)

    near_deadline_correspondences = query.order_by(models.Correspondence.deadline.asc()).all()

    accessible = []
    for corr in near_deadline_correspondences:
        if current_user.is_admin:
            accessible.append(corr)
            continue
        if corr.executor_id == current_user.id or corr.created_by_id == current_user.id:
            accessible.append(corr)
            continue
        transfer = (
            db.query(models.CorrespondenceTransfer)
            .filter(
                and_(
                    models.CorrespondenceTransfer.correspondence_id == corr.id,
                    models.CorrespondenceTransfer.transferred_to_id == current_user.id,
                    models.CorrespondenceTransfer.is_active == True,  # noqa: E712
                )
            )
            .first()
        )
        if transfer:
            accessible.append(corr)

    from app.scheduler import get_next_run_time

    next_run = get_next_run_time()
    next_run_str = next_run.strftime("%d.%m.%Y %H:%M") if next_run else "не запланировано"

    return templates.TemplateResponse(
        "notifications_page.html",
        {
            "request": request,
            "current_user": current_user,
            "notified_count": 0,
            "unread_notifications": unread_notifications,
            "near_deadline_correspondences": accessible,
            "today": today,
            "next_run": next_run_str,
            "urgent": bool(urgent),
        },
    )


@router.get("/api/unread-count")
async def get_unread_notifications_count(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
):
    count = (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == current_user.id,
            models.Notification.is_read == False,  # noqa: E712
        )
        .count()
    )
    return {"unread_count": count}


@router.get("/api/unread")
async def get_unread_notifications(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
):
    notifications = (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == current_user.id,
            models.Notification.is_read == False,  # noqa: E712
        )
        .order_by(models.Notification.sent_at.desc())
        .limit(20)
        .all()
    )
    return [_serialize_notification(n) for n in notifications]


@router.post("/{notification_id}/mark-read")
async def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
):
    notification = (
        db.query(models.Notification)
        .filter(
            models.Notification.id == notification_id,
            models.Notification.user_id == current_user.id,
        )
        .first()
    )
    if not notification:
        raise HTTPException(status_code=404, detail="Уведомление не найдено")

    notification.is_read = True
    db.commit()
    return {"status": "success"}


@router.post("/mark-all-read")
async def mark_all_notifications_read(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
):
    db.query(models.Notification).filter(
        models.Notification.user_id == current_user.id,
        models.Notification.is_read == False,  # noqa: E712
    ).update({"is_read": True})
    db.commit()
    return {"status": "success"}


@router.post("/trigger-check")
async def trigger_notification_check(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
):
    """Ручной запуск проверки сроков (для админов — полная проверка)."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Только администратор может запускать проверку")

    result = await check_deadlines_and_notify(db)
    return {
        "status": "success",
        "message": "Проверка уведомлений выполнена",
        "result": result,
    }
