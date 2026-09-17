from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from typing import Optional
from datetime import date, datetime
from io import BytesIO

from app.database import get_db
from app import models
from app.dependencies import get_current_active_user

router = APIRouter(prefix="/requests", tags=["requests"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def requests_page(
    request: Request,
    request_type_filter: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Страница со списком писем-запросов"""
    current_user = await get_current_active_user(request, db)
    
    # Базовый запрос - только письма с заполненным request_type
    if current_user.is_admin:
        query = db.query(models.Correspondence).filter(
            models.Correspondence.request_type.isnot(None)
        )
    else:
        query = db.query(models.Correspondence).filter(
            and_(
                models.Correspondence.request_type.isnot(None),
                or_(
                    models.Correspondence.executor_id == current_user.id,
                    models.Correspondence.created_by_id == current_user.id,
                    models.Correspondence.id.in_(
                        db.query(models.CorrespondenceTransfer.correspondence_id).filter(
                            models.CorrespondenceTransfer.transferred_to_id == current_user.id,
                            models.CorrespondenceTransfer.is_active == True
                        )
                    )
                )
            )
        )
    
    # Фильтр по типу запроса
    if request_type_filter:
        query = query.filter(models.Correspondence.request_type == request_type_filter)
    
    correspondences = query.order_by(models.Correspondence.created_at.desc()).all()
    
    # Статистика
    total = len(correspondences)
    own_purposes = sum(1 for c in correspondences if c.request_type == models.RequestType.OWN_PURPOSES)
    ministry = sum(1 for c in correspondences if c.request_type == models.RequestType.MINISTRY_OF_EDUCATION)
    
    # Типы запросов для фильтра
    request_types = [
        {"value": models.RequestType.OWN_PURPOSES.value, "label": "Для собственных целей"},
        {"value": models.RequestType.MINISTRY_OF_EDUCATION.value, "label": "Для Министерства просвещения России"}
    ]
    
    return templates.TemplateResponse(
        "requests.html",
        {
            "request": request,
            "current_user": current_user,
            "correspondences": correspondences,
            "total": total,
            "own_purposes": own_purposes,
            "ministry": ministry,
            "request_types": request_types,
            "request_type_filter": request_type_filter,
            "today": date.today(),
            "statuses": [s.value for s in models.CorrespondenceStatus]
        }
    )


@router.get("/export")
async def export_requests_to_excel(
    request: Request,
    request_type_filter: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Экспорт писем-запросов в Excel"""
    from app.utils.excel_export import build_correspondence_workbook

    current_user = await get_current_active_user(request, db)

    if current_user.is_admin:
        query = db.query(models.Correspondence).filter(
            models.Correspondence.request_type.isnot(None)
        )
    else:
        query = db.query(models.Correspondence).filter(
            and_(
                models.Correspondence.request_type.isnot(None),
                or_(
                    models.Correspondence.executor_id == current_user.id,
                    models.Correspondence.created_by_id == current_user.id,
                    models.Correspondence.id.in_(
                        db.query(models.CorrespondenceTransfer.correspondence_id).filter(
                            models.CorrespondenceTransfer.transferred_to_id == current_user.id,
                            models.CorrespondenceTransfer.is_active == True
                        )
                    )
                )
            )
        )

    if request_type_filter:
        query = query.filter(models.Correspondence.request_type == request_type_filter)

    correspondences = query.order_by(models.Correspondence.created_at.desc()).all()

    output = build_correspondence_workbook(
        correspondences,
        sheet_title="Письма-запросы",
        report_title="Письма-запросы",
        include_flow=True,
        include_request=True,
        extra_subtitle=f"Пользователь: {current_user.full_name}",
    )

    filename = f"requests_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
