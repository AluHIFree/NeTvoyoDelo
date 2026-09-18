"""Раздел «Парсер почты» — поиск писем по IMAP с разбором вложений."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.dependencies import enforce_not_viewer, get_current_active_user
from app.services import mail_parser_service as mps
from app.services.mail_crypto import decrypt_secret

router = APIRouter(prefix="/mail-parser", tags=["mail-parser"])
templates = Jinja2Templates(directory="app/templates")


def _page_context(
    request: Request,
    current_user,
    account,
    *,
    error: str = "",
    success: str = "",
    search_result=None,
    query: str = "",
    days: int = 90,
    max_messages: int = 120,
):
    return {
        "request": request,
        "current_user": current_user,
        "account": account,
        "providers": mps.PROVIDER_PRESETS,
        "error": error,
        "success": success,
        "search_result": search_result,
        "query": query,
        "days": days,
        "max_messages": max_messages,
    }


def _ssl_from_form(use_ssl: Optional[str], default: bool = True) -> bool:
    if use_ssl is None:
        return default
    return use_ssl in ("1", "on", "true", "True", "yes")


@router.get("/", response_class=HTMLResponse)
async def mail_parser_page(request: Request, db: Session = Depends(get_db)):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    account = mps.get_account_for_user(db, current_user.id)
    return templates.TemplateResponse(
        "mail_parser.html",
        _page_context(request, current_user, account),
    )


@router.post("/settings", response_class=HTMLResponse)
async def save_mailbox_settings(
    request: Request,
    provider: str = Form("yandex"),
    email: str = Form(...),
    password: Optional[str] = Form(None),
    imap_host: Optional[str] = Form(None),
    imap_port: Optional[int] = Form(None),
    use_ssl: Optional[str] = Form(None),
    folder: str = Form("INBOX"),
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    account = mps.get_account_for_user(db, current_user.id)
    error = ""
    success = ""
    try:
        account = mps.upsert_mailbox_account(
            db,
            current_user,
            provider=(provider or "custom").strip().lower(),
            email_addr=email,
            password=(password or "").strip() or None,
            imap_host=(imap_host or "").strip() or None,
            imap_port=imap_port,
            use_ssl=_ssl_from_form(use_ssl, default=True),
            folder=(folder or "INBOX").strip() or "INBOX",
        )
        success = "Настройки почтового ящика сохранены."
    except Exception as exc:
        error = str(exc)
        account = mps.get_account_for_user(db, current_user.id)

    return templates.TemplateResponse(
        "mail_parser.html",
        _page_context(request, current_user, account, error=error, success=success),
        status_code=400 if error else 200,
    )


@router.post("/test", response_class=HTMLResponse)
async def test_mailbox(
    request: Request,
    provider: str = Form("yandex"),
    email: str = Form(...),
    password: Optional[str] = Form(None),
    imap_host: Optional[str] = Form(None),
    imap_port: Optional[int] = Form(None),
    use_ssl: Optional[str] = Form(None),
    folder: str = Form("INBOX"),
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    account = mps.get_account_for_user(db, current_user.id)
    error = ""
    success = ""

    pwd = (password or "").strip()
    if not pwd and account:
        try:
            pwd = decrypt_secret(account.password_encrypted)
        except Exception:
            pwd = ""

    try:
        default_ssl = bool(account.use_ssl) if account else True
        msg = mps.test_imap_connection(
            provider=(provider or "custom").strip().lower(),
            email_addr=email,
            password=pwd,
            imap_host=(imap_host or "").strip() or None,
            imap_port=imap_port,
            use_ssl=_ssl_from_form(use_ssl, default=default_ssl),
        )
        success = f"Проверка IMAP: {msg}"
    except Exception as exc:
        # test_imap_connection уже отдаёт понятный текст для auth-ошибок
        error = str(exc) if str(exc) else mps.format_imap_auth_error(exc, (provider or "").strip().lower())

    return templates.TemplateResponse(
        "mail_parser.html",
        _page_context(request, current_user, account, error=error, success=success),
        status_code=400 if error else 200,
    )


@router.post("/search", response_class=HTMLResponse)
async def search_mailbox(
    request: Request,
    query: str = Form(...),
    days: int = Form(90),
    max_messages: int = Form(120),
    db: Session = Depends(get_db),
):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    account = mps.get_account_for_user(db, current_user.id)
    error = ""
    search_result = None
    days = max(1, min(int(days or 90), 365))
    max_messages = max(10, min(int(max_messages or 120), 300))
    q = (query or "").strip()

    if not account:
        error = "Сначала сохраните настройки IMAP-ящика."
    elif not q:
        error = "Введите, что нужно найти (тема, смысл, фрагмент текста)."
    else:
        try:
            search_result = mps.sync_and_search(
                db,
                account,
                q,
                days=days,
                max_messages=max_messages,
            )
        except Exception as exc:
            error = f"Ошибка поиска: {exc}"

    return templates.TemplateResponse(
        "mail_parser.html",
        _page_context(
            request,
            current_user,
            account,
            error=error,
            search_result=search_result,
            query=q,
            days=days,
            max_messages=max_messages,
        ),
        status_code=400 if error else 200,
    )


@router.post("/clear-cache")
async def clear_mail_cache(request: Request, db: Session = Depends(get_db)):
    current_user = await get_current_active_user(request, db)
    enforce_not_viewer(current_user)
    account = mps.get_account_for_user(db, current_user.id)
    if account:
        db.query(models.MailMessageCache).filter(
            models.MailMessageCache.account_id == account.id
        ).delete()
        db.commit()
    return RedirectResponse(url="/mail-parser/", status_code=303)
