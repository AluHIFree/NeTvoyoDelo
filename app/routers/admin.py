from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime

from app.database import get_db
from app import models, schemas, auth
from app.dependencies import admin_required, list_department_names
from app.auth import get_password_hash

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


def _dept_stats(db: Session) -> dict:
    """Счётчики пользователей по всем отделам из справочника."""
    names = list_department_names(db, active_only=False)
    counts = {}
    for name in names:
        counts[name] = db.query(models.User).filter(models.User.department == name).count()
    return {"by_department": counts}


@router.get("/", response_class=HTMLResponse)
async def admin_panel(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required)
):
    """Главная страница админ-панели"""
    total_users = db.query(models.User).count()
    active_users = db.query(models.User).filter(models.User.is_active == True).count()
    blocked_users = db.query(models.User).filter(models.User.is_active == False).count()
    admin_count = db.query(models.User).filter(models.User.is_admin == True).count()

    dept = _dept_stats(db)

    total_correspondences = db.query(models.Correspondence).count()
    completed_correspondences = db.query(models.Correspondence).filter(
        models.Correspondence.status == models.CorrespondenceStatus.COMPLETED
    ).count()
    pending_correspondences = db.query(models.Correspondence).filter(
        models.Correspondence.status == models.CorrespondenceStatus.PENDING
    ).count()

    stats = {
        "total_users": total_users,
        "active_users": active_users,
        "blocked_users": blocked_users,
        "admin_count": admin_count,
        "by_department": dept["by_department"],
        "total_correspondences": total_correspondences,
        "completed_correspondences": completed_correspondences,
        "pending_correspondences": pending_correspondences,
    }

    return templates.TemplateResponse(
        "admin_panel.html",
        {
            "request": request,
            "current_user": current_user,
            "stats": stats
        }
    )


@router.get("/users", response_class=HTMLResponse)
async def manage_users(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required),
    department: Optional[str] = None,
    is_active: Optional[str] = None
):
    """Управление пользователями"""
    query = db.query(models.User)

    if department:
        query = query.filter(models.User.department == department)

    if is_active == "active":
        query = query.filter(models.User.is_active == True)
    elif is_active == "blocked":
        query = query.filter(models.User.is_active == False)

    users = query.order_by(models.User.created_at.desc()).all()
    departments = list_department_names(db, active_only=False)

    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request,
            "current_user": current_user,
            "users": users,
            "departments": departments,
            "roles": list(models.UserRole.ALL),
            "selected_department": department,
            "selected_status": is_active
        }
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def view_user_details(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required)
):
    """Просмотр деталей пользователя"""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    assigned_count = db.query(models.Correspondence).filter(
        models.Correspondence.executor_id == user_id
    ).count()
    completed_count = db.query(models.Correspondence).filter(
        models.Correspondence.executor_id == user_id,
        models.Correspondence.status == models.CorrespondenceStatus.COMPLETED
    ).count()
    created_count = db.query(models.Correspondence).filter(
        models.Correspondence.created_by_id == user_id
    ).count()
    transferred_to_count = db.query(models.CorrespondenceTransfer).filter(
        models.CorrespondenceTransfer.transferred_to_id == user_id,
        models.CorrespondenceTransfer.is_active == True
    ).count()
    transferred_by_count = db.query(models.CorrespondenceTransfer).filter(
        models.CorrespondenceTransfer.transferred_by_id == user_id
    ).count()

    user_stats = {
        "assigned_count": assigned_count,
        "completed_count": completed_count,
        "created_count": created_count,
        "transferred_to_count": transferred_to_count,
        "transferred_by_count": transferred_by_count
    }

    return templates.TemplateResponse(
        "admin_user_detail.html",
        {
            "request": request,
            "current_user": current_user,
            "target_user": user,
            "stats": user_stats,
            "departments": list_department_names(db, active_only=False),
            "roles": list(models.UserRole.ALL),
        }
    )


@router.post("/users/{user_id}/block")
async def block_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required)
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot block yourself")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = False
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/unblock")
async def unblock_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required)
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = True
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/make-admin")
async def make_admin(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required)
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_admin = True
    db.commit()
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/remove-admin")
async def remove_admin(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required)
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot remove your own admin rights")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_admin = False
    db.commit()
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required)
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    db.query(models.CorrespondenceTransfer).filter(
        (models.CorrespondenceTransfer.transferred_by_id == user_id) |
        (models.CorrespondenceTransfer.transferred_to_id == user_id)
    ).delete()
    db.query(models.Correspondence).filter(
        models.Correspondence.executor_id == user_id
    ).update({"executor_id": None})
    admin_user = db.query(models.User).filter(models.User.is_admin == True).first()
    if admin_user:
        db.query(models.Correspondence).filter(
            models.Correspondence.created_by_id == user_id
        ).update({"created_by_id": admin_user.id})
    db.query(models.Notification).filter(
        models.Notification.user_id == user_id
    ).delete()
    db.delete(user)
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=status.HTTP_303_SEE_OTHER)


def _normalize_role(role: Optional[str]) -> str:
    if role and role in models.UserRole.ALL:
        return role
    return models.UserRole.EXECUTOR


@router.post("/users/create")
async def create_user_by_admin(
    request: Request,
    email: str = Form(...),
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    department: str = Form(...),
    phone_number: Optional[str] = Form(None),
    role: Optional[str] = Form("executor"),
    is_admin: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required)
):
    """Создание пользователя администратором (department — строка)."""
    departments = list_department_names(db, active_only=False)
    roles = list(models.UserRole.ALL)

    existing_user = auth.get_user_by_username(db, username)
    if existing_user:
        return templates.TemplateResponse(
            "admin_user_detail.html",
            {
                "request": request,
                "current_user": current_user,
                "target_user": None,
                "stats": {},
                "departments": departments,
                "roles": roles,
                "error": "Пользователь с таким именем уже существует"
            },
            status_code=status.HTTP_400_BAD_REQUEST
        )

    existing_email = auth.get_user_by_email(db, email)
    if existing_email:
        return templates.TemplateResponse(
            "admin_user_detail.html",
            {
                "request": request,
                "current_user": current_user,
                "target_user": None,
                "stats": {},
                "departments": departments,
                "roles": roles,
                "error": "Пользователь с таким email уже существует"
            },
            status_code=status.HTTP_400_BAD_REQUEST
        )

    dept_name = department.strip()
    if dept_name not in departments and departments:
        dept_name = departments[0]
    elif not dept_name:
        dept_name = models.Department.SCHOOL_DEPARTMENT.value

    hashed_password = get_password_hash(password)
    new_user = models.User(
        email=email,
        username=username,
        full_name=full_name,
        hashed_password=hashed_password,
        department=dept_name,
        role=_normalize_role(role),
        is_active=True,
        is_admin=is_admin,
        phone_number=phone_number
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return RedirectResponse(
        url=f"/admin/users/{new_user.id}",
        status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/users/{user_id}/update")
async def update_user_by_admin(
    request: Request,
    user_id: int,
    email: str = Form(...),
    username: str = Form(...),
    full_name: str = Form(...),
    department: str = Form(...),
    phone_number: Optional[str] = Form(None),
    role: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required)
):
    """Обновление данных пользователя (department и role — строки)."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    departments = list_department_names(db, active_only=False)
    roles = list(models.UserRole.ALL)

    if user.username != username:
        existing = auth.get_user_by_username(db, username)
        if existing:
            return templates.TemplateResponse(
                "admin_user_detail.html",
                {
                    "request": request,
                    "current_user": current_user,
                    "target_user": user,
                    "stats": {},
                    "departments": departments,
                    "roles": roles,
                    "error": "Пользователь с таким именем уже существует"
                },
                status_code=status.HTTP_400_BAD_REQUEST
            )

    if user.email != email:
        existing = auth.get_user_by_email(db, email)
        if existing:
            return templates.TemplateResponse(
                "admin_user_detail.html",
                {
                    "request": request,
                    "current_user": current_user,
                    "target_user": user,
                    "stats": {},
                    "departments": departments,
                    "roles": roles,
                    "error": "Пользователь с таким email уже существует"
                },
                status_code=status.HTTP_400_BAD_REQUEST
            )

    dept_name = department.strip()
    if dept_name not in departments and departments:
        dept_name = user.department

    user.email = email
    user.username = username
    user.full_name = full_name
    user.department = dept_name
    user.phone_number = phone_number
    if role is not None:
        user.role = _normalize_role(role)

    db.commit()

    return RedirectResponse(
        url=f"/admin/users/{user_id}",
        status_code=status.HTTP_303_SEE_OTHER
    )


# --- Отделы ---
@router.get("/departments", response_class=HTMLResponse)
async def departments_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required),
):
    departments = (
        db.query(models.DepartmentEntity)
        .order_by(models.DepartmentEntity.name)
        .all()
    )
    return templates.TemplateResponse(
        "admin_departments.html",
        {
            "request": request,
            "current_user": current_user,
            "departments": departments,
        },
    )


@router.post("/departments", response_class=HTMLResponse)
async def create_or_toggle_department(
    request: Request,
    name: Optional[str] = Form(None),
    department_id: Optional[int] = Form(None),
    action: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required),
):
    """Создание отдела, переключение is_active или удаление."""
    if action == "toggle" and department_id:
        dept = (
            db.query(models.DepartmentEntity)
            .filter(models.DepartmentEntity.id == department_id)
            .first()
        )
        if dept:
            dept.is_active = not dept.is_active
            db.commit()
        return RedirectResponse(url="/admin/departments", status_code=status.HTTP_303_SEE_OTHER)

    if action == "delete" and department_id:
        dept = (
            db.query(models.DepartmentEntity)
            .filter(models.DepartmentEntity.id == department_id)
            .first()
        )
        if not dept:
            return RedirectResponse(url="/admin/departments", status_code=status.HTTP_303_SEE_OTHER)

        users_count = (
            db.query(models.User)
            .filter(models.User.department == dept.name)
            .count()
        )
        if users_count > 0:
            departments = (
                db.query(models.DepartmentEntity)
                .order_by(models.DepartmentEntity.name)
                .all()
            )
            return templates.TemplateResponse(
                "admin_departments.html",
                {
                    "request": request,
                    "current_user": current_user,
                    "departments": departments,
                    "error": (
                        f"Нельзя удалить «{dept.name}»: к отделу привязано "
                        f"{users_count} пользователь(ей). Сначала переназначьте их."
                    ),
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        db.delete(dept)
        db.commit()
        return RedirectResponse(url="/admin/departments", status_code=status.HTTP_303_SEE_OTHER)

    if name and name.strip():
        existing = (
            db.query(models.DepartmentEntity)
            .filter(models.DepartmentEntity.name == name.strip())
            .first()
        )
        if not existing:
            db.add(
                models.DepartmentEntity(
                    name=name.strip(),
                    is_active=True,
                    created_at=datetime.utcnow(),
                )
            )
            db.commit()

    return RedirectResponse(url="/admin/departments", status_code=status.HTTP_303_SEE_OTHER)


# --- Отчёты ---
@router.get("/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required),
):
    from datetime import date as date_cls
    from app.dependencies import correspondence_department

    today = date_cls.today()
    all_letters = db.query(models.Correspondence).all()

    total = len(all_letters)
    by_status = {s.value: 0 for s in models.CorrespondenceStatus}
    completed_list = []
    overdue = 0
    in_progress = 0
    pending = 0
    transferred = 0

    for c in all_letters:
        status_val = c.status.value if c.status else "—"
        by_status[status_val] = by_status.get(status_val, 0) + 1

        if c.status == models.CorrespondenceStatus.COMPLETED:
            completed_list.append(c)
        elif c.status == models.CorrespondenceStatus.EXPIRED:
            overdue += 1
        elif c.status == models.CorrespondenceStatus.IN_PROGRESS:
            in_progress += 1
        elif c.status == models.CorrespondenceStatus.PENDING:
            pending += 1
        elif c.status == models.CorrespondenceStatus.TRANSFERRED:
            transferred += 1

    # Письма с истёкшим сроком, но не исполненные
    past_deadline = sum(
        1
        for c in all_letters
        if c.status != models.CorrespondenceStatus.COMPLETED
        and c.deadline
        and c.deadline < today
    )

    total_completed = len(completed_list)
    on_time = 0
    completion_days = []
    for c in completed_list:
        done_date = None
        if c.completed_at:
            done_date = c.completed_at.date() if hasattr(c.completed_at, "date") else c.completed_at
        elif c.updated_at:
            done_date = c.updated_at.date()
        if done_date and c.deadline and done_date <= c.deadline:
            on_time += 1
        if done_date and c.received_date:
            completion_days.append((done_date - c.received_date).days)

    on_time_pct = round((on_time / total_completed * 100) if total_completed else 0, 1)
    avg_days = round(sum(completion_days) / len(completion_days), 1) if completion_days else 0

    # По отделам
    dept_names = list_department_names(db, active_only=False)
    letter_depts = {c.id: correspondence_department(db, c) for c in all_letters}
    by_department = []
    for name in dept_names:
        dept_letters = [c for c in all_letters if letter_depts.get(c.id) == name]
        dept_done = sum(
            1 for c in dept_letters if c.status == models.CorrespondenceStatus.COMPLETED
        )
        dept_open = len(dept_letters) - dept_done
        dept_overdue = sum(
            1
            for c in dept_letters
            if c.status != models.CorrespondenceStatus.COMPLETED
            and c.deadline
            and c.deadline < today
        )
        by_department.append({
            "department": name,
            "total": len(dept_letters),
            "completed": dept_done,
            "open": dept_open,
            "overdue": dept_overdue,
            "completion_pct": round((dept_done / len(dept_letters) * 100) if dept_letters else 0, 1),
        })
    known = set(dept_names)
    no_dept = [c for c in all_letters if letter_depts.get(c.id) not in known]
    if no_dept:
        done = sum(1 for c in no_dept if c.status == models.CorrespondenceStatus.COMPLETED)
        by_department.append({
            "department": "Без отдела / прочее",
            "total": len(no_dept),
            "completed": done,
            "open": len(no_dept) - done,
            "overdue": sum(
                1 for c in no_dept
                if c.status != models.CorrespondenceStatus.COMPLETED
                and c.deadline and c.deadline < today
            ),
            "completion_pct": round((done / len(no_dept) * 100) if no_dept else 0, 1),
        })
    by_department.sort(key=lambda x: x["total"], reverse=True)

    # Нагрузка по сотрудникам
    users = db.query(models.User).filter(models.User.is_active == True).all()
    load_by_employee = []
    for u in users:
        assigned = db.query(models.Correspondence).filter(
            models.Correspondence.executor_id == u.id
        ).count()
        done = db.query(models.Correspondence).filter(
            models.Correspondence.executor_id == u.id,
            models.Correspondence.status == models.CorrespondenceStatus.COMPLETED,
        ).count()
        open_count = assigned - done
        overdue_emp = db.query(models.Correspondence).filter(
            models.Correspondence.executor_id == u.id,
            models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
            models.Correspondence.deadline < today,
        ).count()
        load_by_employee.append({
            "id": u.id,
            "full_name": u.full_name,
            "department": u.department,
            "assigned": assigned,
            "completed": done,
            "open": open_count,
            "overdue": overdue_emp,
            "completion_pct": round((done / assigned * 100) if assigned else 0, 1),
        })
    load_by_employee.sort(key=lambda x: (x["open"], x["overdue"]), reverse=True)

    # Сводка ключевых показателей
    report_stats = {
        "total": total,
        "completed": total_completed,
        "overdue": overdue if overdue else past_deadline,
        "in_progress": in_progress + pending,
        "pending": pending,
        "transferred": transferred,
        "past_deadline": past_deadline,
        "on_time_pct": on_time_pct,
        "on_time_count": on_time,
        "avg_completion_days": avg_days,
    }

    status_rows = [
        {"label": name, "value": count}
        for name, count in by_status.items()
        if count > 0 or name in {s.value for s in models.CorrespondenceStatus}
    ]

    summary_rows = [
        {"label": "Всего документов", "value": total},
        {"label": "Исполнено", "value": total_completed},
        {"label": "Исполнено в срок", "value": f"{on_time} ({on_time_pct}%)"},
        {"label": "Средний срок исполнения, дней", "value": avg_days},
        {"label": "С истёкшим сроком (не закрыты)", "value": past_deadline},
        {"label": "В ожидании", "value": pending},
        {"label": "В работе", "value": in_progress},
        {"label": "Передано", "value": transferred},
        {"label": "Просрочено (статус)", "value": overdue},
    ]

    return templates.TemplateResponse(
        "admin_reports.html",
        {
            "request": request,
            "current_user": current_user,
            "report_stats": report_stats,
            "by_department": by_department,
            "by_status": status_rows,
            "load_by_employee": load_by_employee,
            "summary_rows": summary_rows,
            "on_time_pct": on_time_pct,
            "avg_completion_days": avg_days,
            "total_completed": total_completed,
            "on_time_count": on_time,
            "generated_at": datetime.utcnow(),
        },
    )


# --- Логи доставки ---
@router.get("/delivery-logs", response_class=HTMLResponse)
async def delivery_logs_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(admin_required),
):
    logs = (
        db.query(models.NotificationDeliveryLog)
        .order_by(models.NotificationDeliveryLog.created_at.desc())
        .limit(200)
        .all()
    )
    return templates.TemplateResponse(
        "admin_delivery_logs.html",
        {
            "request": request,
            "current_user": current_user,
            "logs": logs,
        },
    )
