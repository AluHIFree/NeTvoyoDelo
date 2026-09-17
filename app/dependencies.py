from typing import List, Optional
from fastapi import Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from datetime import date, timedelta

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user, get_current_admin_user, get_current_active_user


def list_department_names(db: Session, active_only: bool = True) -> List[str]:
    """Названия отделов из DepartmentEntity (fallback на Enum)."""
    q = db.query(models.DepartmentEntity)
    if active_only:
        q = q.filter(models.DepartmentEntity.is_active == True)
    depts = q.order_by(models.DepartmentEntity.name).all()
    if depts:
        return [d.name for d in depts]
    return [d.value for d in models.Department]


def get_user_repository(db: Session = Depends(get_db)):
    """Получение репозитория пользователей"""
    return db


async def get_current_user_department(request: Request, db: Session = Depends(get_db)):
    """Получение отдела текущего пользователя"""
    current_user = await get_current_active_user(request, db)
    return current_user.department


def check_user_belongs_to_department(
    user_id: int,
    department: str,
    db: Session = Depends(get_db)
):
    """Проверка, принадлежит ли пользователь к отделу"""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.department != department:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User does not belong to {department}"
        )
    return user


def get_department_users(
    department: str,
    db: Session
) -> List[models.User]:
    """Получение всех активных пользователей отдела (department — строка)."""
    return db.query(models.User).filter(
        and_(
            models.User.department == department,
            models.User.is_active == True
        )
    ).all()


def _user_department(db: Session, user_id: Optional[int]) -> Optional[str]:
    if not user_id:
        return None
    u = db.query(models.User).filter(models.User.id == user_id).first()
    return u.department if u else None


def correspondence_department(db: Session, correspondence: models.Correspondence) -> Optional[str]:
    """Отдел письма = отдел исполнителя, иначе создателя."""
    return (
        _user_department(db, correspondence.executor_id)
        or _user_department(db, correspondence.created_by_id)
    )


def get_correspondence_or_404(
    correspondence_id: int,
    db: Session = Depends(get_db)
) -> models.Correspondence:
    """Получение письма или 404 ошибка"""
    correspondence = db.query(models.Correspondence).filter(
        models.Correspondence.id == correspondence_id
    ).first()

    if not correspondence:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Correspondence with id {correspondence_id} not found"
        )
    return correspondence


async def check_correspondence_access(
    request: Request,
    correspondence_id: int,
    db: Session = Depends(get_db)
) -> models.Correspondence:
    """
    Доступ: admin OR executor OR creator OR transfer
    OR (controller/viewer и тот же отдел, что у исполнителя/создателя письма).
    """
    current_user = await get_current_active_user(request, db)
    correspondence = get_correspondence_or_404(correspondence_id, db)

    if current_user.is_admin:
        return correspondence

    if correspondence.executor_id == current_user.id:
        return correspondence

    if correspondence.created_by_id == current_user.id:
        return correspondence

    active_transfer = db.query(models.CorrespondenceTransfer).filter(
        and_(
            models.CorrespondenceTransfer.correspondence_id == correspondence.id,
            models.CorrespondenceTransfer.transferred_to_id == current_user.id,
            models.CorrespondenceTransfer.is_active == True
        )
    ).first()

    if active_transfer:
        return correspondence

    letter_dept = correspondence_department(db, correspondence)
    if (
        letter_dept
        and current_user.department == letter_dept
        and current_user.role in (models.UserRole.CONTROLLER, models.UserRole.VIEWER)
    ):
        return correspondence

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You don't have access to this correspondence"
    )


def enforce_not_viewer(user: models.User) -> None:
    """Viewer — только чтение."""
    if user.role == models.UserRole.VIEWER and not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewer role is read-only"
        )


async def get_user_correspondences(
    request: Request,
    db: Session = Depends(get_db),
    status_filter: Optional[str] = None,
    days_until_deadline: Optional[int] = None,
    q: Optional[str] = None,
    urgent: Optional[bool] = None,
    executor_id: Optional[int] = None,
) -> List[models.Correspondence]:
    """Получение писем пользователя с фильтрацией"""
    current_user = await get_current_active_user(request, db)

    query = db.query(models.Correspondence)

    if not current_user.is_admin:
        personal = or_(
            models.Correspondence.executor_id == current_user.id,
            models.Correspondence.created_by_id == current_user.id,
            models.Correspondence.id.in_(
                db.query(models.CorrespondenceTransfer.correspondence_id).filter(
                    and_(
                        models.CorrespondenceTransfer.transferred_to_id == current_user.id,
                        models.CorrespondenceTransfer.is_active == True
                    )
                )
            )
        )

        if current_user.role in (models.UserRole.CONTROLLER, models.UserRole.VIEWER):
            dept_user_ids = [
                u.id for u in get_department_users(current_user.department, db)
            ]
            if dept_user_ids:
                query = query.filter(
                    or_(
                        personal,
                        models.Correspondence.executor_id.in_(dept_user_ids),
                        models.Correspondence.created_by_id.in_(dept_user_ids),
                    )
                )
            else:
                query = query.filter(personal)
        else:
            query = query.filter(personal)

    if status_filter:
        query = query.filter(models.Correspondence.status == status_filter)

    if executor_id:
        query = query.filter(models.Correspondence.executor_id == executor_id)

    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                models.Correspondence.incoming_number.ilike(like),
                models.Correspondence.sender.ilike(like),
                models.Correspondence.content.ilike(like),
            )
        )

    today = date.today()
    if urgent:
        near = today + timedelta(days=5)
        query = query.filter(
            models.Correspondence.status != models.CorrespondenceStatus.COMPLETED,
            models.Correspondence.waiting_external == False,  # noqa: E712
            or_(
                models.Correspondence.deadline < today,
                and_(
                    models.Correspondence.deadline >= today,
                    models.Correspondence.deadline <= near,
                ),
            ),
        )

    if days_until_deadline is not None:
        target_date = today + timedelta(days=days_until_deadline)
        query = query.filter(models.Correspondence.deadline <= target_date)
        query = query.filter(models.Correspondence.status != models.CorrespondenceStatus.COMPLETED)

    return query.order_by(models.Correspondence.deadline.asc()).all()


async def get_dashboard_stats(
    request: Request,
    db: Session = Depends(get_db)
):
    """Получение статистики для дашборда"""
    current_user = await get_current_active_user(request, db)

    if current_user.is_admin:
        correspondences = db.query(models.Correspondence).all()
    else:
        correspondences = await get_user_correspondences(request, db)

    today = date.today()
    near_deadline_date = today + timedelta(days=5)

    stats = schemas.DashboardStats(
        total_correspondences=len(correspondences),
        pending_count=sum(1 for c in correspondences if c.status == models.CorrespondenceStatus.PENDING),
        in_progress_count=sum(1 for c in correspondences if c.status == models.CorrespondenceStatus.IN_PROGRESS),
        completed_count=sum(1 for c in correspondences if c.status == models.CorrespondenceStatus.COMPLETED),
        expired_count=sum(
            1 for c in correspondences
            if c.status == models.CorrespondenceStatus.EXPIRED
            and not getattr(c, "waiting_external", False)
        ),
        near_deadline_count=sum(
            1 for c in correspondences
            if c.deadline <= near_deadline_date
            and c.deadline >= today
            and c.status != models.CorrespondenceStatus.COMPLETED
            and not getattr(c, "waiting_external", False)
        )
    )

    return stats


async def admin_required(
    request: Request,
    db: Session = Depends(get_db)
):
    """Декоратор для проверки прав администратора"""
    current_user = await get_current_admin_user(request, db)
    return current_user
