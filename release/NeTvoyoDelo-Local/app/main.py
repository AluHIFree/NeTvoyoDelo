from fastapi import FastAPI, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import and_
from datetime import date, datetime, timedelta
import os
from dotenv import load_dotenv

from app.scheduler import start_scheduler, stop_scheduler
from app.database import engine, get_db, Base
from app import models
from app.routers import auth, correspondences, admin, notifications, flows, requests, profile, updates
from app.auth import get_current_user, get_current_active_user
from app.migrate import run_migrations
from app.config_env import load_project_env
from app.services.productivity import build_work_day_plan

load_project_env(override=False)
# Создание таблиц и миграции схемы
Base.metadata.create_all(bind=engine)
run_migrations(engine)

# Инициализация FastAPI
from app.version import __version__ as APP_VERSION

app = FastAPI(
    title="Система учета входящей корреспонденции",
    description="Платформа для учета входящих писем с уведомлениями",
    version=APP_VERSION,
)

# CORS из env (не *)
_cors_raw = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000",
)
CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Статика и загрузки
os.makedirs("app/static", exist_ok=True)
os.makedirs("app/static/uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

app.include_router(auth.router)
app.include_router(correspondences.router)
app.include_router(admin.router)
app.include_router(notifications.router)
app.include_router(flows.router)
app.include_router(requests.router)
app.include_router(profile.router)
app.include_router(updates.router)


@app.on_event("startup")
async def startup_event():
    """Действия при запуске приложения"""
    from app.auth import create_first_admin
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        create_first_admin(db)
    finally:
        db.close()

    start_scheduler()
    print("[OK] Система учета входящей корреспонденции запущена")
    print("[BELL] notifications scheduler active")


@app.on_event("shutdown")
async def shutdown_event():
    """Действия при остановке приложения"""
    stop_scheduler()
    print("[BYE] Система остановлена")


def _accessible_correspondence_ids(db: Session, user: models.User) -> set:
    """ID писем, доступных пользователю (не админ)."""
    accessible_ids = set()

    executor_ids = [
        c.id
        for c in db.query(models.Correspondence)
        .filter(models.Correspondence.executor_id == user.id)
        .all()
    ]
    accessible_ids.update(executor_ids)

    creator_ids = [
        c.id
        for c in db.query(models.Correspondence)
        .filter(models.Correspondence.created_by_id == user.id)
        .all()
    ]
    accessible_ids.update(creator_ids)

    transferred_ids = [
        t.correspondence_id
        for t in db.query(models.CorrespondenceTransfer)
        .filter(
            and_(
                models.CorrespondenceTransfer.transferred_to_id == user.id,
                models.CorrespondenceTransfer.is_active == True,
            )
        )
        .all()
    ]
    accessible_ids.update(transferred_ids)

    # Контроллер / viewer — письма отдела
    if user.role in (models.UserRole.CONTROLLER, models.UserRole.VIEWER) and user.department:
        dept_user_ids = [
            u.id
            for u in db.query(models.User)
            .filter(
                models.User.department == user.department,
                models.User.is_active == True,
            )
            .all()
        ]
        if dept_user_ids:
            dept_corr_ids = [
                c.id
                for c in db.query(models.Correspondence)
                .filter(
                    (models.Correspondence.executor_id.in_(dept_user_ids))
                    | (models.Correspondence.created_by_id.in_(dept_user_ids))
                )
                .all()
            ]
            accessible_ids.update(dept_corr_ids)

    return accessible_ids


def _build_todo_feed(db: Session, user: models.User, today: date) -> list:
    """Лента дел: просроченные, сегодня, близкий срок, передачи, непрочитанные."""
    feed = []
    near_date = today + timedelta(days=getattr(user, "reminder_days_before", None) or 5)

    if user.is_admin:
        base_q = db.query(models.Correspondence)
    else:
        ids = _accessible_correspondence_ids(db, user)
        if not ids:
            base_q = db.query(models.Correspondence).filter(False)
        else:
            base_q = db.query(models.Correspondence).filter(
                models.Correspondence.id.in_(ids)
            )

    overdue = (
        base_q.filter(
            models.Correspondence.deadline < today,
            models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
            models.Correspondence.waiting_external == False,  # noqa: E712
        )
        .order_by(models.Correspondence.deadline.asc())
        .limit(8)
        .all()
    )
    for c in overdue:
        feed.append({
            "type": "overdue",
            "title": f"Просрочено: №{c.incoming_number}",
            "link": f"/api/correspondences/{c.id}",
            "meta": f"Срок {c.deadline.strftime('%d.%m.%Y') if c.deadline else '—'}",
        })

    due_today = (
        base_q.filter(
            models.Correspondence.deadline == today,
            models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
            models.Correspondence.waiting_external == False,  # noqa: E712
        )
        .order_by(models.Correspondence.incoming_number.asc())
        .limit(8)
        .all()
    )
    for c in due_today:
        feed.append({
            "type": "due_today",
            "title": f"Срок сегодня: №{c.incoming_number}",
            "link": f"/api/correspondences/{c.id}",
            "meta": c.sender or "",
        })

    near = (
        base_q.filter(
            models.Correspondence.deadline > today,
            models.Correspondence.deadline <= near_date,
            models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
            models.Correspondence.waiting_external == False,  # noqa: E712
        )
        .order_by(models.Correspondence.deadline.asc())
        .limit(8)
        .all()
    )
    for c in near:
        days = (c.deadline - today).days if c.deadline else 0
        feed.append({
            "type": "near_deadline",
            "title": f"Близкий срок: №{c.incoming_number}",
            "link": f"/api/correspondences/{c.id}",
            "meta": f"Осталось {days} дн.",
        })

    transfers = (
        db.query(models.CorrespondenceTransfer)
        .filter(
            models.CorrespondenceTransfer.transferred_to_id == user.id,
            models.CorrespondenceTransfer.is_active == True,
        )
        .order_by(models.CorrespondenceTransfer.transfer_date.desc())
        .limit(8)
        .all()
    )
    for t in transfers:
        corr = t.correspondence
        if not corr:
            continue
        feed.append({
            "type": "transferred",
            "title": f"Передано вам: №{corr.incoming_number}",
            "link": f"/api/correspondences/{corr.id}",
            "meta": (t.note or "")[:80],
        })

    unread = (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == user.id,
            models.Notification.is_read == False,
        )
        .order_by(models.Notification.sent_at.desc())
        .limit(8)
        .all()
    )
    for n in unread:
        link = "/notifications/"
        if n.correspondence_id:
            link = f"/api/correspondences/{n.correspondence_id}"
        feed.append({
            "type": "unread_notification",
            "title": (n.message or "Уведомление")[:100],
            "link": link,
            "meta": n.notification_type or "browser",
        })

    return feed


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Session = Depends(get_db)
):
    """Главная страница - дашборд"""
    current_user = await get_current_user(request, db)

    if current_user is None:
        return RedirectResponse(url="/api/auth/login", status_code=302)

    from app.dependencies import get_dashboard_stats
    stats = await get_dashboard_stats(request, db)

    today = date.today()
    near_deadline_date = today + timedelta(days=5)

    if current_user.is_admin:
        near_deadline_correspondences = db.query(models.Correspondence).filter(
            models.Correspondence.deadline <= near_deadline_date,
            models.Correspondence.deadline >= today,
            models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
            models.Correspondence.waiting_external == False,  # noqa: E712
        ).order_by(models.Correspondence.deadline.asc()).limit(10).all()
        recent_correspondences = db.query(models.Correspondence).order_by(
            models.Correspondence.created_at.desc()
        ).limit(5).all()
    else:
        accessible_ids = _accessible_correspondence_ids(db, current_user)
        if accessible_ids:
            near_deadline_correspondences = db.query(models.Correspondence).filter(
                models.Correspondence.id.in_(accessible_ids),
                models.Correspondence.deadline <= near_deadline_date,
                models.Correspondence.deadline >= today,
                models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
                models.Correspondence.waiting_external == False,  # noqa: E712
            ).order_by(models.Correspondence.deadline.asc()).limit(10).all()
            recent_correspondences = db.query(models.Correspondence).filter(
                models.Correspondence.id.in_(accessible_ids)
            ).order_by(models.Correspondence.created_at.desc()).limit(5).all()
        else:
            near_deadline_correspondences = []
            recent_correspondences = []

    unread_notifications_count = db.query(models.Notification).filter(
        models.Notification.user_id == current_user.id,
        models.Notification.is_read == False
    ).count()

    work_plan = build_work_day_plan(db, current_user, today)
    todo_feed = _build_todo_feed(db, current_user, today)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "current_user": current_user,
            "stats": stats,
            "near_deadline_correspondences": near_deadline_correspondences,
            "recent_correspondences": recent_correspondences,
            "unread_notifications_count": unread_notifications_count,
            "todo_feed": todo_feed,
            "work_plan": work_plan,
            "today": today
        }
    )


@app.post("/dashboard/plan-note/{correspondence_id}")
async def save_plan_note(
    request: Request,
    correspondence_id: int,
    note_text: str = Form(""),
    db: Session = Depends(get_db),
):
    """Сохранить заметку к письму в плане дня."""
    current_user = await get_current_active_user(request, db)
    corr = db.query(models.Correspondence).filter(
        models.Correspondence.id == correspondence_id
    ).first()
    if not corr:
        return RedirectResponse(url="/", status_code=303)

    text = (note_text or "").strip()
    existing = (
        db.query(models.WorkPlanNote)
        .filter(
            models.WorkPlanNote.user_id == current_user.id,
            models.WorkPlanNote.correspondence_id == correspondence_id,
        )
        .first()
    )
    if not text:
        if existing:
            db.delete(existing)
            db.commit()
    elif existing:
        existing.text = text
        existing.updated_at = datetime.utcnow()
        db.commit()
    else:
        db.add(
            models.WorkPlanNote(
                user_id=current_user.id,
                correspondence_id=correspondence_id,
                text=text,
            )
        )
        db.commit()

    return RedirectResponse(url="/#work-plan", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page_redirect(request: Request):
    """Перенаправление на страницу логина"""
    return RedirectResponse(url="/api/auth/login", status_code=302)


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request, db: Session = Depends(get_db)):
    """Интерактивная документация платформы"""
    current_user = await get_current_user(request, db)
    return templates.TemplateResponse(
        "help.html",
        {
            "request": request,
            "current_user": current_user,
        },
    )


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """Обработка 404 ошибки"""
    return templates.TemplateResponse(
        "404.html",
        {"request": request},
        status_code=404
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    """Обработка 500 ошибки"""
    return templates.TemplateResponse(
        "500.html",
        {"request": request, "error": str(exc)},
        status_code=500
    )
