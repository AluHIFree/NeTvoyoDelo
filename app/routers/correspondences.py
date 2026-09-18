from fastapi import APIRouter, Depends, HTTPException, status, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from typing import Annotated, Optional, List, Any
from datetime import date, datetime
from pathlib import Path
from pydantic import BeforeValidator
import json
import os
import uuid


def _coerce_optional_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ValueError("invalid int")
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if not s:
        return None
    return int(s)


OptionalInt = Annotated[Optional[int], BeforeValidator(_coerce_optional_int)]

from app.database import get_db
from app import models
from app.dependencies import (
    get_current_active_user,
    check_correspondence_access,
    get_user_correspondences,
    get_department_users,
    enforce_not_viewer,
)
from app.services.productivity import (
    deadline_stage,
    stage_hint,
    STAGE_LABELS,
    completion_reward_message,
)

router = APIRouter(prefix="/api/correspondences", tags=["correspondences"])
templates = Jinja2Templates(directory="app/templates")

UPLOAD_ROOT = Path("app/static/uploads/correspondences")

LINK_TYPE_LABELS = {
    "related": "Связано",
    "reply_to": "Ответ на",
    "follow_up": "Продолжение",
    "supersedes": "Заменяет",
}


def _completion_redirect_url(correspondence: models.Correspondence, just_completed: bool = False) -> str:
    base = f"/api/correspondences/{correspondence.id}"
    if not just_completed:
        return base
    msg = completion_reward_message(correspondence.deadline, correspondence.completed_at)
    from urllib.parse import quote
    return f"{base}?just_completed=1&reward={quote(msg or 'Письмо закрыто')}"


def extract_flow_from_number(incoming_number: str) -> str:
    if not incoming_number:
        return "без потока"
    if "/" in incoming_number:
        parts = incoming_number.split("/", 1)
        if len(parts) > 1 and parts[1].strip():
            return parts[1].strip()
    return "без потока"


def _add_history(
    db: Session,
    correspondence_id: int,
    action: str,
    user_id: Optional[int] = None,
    note: Optional[str] = None,
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


def _status_enum(status_value: str):
    for s in models.CorrespondenceStatus:
        if s.value == status_value:
            return s
    return None


def _normalize_link_type(link_type: Optional[str]) -> str:
    if link_type and link_type in LINK_TYPE_LABELS:
        return link_type
    return "related"


def _create_link_from_form(
    db: Session,
    source_id: int,
    user_id: int,
    link_mode: Optional[str],
    linked_correspondence_id: Optional[int],
    external_number: Optional[str],
    external_date: Optional[str],
    external_sender: Optional[str],
    external_note: Optional[str],
    link_type: Optional[str],
    link_note: Optional[str],
) -> Optional[models.CorrespondenceLink]:
    """Создаёт связь: платформенная или внешняя. None — если данных нет."""
    mode = (link_mode or "").strip()
    if not mode or mode == "none":
        return None

    lt = _normalize_link_type(link_type)
    note = (link_note or "").strip() or None

    if mode == "platform":
        if not linked_correspondence_id:
            return None
        if linked_correspondence_id == source_id:
            raise ValueError("Нельзя связать письмо само с собой")
        target = (
            db.query(models.Correspondence)
            .filter(models.Correspondence.id == linked_correspondence_id)
            .first()
        )
        if not target:
            raise ValueError("Выбранное связанное письмо не найдено")
        link = models.CorrespondenceLink(
            source_id=source_id,
            target_id=target.id,
            link_type=lt,
            note=note,
            created_by_id=user_id,
        )
        db.add(link)
        return link

    if mode == "external":
        number = (external_number or "").strip()
        if not number:
            raise ValueError("Укажите номер внешнего документа")
        ext_date = None
        if external_date:
            ext_date = datetime.strptime(external_date, "%Y-%m-%d").date()
        link = models.CorrespondenceLink(
            source_id=source_id,
            target_id=None,
            external_number=number,
            external_date=ext_date,
            external_sender=(external_sender or "").strip() or None,
            external_note=(external_note or "").strip() or None,
            link_type=lt,
            note=note,
            created_by_id=user_id,
        )
        db.add(link)
        return link

    return None


def _link_options_for_form(db: Session, exclude_id: Optional[int] = None) -> List[models.Correspondence]:
    q = db.query(models.Correspondence).order_by(models.Correspondence.incoming_date.desc())
    if exclude_id:
        q = q.filter(models.Correspondence.id != exclude_id)
    return q.limit(300).all()


def _serialize_link(link: models.CorrespondenceLink) -> dict:
    item = {
        "id": link.id,
        "source_id": link.source_id,
        "link_type": link.link_type,
        "link_type_label": LINK_TYPE_LABELS.get(link.link_type, link.link_type),
        "note": link.note,
        "is_external": link.target_id is None,
    }
    if link.target_id and link.target:
        t = link.target
        item["target"] = {
            "id": t.id,
            "incoming_number": t.incoming_number,
            "sender": t.sender,
            "status": t.status.value if t.status else "",
            "incoming_date": t.incoming_date.strftime("%d.%m.%Y") if t.incoming_date else "",
            "deadline": t.deadline.strftime("%d.%m.%Y") if t.deadline else "",
            "content": (t.content or "")[:120],
            "executor": t.executor.full_name if t.executor else None,
        }
    else:
        item["external"] = {
            "number": link.external_number,
            "date": link.external_date.strftime("%d.%m.%Y") if link.external_date else "",
            "sender": link.external_sender,
            "note": link.external_note,
        }
    return item


def _load_saved_filter(db: Session, user_id: int, saved_filter_id: Optional[int]) -> dict:
    if not saved_filter_id:
        return {}
    sf = (
        db.query(models.SavedFilter)
        .filter(
            models.SavedFilter.id == saved_filter_id,
            models.SavedFilter.user_id == user_id,
        )
        .first()
    )
    if not sf:
        return {}
    try:
        return json.loads(sf.filters_json or "{}")
    except json.JSONDecodeError:
        return {}


# --- HTML ---
@router.get("/", response_class=HTMLResponse)
async def correspondences_page(
    request: Request,
    q: Optional[str] = None,
    status_filter: Optional[str] = None,
    urgent: Optional[str] = None,
    executor_id: OptionalInt = None,
    saved_filter_id: OptionalInt = None,
    db: Session = Depends(get_db),
):
    """Список писем с поиском и фильтрами."""
    current_user = await get_current_active_user(request, db)

    saved = _load_saved_filter(db, current_user.id, saved_filter_id)
    if saved:
        q = q if q is not None else saved.get("q")
        status_filter = status_filter if status_filter is not None else saved.get("status_filter")
        urgent = urgent if urgent is not None else saved.get("urgent")
        if executor_id is None and saved.get("executor_id"):
            try:
                executor_id = int(saved["executor_id"])
            except (TypeError, ValueError):
                pass

    urgent_flag = urgent in ("1", "true", "yes", "on") if urgent else False

    correspondences = await get_user_correspondences(
        request,
        db,
        status_filter=status_filter,
        q=q,
        urgent=urgent_flag if urgent_flag else None,
        executor_id=executor_id,
    )

    users = get_department_users(current_user.department, db)
    if current_user.is_admin:
        users = db.query(models.User).filter(models.User.is_active == True).all()

    saved_filters = (
        db.query(models.SavedFilter)
        .filter(models.SavedFilter.user_id == current_user.id)
        .order_by(models.SavedFilter.created_at.desc())
        .all()
    )

    return templates.TemplateResponse(
        "correspondences.html",
        {
            "request": request,
            "correspondences": correspondences,
            "current_user": current_user,
            "status_filter": status_filter,
            "q": q or "",
            "urgent": urgent_flag,
            "executor_id": executor_id,
            "saved_filter_id": saved_filter_id,
            "saved_filters": saved_filters,
            "users": users,
            "statuses": [s.value for s in models.CorrespondenceStatus],
            "today": date.today(),
        },
    )


@router.post("/saved-filters")
async def save_filter(
    request: Request,
    name: str = Form(...),
    q: Optional[str] = Form(None),
    status_filter: Optional[str] = Form(None),
    urgent: Optional[str] = Form(None),
    executor_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Сохранить текущие фильтры."""
    current_user = await get_current_active_user(request, db)
    filters = {
        "q": q or "",
        "status_filter": status_filter or "",
        "urgent": urgent or "",
        "executor_id": executor_id or "",
    }
    sf = models.SavedFilter(
        user_id=current_user.id,
        name=name.strip() or "Фильтр",
        filters_json=json.dumps(filters, ensure_ascii=False),
    )
    db.add(sf)
    db.commit()
    return RedirectResponse(url="/api/correspondences/", status_code=303)


@router.get("/saved-filters/{filter_id}/delete")
async def delete_saved_filter(
    request: Request,
    filter_id: int,
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    sf = (
        db.query(models.SavedFilter)
        .filter(
            models.SavedFilter.id == filter_id,
            models.SavedFilter.user_id == current_user.id,
        )
        .first()
    )
    if sf:
        db.delete(sf)
        db.commit()
    return RedirectResponse(url="/api/correspondences/", status_code=303)


@router.get("/create", response_class=HTMLResponse)
async def create_correspondence_form(
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    if current_user.is_admin:
        users = db.query(models.User).filter(models.User.is_active == True).all()
    else:
        users = get_department_users(current_user.department, db)

    request_types = [
        {"value": models.RequestType.OWN_PURPOSES.value, "label": "Для собственных целей"},
        {
            "value": models.RequestType.MINISTRY_OF_EDUCATION.value,
            "label": "Для Министерства просвещения России",
        },
    ]

    return templates.TemplateResponse(
        "correspondence_form.html",
        {
            "request": request,
            "current_user": current_user,
            "users": users,
            "statuses": [s.value for s in models.CorrespondenceStatus],
            "request_types": request_types,
            "is_edit": False,
            "link_options": _link_options_for_form(db),
            "link_types": LINK_TYPE_LABELS,
            "existing_links": [],
        },
    )


@router.get("/export")
async def export_correspondences_to_excel(
    request: Request,
    status_filter: Optional[str] = None,
    q: Optional[str] = None,
    urgent: Optional[str] = None,
    executor_id: OptionalInt = None,
    db: Session = Depends(get_db),
):
    from fastapi.responses import StreamingResponse
    from app.utils.excel_export import build_correspondence_workbook

    current_user = await get_current_active_user(request, db)
    urgent_flag = urgent in ("1", "true", "yes", "on") if urgent else False
    correspondences = await get_user_correspondences(
        request,
        db,
        status_filter=status_filter,
        q=q,
        urgent=urgent_flag if urgent_flag else None,
        executor_id=executor_id,
    )

    output = build_correspondence_workbook(
        correspondences,
        sheet_title="Входящая корреспонденция",
        report_title="Реестр входящей корреспонденции",
        include_flow=True,
        include_request=True,
        extra_subtitle=f"Пользователь: {current_user.full_name}",
    )
    filename = f"correspondences_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/links", response_class=HTMLResponse)
async def letter_links_page(
    request: Request,
    db: Session = Depends(get_db),
    focus: Optional[int] = None,
):
    """Визуализация связей писем (граф)."""
    current_user = await get_current_active_user(request, db)
    link_count = db.query(models.CorrespondenceLink).count()
    return templates.TemplateResponse(
        "correspondence_links.html",
        {
            "request": request,
            "current_user": current_user,
            "link_count": link_count,
            "focus_id": focus,
            "link_types": LINK_TYPE_LABELS,
        },
    )


@router.get("/links/data")
async def letter_links_data(
    request: Request,
    db: Session = Depends(get_db),
    focus: Optional[int] = None,
):
    """JSON-граф связей для визуализации."""
    current_user = await get_current_active_user(request, db)
    links = db.query(models.CorrespondenceLink).all()

    node_ids = set()
    for link in links:
        node_ids.add(link.source_id)
        if link.target_id:
            node_ids.add(link.target_id)

    if focus:
        node_ids.add(focus)

    # Если связей нет — покажем доступные письма как изолированные узлы (ограниченно)
    if not node_ids:
        accessible = await get_user_correspondences(request, db)
        for c in accessible[:40]:
            node_ids.add(c.id)

    correspondences = (
        db.query(models.Correspondence)
        .filter(models.Correspondence.id.in_(node_ids))
        .all()
        if node_ids
        else []
    )
    corr_map = {c.id: c for c in correspondences}

    nodes = []
    for cid, c in corr_map.items():
        nodes.append({
            "id": f"c-{cid}",
            "kind": "platform",
            "corr_id": cid,
            "label": c.incoming_number,
            "sender": c.sender,
            "status": c.status.value if c.status else "",
            "date": c.incoming_date.strftime("%d.%m.%Y") if c.incoming_date else "",
            "deadline": c.deadline.strftime("%d.%m.%Y") if c.deadline else "",
            "content": (c.content or "")[:160],
            "executor": c.executor.full_name if c.executor else "Не назначен",
            "url": f"/api/correspondences/{cid}",
        })

    edges = []
    for link in links:
        source_key = f"c-{link.source_id}"
        if link.target_id:
            target_key = f"c-{link.target_id}"
        else:
            ext_id = f"ext-{link.id}"
            target_key = ext_id
            nodes.append({
                "id": ext_id,
                "kind": "external",
                "corr_id": None,
                "label": link.external_number or "Внешний",
                "sender": link.external_sender or "—",
                "status": "Внешний документ",
                "date": link.external_date.strftime("%d.%m.%Y") if link.external_date else "",
                "deadline": "",
                "content": link.external_note or link.note or "",
                "executor": "—",
                "url": None,
            })
        edges.append({
            "id": link.id,
            "source": source_key,
            "target": target_key,
            "type": link.link_type,
            "type_label": LINK_TYPE_LABELS.get(link.link_type, link.link_type),
            "note": link.note or link.external_note or "",
        })

    return JSONResponse({"nodes": nodes, "edges": edges, "focus": f"c-{focus}" if focus else None})


@router.get("/{correspondence_id}/edit", response_class=HTMLResponse)
async def edit_correspondence_form(
    request: Request,
    correspondence_id: int,
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    correspondence = await check_correspondence_access(request, correspondence_id, db)
    if current_user.is_admin:
        users = db.query(models.User).filter(models.User.is_active == True).all()
    else:
        users = get_department_users(current_user.department, db)

    request_types = [
        {"value": models.RequestType.OWN_PURPOSES.value, "label": "Для собственных целей"},
        {
            "value": models.RequestType.MINISTRY_OF_EDUCATION.value,
            "label": "Для Министерства просвещения России",
        },
    ]

    from app.utils.email_sender import smtp_is_configured

    letter_attachments = (
        db.query(models.CorrespondenceAttachment)
        .filter(models.CorrespondenceAttachment.correspondence_id == correspondence_id)
        .order_by(models.CorrespondenceAttachment.uploaded_at.asc())
        .all()
    )

    return templates.TemplateResponse(
        "correspondence_form.html",
        {
            "request": request,
            "current_user": current_user,
            "users": users,
            "correspondence": correspondence,
            "statuses": [s.value for s in models.CorrespondenceStatus],
            "request_types": request_types,
            "is_edit": True,
            "link_options": _link_options_for_form(db, exclude_id=correspondence_id),
            "link_types": LINK_TYPE_LABELS,
            "existing_links": correspondence.outgoing_links or [],
            "smtp_configured": smtp_is_configured(),
            "email_sent": request.query_params.get("email_sent") == "1",
            "email_to": request.query_params.get("email_to") or "",
            "email_error": request.query_params.get("email_error") or "",
            "letter_attachments": letter_attachments,
        },
    )


@router.get("/{correspondence_id}", response_class=HTMLResponse)
async def view_correspondence(
    request: Request,
    correspondence_id: int,
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    correspondence = await check_correspondence_access(request, correspondence_id, db)

    transfers = (
        db.query(models.CorrespondenceTransfer)
        .filter(
            models.CorrespondenceTransfer.correspondence_id == correspondence_id,
            models.CorrespondenceTransfer.is_active == True,
        )
        .all()
    )

    if current_user.is_admin:
        available_users = db.query(models.User).filter(models.User.is_active == True).all()
    else:
        available_users = get_department_users(current_user.department, db)

    attachments = (
        db.query(models.CorrespondenceAttachment)
        .filter(models.CorrespondenceAttachment.correspondence_id == correspondence_id)
        .order_by(models.CorrespondenceAttachment.uploaded_at.desc())
        .all()
    )
    history = (
        db.query(models.CorrespondenceHistory)
        .filter(models.CorrespondenceHistory.correspondence_id == correspondence_id)
        .order_by(models.CorrespondenceHistory.created_at.desc())
        .all()
    )

    outgoing_links = (
        db.query(models.CorrespondenceLink)
        .filter(models.CorrespondenceLink.source_id == correspondence_id)
        .order_by(models.CorrespondenceLink.created_at.desc())
        .all()
    )
    incoming_links = (
        db.query(models.CorrespondenceLink)
        .filter(models.CorrespondenceLink.target_id == correspondence_id)
        .order_by(models.CorrespondenceLink.created_at.desc())
        .all()
    )
    # Подгружаем связанные письма для шаблона
    for link in outgoing_links:
        if link.target_id:
            _ = link.target
    for link in incoming_links:
        _ = link.source

    today = date.today()
    rem = getattr(current_user, "reminder_days_before", None) or 5
    stage = deadline_stage(
        correspondence.deadline,
        today=today,
        reminder_days=rem,
        status=correspondence.status,
        waiting_external=bool(getattr(correspondence, "waiting_external", False)),
    )
    left = (correspondence.deadline - today).days if correspondence.deadline else None

    return templates.TemplateResponse(
        "correspondence_detail.html",
        {
            "request": request,
            "correspondence": correspondence,
            "current_user": current_user,
            "transfers": transfers,
            "available_users": available_users,
            "attachments": attachments,
            "history": history,
            "outgoing_links": outgoing_links,
            "incoming_links": incoming_links,
            "link_options": _link_options_for_form(db, exclude_id=correspondence_id),
            "link_types": LINK_TYPE_LABELS,
            "statuses": [s.value for s in models.CorrespondenceStatus],
            "today": today,
            "just_completed": request.query_params.get("just_completed") == "1",
            "completion_reward": request.query_params.get("reward") or "",
            "deadline_stage": stage,
            "deadline_stage_labels": STAGE_LABELS,
            "deadline_stage_hint": stage_hint(stage, left),
        },
    )


@router.post("/create", response_class=HTMLResponse)
async def create_correspondence(
    request: Request,
    incoming_number: str = Form(...),
    incoming_date: str = Form(...),
    received_date: str = Form(...),
    sender: str = Form(...),
    content: str = Form(...),
    deadline: str = Form(...),
    status_value: str = Form(..., alias="status"),
    executor_id: Optional[int] = Form(None),
    sent_info: Optional[str] = Form(None),
    to_whom: Optional[str] = Form(None),
    control: Optional[str] = Form(None),
    report: Optional[str] = Form(None),
    report_date: Optional[str] = Form(None),
    request_type: Optional[str] = Form(None),
    request_note: Optional[str] = Form(None),
    completion_note: Optional[str] = Form(None),
    waiting_external: Optional[str] = Form(None),
    waiting_external_note: Optional[str] = Form(None),
    link_mode: Optional[str] = Form("none"),
    linked_correspondence_id: Optional[int] = Form(None),
    external_number: Optional[str] = Form(None),
    external_date: Optional[str] = Form(None),
    external_sender: Optional[str] = Form(None),
    external_note: Optional[str] = Form(None),
    link_type: Optional[str] = Form("related"),
    link_note: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)

    try:
        incoming_date_obj = datetime.strptime(incoming_date, "%Y-%m-%d").date()
        received_date_obj = datetime.strptime(received_date, "%Y-%m-%d").date()
        deadline_obj = datetime.strptime(deadline, "%Y-%m-%d").date()
        report_date_obj = (
            datetime.strptime(report_date, "%Y-%m-%d").date() if report_date else None
        )

        status_enum = _status_enum(status_value)
        request_type_enum = None
        if request_type:
            for rt in models.RequestType:
                if rt.value == request_type:
                    request_type_enum = rt
                    break

        if status_enum == models.CorrespondenceStatus.COMPLETED and not (completion_note or "").strip():
            raise ValueError("При статусе «Исполнено» требуется примечание (completion_note)")

        flow = extract_flow_from_number(incoming_number)
        just_completed = status_enum == models.CorrespondenceStatus.COMPLETED
        completed_at = datetime.utcnow() if just_completed else None
        is_waiting = waiting_external in ("1", "on", "true", "True")

        new_correspondence = models.Correspondence(
            incoming_number=incoming_number,
            incoming_date=incoming_date_obj,
            received_date=received_date_obj,
            sender=sender,
            content=content,
            deadline=deadline_obj,
            status=status_enum or models.CorrespondenceStatus.PENDING,
            executor_id=executor_id,
            created_by_id=current_user.id,
            sent_info=sent_info,
            to_whom=to_whom,
            control=control,
            report=report,
            report_date=report_date_obj,
            flow=flow,
            request_type=request_type_enum,
            request_note=request_note if request_note else None,
            completion_note=completion_note if completion_note else None,
            completed_at=completed_at,
            waiting_external=is_waiting,
            waiting_external_note=(waiting_external_note or None) if is_waiting else None,
        )

        db.add(new_correspondence)
        db.flush()
        _add_history(
            db,
            new_correspondence.id,
            action="created",
            user_id=current_user.id,
            note=f"Создано письмо №{incoming_number}",
        )
        link = _create_link_from_form(
            db,
            new_correspondence.id,
            current_user.id,
            link_mode,
            linked_correspondence_id,
            external_number,
            external_date,
            external_sender,
            external_note,
            link_type,
            link_note,
        )
        if link:
            _add_history(
                db,
                new_correspondence.id,
                action="link_added",
                user_id=current_user.id,
                note="Добавлена связь с документом",
            )
        db.commit()
        db.refresh(new_correspondence)

        return RedirectResponse(
            url=_completion_redirect_url(new_correspondence, just_completed=just_completed),
            status_code=303,
        )
    except Exception as e:
        if current_user.is_admin:
            users = db.query(models.User).filter(models.User.is_active == True).all()
        else:
            users = get_department_users(current_user.department, db)
        request_types = [
            {"value": models.RequestType.OWN_PURPOSES.value, "label": "Для собственных целей"},
            {
                "value": models.RequestType.MINISTRY_OF_EDUCATION.value,
                "label": "Для Министерства просвещения России",
            },
        ]
        return templates.TemplateResponse(
            "correspondence_form.html",
            {
                "request": request,
                "current_user": current_user,
                "users": users,
                "statuses": [s.value for s in models.CorrespondenceStatus],
                "request_types": request_types,
                "is_edit": False,
                "link_options": _link_options_for_form(db),
                "link_types": LINK_TYPE_LABELS,
                "existing_links": [],
                "error": f"Ошибка при создании: {str(e)}",
            },
            status_code=400,
        )


@router.post("/{correspondence_id}/edit")
async def update_correspondence(
    request: Request,
    correspondence_id: int,
    incoming_number: str = Form(...),
    incoming_date: str = Form(...),
    received_date: str = Form(...),
    sender: str = Form(...),
    content: str = Form(...),
    deadline: str = Form(...),
    status_value: str = Form(..., alias="status"),
    executor_id: Optional[int] = Form(None),
    sent_info: Optional[str] = Form(None),
    to_whom: Optional[str] = Form(None),
    control: Optional[str] = Form(None),
    report: Optional[str] = Form(None),
    report_date: Optional[str] = Form(None),
    request_type: Optional[str] = Form(None),
    request_note: Optional[str] = Form(None),
    completion_note: Optional[str] = Form(None),
    waiting_external: Optional[str] = Form(None),
    waiting_external_note: Optional[str] = Form(None),
    link_mode: Optional[str] = Form("none"),
    linked_correspondence_id: Optional[int] = Form(None),
    external_number: Optional[str] = Form(None),
    external_date: Optional[str] = Form(None),
    external_sender: Optional[str] = Form(None),
    external_note: Optional[str] = Form(None),
    link_type: Optional[str] = Form("related"),
    link_note: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    correspondence = await check_correspondence_access(request, correspondence_id, db)

    try:
        incoming_date_obj = datetime.strptime(incoming_date, "%Y-%m-%d").date()
        received_date_obj = datetime.strptime(received_date, "%Y-%m-%d").date()
        deadline_obj = datetime.strptime(deadline, "%Y-%m-%d").date()
        report_date_obj = (
            datetime.strptime(report_date, "%Y-%m-%d").date() if report_date else None
        )

        status_enum = _status_enum(status_value)
        request_type_enum = None
        if request_type:
            for rt in models.RequestType:
                if rt.value == request_type:
                    request_type_enum = rt
                    break

        if status_enum == models.CorrespondenceStatus.COMPLETED and not (
            completion_note or correspondence.completion_note or ""
        ).strip():
            raise ValueError("При статусе «Исполнено» требуется примечание (completion_note)")

        old_status = correspondence.status.value if correspondence.status else None
        completed_at = correspondence.completed_at
        just_completed = False
        if (
            status_enum == models.CorrespondenceStatus.COMPLETED
            and correspondence.status != models.CorrespondenceStatus.COMPLETED
        ):
            completed_at = datetime.utcnow()
            just_completed = True

        flow = extract_flow_from_number(incoming_number)
        is_waiting = waiting_external in ("1", "on", "true", "True")

        correspondence.incoming_number = incoming_number
        correspondence.incoming_date = incoming_date_obj
        correspondence.received_date = received_date_obj
        correspondence.sender = sender
        correspondence.content = content
        correspondence.deadline = deadline_obj
        correspondence.status = status_enum or models.CorrespondenceStatus.PENDING
        correspondence.executor_id = executor_id
        correspondence.sent_info = sent_info
        correspondence.to_whom = to_whom
        correspondence.control = control
        correspondence.report = report
        correspondence.report_date = report_date_obj
        correspondence.completed_at = completed_at
        correspondence.updated_at = datetime.utcnow()
        correspondence.flow = flow
        correspondence.request_type = request_type_enum
        correspondence.request_note = request_note if request_note else None
        if completion_note is not None:
            correspondence.completion_note = completion_note or None
        correspondence.waiting_external = is_waiting
        correspondence.waiting_external_note = (
            (waiting_external_note or None) if is_waiting else None
        )

        new_status = correspondence.status.value if correspondence.status else None
        if old_status != new_status:
            _add_history(
                db,
                correspondence.id,
                action="status_changed",
                user_id=current_user.id,
                field_name="status",
                old_value=old_status,
                new_value=new_status,
                note=completion_note,
            )
        else:
            _add_history(
                db,
                correspondence.id,
                action="updated",
                user_id=current_user.id,
                note="Обновление письма",
            )

        link = _create_link_from_form(
            db,
            correspondence.id,
            current_user.id,
            link_mode,
            linked_correspondence_id,
            external_number,
            external_date,
            external_sender,
            external_note,
            link_type,
            link_note,
        )
        if link:
            _add_history(
                db,
                correspondence.id,
                action="link_added",
                user_id=current_user.id,
                note="Добавлена связь с документом",
            )

        db.commit()
        db.refresh(correspondence)

        return RedirectResponse(
            url=_completion_redirect_url(correspondence, just_completed=just_completed),
            status_code=303,
        )
    except Exception as e:
        if current_user.is_admin:
            users = db.query(models.User).filter(models.User.is_active == True).all()
        else:
            users = get_department_users(current_user.department, db)
        request_types = [
            {"value": models.RequestType.OWN_PURPOSES.value, "label": "Для собственных целей"},
            {
                "value": models.RequestType.MINISTRY_OF_EDUCATION.value,
                "label": "Для Министерства просвещения России",
            },
        ]
        return templates.TemplateResponse(
            "correspondence_form.html",
            {
                "request": request,
                "current_user": current_user,
                "users": users,
                "correspondence": correspondence,
                "statuses": [s.value for s in models.CorrespondenceStatus],
                "request_types": request_types,
                "is_edit": True,
                "link_options": _link_options_for_form(db, exclude_id=correspondence_id),
                "link_types": LINK_TYPE_LABELS,
                "existing_links": correspondence.outgoing_links or [],
                "error": f"Ошибка при обновлении: {str(e)}",
            },
            status_code=400,
        )


@router.post("/{correspondence_id}/links")
async def add_correspondence_link(
    request: Request,
    correspondence_id: int,
    link_mode: str = Form(...),
    linked_correspondence_id: Optional[int] = Form(None),
    external_number: Optional[str] = Form(None),
    external_date: Optional[str] = Form(None),
    external_sender: Optional[str] = Form(None),
    external_note: Optional[str] = Form(None),
    link_type: Optional[str] = Form("related"),
    link_note: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    await check_correspondence_access(request, correspondence_id, db)

    try:
        link = _create_link_from_form(
            db,
            correspondence_id,
            current_user.id,
            link_mode,
            linked_correspondence_id,
            external_number,
            external_date,
            external_sender,
            external_note,
            link_type,
            link_note,
        )
        if not link:
            raise ValueError("Укажите письмо на платформе или реквизиты внешнего документа")
        _add_history(
            db,
            correspondence_id,
            action="link_added",
            user_id=current_user.id,
            note="Добавлена связь с документом",
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse(
        url=f"/api/correspondences/{correspondence_id}",
        status_code=303,
    )


@router.post("/{correspondence_id}/links/{link_id}/delete")
async def delete_correspondence_link(
    request: Request,
    correspondence_id: int,
    link_id: int,
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    await check_correspondence_access(request, correspondence_id, db)

    link = (
        db.query(models.CorrespondenceLink)
        .filter(
            models.CorrespondenceLink.id == link_id,
            models.CorrespondenceLink.source_id == correspondence_id,
        )
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Связь не найдена")

    db.delete(link)
    _add_history(
        db,
        correspondence_id,
        action="link_removed",
        user_id=current_user.id,
        note="Удалена связь с документом",
    )
    db.commit()

    return RedirectResponse(
        url=f"/api/correspondences/{correspondence_id}",
        status_code=303,
    )


@router.post("/{correspondence_id}/quick-status")
async def quick_status(
    request: Request,
    correspondence_id: int,
    status_value: str = Form(..., alias="status"),
    completion_note: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Быстрая смена статуса (с списка или модалки)."""
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    correspondence = await check_correspondence_access(request, correspondence_id, db)

    status_enum = _status_enum(status_value)
    if not status_enum:
        raise HTTPException(status_code=400, detail="Invalid status")

    if status_enum == models.CorrespondenceStatus.COMPLETED and not (completion_note or "").strip():
        raise HTTPException(
            status_code=400,
            detail="completion_note required when status is Исполнено",
        )

    old = correspondence.status.value if correspondence.status else None
    just_completed = (
        status_enum == models.CorrespondenceStatus.COMPLETED
        and correspondence.status != models.CorrespondenceStatus.COMPLETED
    )
    correspondence.status = status_enum
    if status_enum == models.CorrespondenceStatus.COMPLETED:
        correspondence.completed_at = datetime.utcnow()
        correspondence.completion_note = completion_note
        correspondence.waiting_external = False
        correspondence.waiting_external_note = None
    correspondence.updated_at = datetime.utcnow()

    _add_history(
        db,
        correspondence.id,
        action="status_changed",
        user_id=current_user.id,
        field_name="status",
        old_value=old,
        new_value=status_enum.value,
        note=completion_note,
    )
    db.commit()
    db.refresh(correspondence)

    if just_completed:
        return RedirectResponse(
            url=_completion_redirect_url(correspondence, just_completed=True),
            status_code=303,
        )

    referer = request.headers.get("referer") or f"/api/correspondences/{correspondence_id}"
    return RedirectResponse(url=referer, status_code=303)


@router.post("/{correspondence_id}/take-in-work")
async def take_in_work(
    request: Request,
    correspondence_id: int,
    db: Session = Depends(get_db),
):
    """Взять письмо в работу с дашборда / карточки."""
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    correspondence = await check_correspondence_access(request, correspondence_id, db)

    if correspondence.status == models.CorrespondenceStatus.COMPLETED:
        return RedirectResponse(
            url=request.headers.get("referer") or f"/api/correspondences/{correspondence_id}",
            status_code=303,
        )

    old = correspondence.status.value if correspondence.status else None
    correspondence.status = models.CorrespondenceStatus.IN_PROGRESS
    correspondence.updated_at = datetime.utcnow()
    if correspondence.executor_id is None:
        correspondence.executor_id = current_user.id

    _add_history(
        db,
        correspondence.id,
        action="status_changed",
        user_id=current_user.id,
        field_name="status",
        old_value=old,
        new_value=models.CorrespondenceStatus.IN_PROGRESS.value,
        note="Взято в работу",
    )
    db.commit()

    referer = request.headers.get("referer") or "/"
    if "just_completed" in (referer or ""):
        referer = f"/api/correspondences/{correspondence_id}"
    return RedirectResponse(url=referer if referer else "/#work-plan", status_code=303)


@router.post("/{correspondence_id}/waiting-external")
async def toggle_waiting_external(
    request: Request,
    correspondence_id: int,
    waiting: Optional[str] = Form("1"),
    note: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Флаг «жду внешнего ответа» — исключает из личной нагрузки."""
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    correspondence = await check_correspondence_access(request, correspondence_id, db)

    is_on = waiting in ("1", "on", "true", "True")
    old = bool(getattr(correspondence, "waiting_external", False))
    correspondence.waiting_external = is_on
    correspondence.waiting_external_note = (note or None) if is_on else None
    correspondence.updated_at = datetime.utcnow()

    _add_history(
        db,
        correspondence.id,
        action="waiting_external",
        user_id=current_user.id,
        field_name="waiting_external",
        old_value=str(old),
        new_value=str(is_on),
        note=note if is_on else "Снято ожидание внешнего ответа",
    )
    db.commit()

    referer = request.headers.get("referer") or f"/api/correspondences/{correspondence_id}"
    return RedirectResponse(url=referer, status_code=303)


def _resolve_attachment_path(stored_path: str) -> Path:
    """Путь к файлу вложения на диске."""
    rel = (stored_path or "").replace("\\", "/").lstrip("/")
    candidates = [
        Path("app/static") / rel,
        Path("app/static") / stored_path if stored_path else Path(""),
        Path(rel),
    ]
    for p in candidates:
        if p and p.exists() and p.is_file():
            return p
    return Path("app/static") / rel


@router.post("/{correspondence_id}/send-email")
async def send_correspondence_by_email(
    request: Request,
    correspondence_id: int,
    send_to_self: Optional[str] = Form(None),
    recipient_email: Optional[str] = Form(None),
    personal_note: Optional[str] = Form(None),
    include_linked: Optional[str] = Form("1"),
    attachment: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    """
    Отправить карточку письма на email.
    По умолчанию подтягивает файлы, уже связанные с письмом;
    дополнительно можно вручную приложить файл (или отправить без вложений).
    """
    from app.utils.email_sender import (
        send_html_email,
        smtp_is_configured,
        is_allowed_email_attachment,
        MAX_EMAIL_ATTACHMENT_BYTES,
    )
    from app.utils.correspondence_email import build_correspondence_email_html
    from urllib.parse import quote
    import re

    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    correspondence = await check_correspondence_access(request, correspondence_id, db)

    edit_url = f"/api/correspondences/{correspondence_id}/edit"

    if not smtp_is_configured():
        return RedirectResponse(
            url=f"{edit_url}?email_error=smtp",
            status_code=303,
        )

    to_self = send_to_self in ("1", "on", "true", "True")
    if to_self:
        to_email = (current_user.email or "").strip()
    else:
        to_email = (recipient_email or "").strip()

    email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    if not to_email or not email_re.match(to_email):
        return RedirectResponse(
            url=f"{edit_url}?email_error=invalid_email",
            status_code=303,
        )

    attachments: list = []
    attach_names: list = []
    total_size = 0
    want_linked = include_linked in ("1", "on", "true", "True")

    if want_linked:
        linked = (
            db.query(models.CorrespondenceAttachment)
            .filter(models.CorrespondenceAttachment.correspondence_id == correspondence_id)
            .order_by(models.CorrespondenceAttachment.uploaded_at.asc())
            .all()
        )
        for att in linked:
            path = _resolve_attachment_path(att.stored_path)
            if not path.exists():
                continue
            data = path.read_bytes()
            if not data:
                continue
            if total_size + len(data) > MAX_EMAIL_ATTACHMENT_BYTES:
                return RedirectResponse(
                    url=f"{edit_url}?email_error=too_large",
                    status_code=303,
                )
            name = att.filename or path.name
            attachments.append((name, data, att.content_type))
            attach_names.append(name)
            total_size += len(data)

    # Ручной файл — дополнительно (или единственное вложение, если связанных нет)
    if attachment and attachment.filename:
        raw_name = attachment.filename
        if not is_allowed_email_attachment(raw_name):
            return RedirectResponse(
                url=f"{edit_url}?email_error=bad_file",
                status_code=303,
            )
        data = await attachment.read()
        if data:
            if total_size + len(data) > MAX_EMAIL_ATTACHMENT_BYTES:
                return RedirectResponse(
                    url=f"{edit_url}?email_error=too_large",
                    status_code=303,
                )
            name = Path(raw_name).name
            attachments.append((name, data, attachment.content_type))
            attach_names.append(name)
            total_size += len(data)

    html = build_correspondence_email_html(
        correspondence,
        sent_by_name=current_user.full_name,
        attachment_names=attach_names or None,
        personal_note=personal_note,
    )
    subject = f"Входящее №{correspondence.incoming_number} — {correspondence.sender or 'корреспонденция'}"

    ok = await send_html_email(
        to_email,
        subject,
        html,
        attachments=attachments or None,
    )
    if not ok:
        return RedirectResponse(
            url=f"{edit_url}?email_error=send_failed",
            status_code=303,
        )

    note_parts = [f"Отправлено на {to_email}"]
    if attach_names:
        note_parts.append("вложения: " + ", ".join(attach_names))
    else:
        note_parts.append("без вложений")

    _add_history(
        db,
        correspondence.id,
        action="email_sent",
        user_id=current_user.id,
        note="; ".join(note_parts),
    )
    db.commit()

    return RedirectResponse(
        url=f"{edit_url}?email_sent=1&email_to={quote(to_email)}",
        status_code=303,
    )


@router.post("/{correspondence_id}/transfer")
async def transfer_correspondence(
    request: Request,
    correspondence_id: int,
    transferred_to_id: int = Form(...),
    note: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    correspondence = await check_correspondence_access(request, correspondence_id, db)

    target_user = db.query(models.User).filter(models.User.id == transferred_to_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    active_transfers = (
        db.query(models.CorrespondenceTransfer)
        .filter(
            and_(
                models.CorrespondenceTransfer.correspondence_id == correspondence_id,
                models.CorrespondenceTransfer.is_active == True,
            )
        )
        .all()
    )
    for transfer in active_transfers:
        transfer.is_active = False

    new_transfer = models.CorrespondenceTransfer(
        correspondence_id=correspondence_id,
        transferred_by_id=current_user.id,
        transferred_to_id=transferred_to_id,
        note=note,
        is_active=True,
    )

    old_executor = correspondence.executor_id
    correspondence.status = models.CorrespondenceStatus.TRANSFERRED
    correspondence.executor_id = transferred_to_id

    db.add(new_transfer)
    _add_history(
        db,
        correspondence_id,
        action="transferred",
        user_id=current_user.id,
        note=note,
        field_name="executor_id",
        old_value=str(old_executor) if old_executor else None,
        new_value=str(transferred_to_id),
    )
    db.commit()
    db.refresh(correspondence)

    try:
        from app.services.notification_service import notify_transfer

        await notify_transfer(db, correspondence, current_user, target_user, note=note)
    except Exception as e:
        print(f"[WARN] Не удалось отправить уведомление о передаче: {e}")

    return RedirectResponse(
        url=f"/api/correspondences/{correspondence_id}",
        status_code=303,
    )


@router.post("/{correspondence_id}/attachments")
async def upload_attachment(
    request: Request,
    correspondence_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    await check_correspondence_access(request, correspondence_id, db)

    dest_dir = UPLOAD_ROOT / str(correspondence_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    original = file.filename or "file"
    safe_name = f"{uuid.uuid4().hex}_{original}"
    stored = dest_dir / safe_name
    contents = await file.read()
    with open(stored, "wb") as f:
        f.write(contents)

    rel_path = f"uploads/correspondences/{correspondence_id}/{safe_name}"
    att = models.CorrespondenceAttachment(
        correspondence_id=correspondence_id,
        uploaded_by_id=current_user.id,
        filename=original,
        stored_path=rel_path,
        content_type=file.content_type,
        size_bytes=len(contents),
    )
    db.add(att)
    _add_history(
        db,
        correspondence_id,
        action="attachment_added",
        user_id=current_user.id,
        note=original,
    )
    db.commit()

    return RedirectResponse(
        url=f"/api/correspondences/{correspondence_id}",
        status_code=303,
    )


@router.get("/{correspondence_id}/attachments/{attachment_id}/download")
async def download_attachment(
    request: Request,
    correspondence_id: int,
    attachment_id: int,
    db: Session = Depends(get_db),
):
    await check_correspondence_access(request, correspondence_id, db)
    att = (
        db.query(models.CorrespondenceAttachment)
        .filter(
            models.CorrespondenceAttachment.id == attachment_id,
            models.CorrespondenceAttachment.correspondence_id == correspondence_id,
        )
        .first()
    )
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")

    path = Path("app/static") / att.stored_path.replace("\\", "/")
    if not path.exists():
        # fallback: stored_path already relative to static
        path = Path("app/static") / att.stored_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")

    return FileResponse(
        path=str(path),
        filename=att.filename,
        media_type=att.content_type or "application/octet-stream",
    )


@router.post("/{correspondence_id}/attachments/{attachment_id}/delete")
async def delete_attachment(
    request: Request,
    correspondence_id: int,
    attachment_id: int,
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    await check_correspondence_access(request, correspondence_id, db)

    att = (
        db.query(models.CorrespondenceAttachment)
        .filter(
            models.CorrespondenceAttachment.id == attachment_id,
            models.CorrespondenceAttachment.correspondence_id == correspondence_id,
        )
        .first()
    )
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")

    if not current_user.is_admin and att.uploaded_by_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only uploader or admin can delete")

    path = Path("app/static") / att.stored_path.replace("\\", "/")
    if path.exists():
        try:
            os.remove(path)
        except OSError:
            pass

    _add_history(
        db,
        correspondence_id,
        action="attachment_deleted",
        user_id=current_user.id,
        note=att.filename,
    )
    db.delete(att)
    db.commit()

    return RedirectResponse(
        url=f"/api/correspondences/{correspondence_id}",
        status_code=303,
    )


@router.post("/{correspondence_id}/delete")
async def delete_correspondence(
    request: Request,
    correspondence_id: int,
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    correspondence = await check_correspondence_access(request, correspondence_id, db)

    if not current_user.is_admin and correspondence.created_by_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin or creator can delete correspondence",
        )

    db.query(models.CorrespondenceTransfer).filter(
        models.CorrespondenceTransfer.correspondence_id == correspondence_id
    ).delete()
    db.query(models.CorrespondenceHistory).filter(
        models.CorrespondenceHistory.correspondence_id == correspondence_id
    ).delete()
    db.query(models.CorrespondenceAttachment).filter(
        models.CorrespondenceAttachment.correspondence_id == correspondence_id
    ).delete()
    db.query(models.CorrespondenceLink).filter(
        or_(
            models.CorrespondenceLink.source_id == correspondence_id,
            models.CorrespondenceLink.target_id == correspondence_id,
        )
    ).delete(synchronize_session=False)

    db.delete(correspondence)
    db.commit()

    return RedirectResponse(url="/api/correspondences/", status_code=303)
