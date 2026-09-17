"""Раздел обновлений системы (только администратор)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import models
from app.dependencies import admin_required
from app.services import update_service

router = APIRouter(prefix="/updates", tags=["updates"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def updates_page(
    request: Request,
    current_user: models.User = Depends(admin_required),
):
    meta = update_service.load_meta()
    return templates.TemplateResponse(
        "updates.html",
        {
            "request": request,
            "current_user": current_user,
            "local_version": update_service.local_version(),
            "meta": meta,
            "repo": update_service._repo_slug(),
        },
    )


@router.post("/check")
async def check_updates(
    request: Request,
    current_user: models.User = Depends(admin_required),
):
    info = await update_service.check_for_updates()
    wants_html = "text/html" in (request.headers.get("accept") or "")
    if wants_html and "application/json" not in (request.headers.get("accept") or ""):
        # form submit → redirect back with flash via query
        q = "checked=1"
        if info.error:
            q += "&error=1"
        elif info.update_available:
            q += "&available=1"
        else:
            q += "&uptodate=1"
        return RedirectResponse(url=f"/updates/?{q}", status_code=303)

    return JSONResponse(
        {
            "update_available": info.update_available,
            "local_version": info.local_version,
            "remote_version": info.remote_version,
            "remote_sha": info.remote_sha,
            "release_name": info.release_name,
            "release_url": info.release_url,
            "published_at": info.published_at,
            "changelog": info.changelog,
            "source": info.source,
            "error": info.error,
            "commits": info.commits,
        }
    )


@router.get("/status")
async def updates_status(
    current_user: models.User = Depends(admin_required),
):
    info = await update_service.check_for_updates()
    return JSONResponse(
        {
            "update_available": info.update_available,
            "local_version": info.local_version,
            "remote_version": info.remote_version,
            "remote_sha": info.remote_sha,
            "release_name": info.release_name,
            "release_url": info.release_url,
            "published_at": info.published_at,
            "changelog": info.changelog,
            "source": info.source,
            "error": info.error,
            "commits": info.commits,
            "meta": update_service.load_meta(),
        }
    )


@router.post("/apply")
async def apply_updates(
    request: Request,
    current_user: models.User = Depends(admin_required),
):
    try:
        result = await update_service.apply_update()
    except Exception as exc:
        accept = request.headers.get("accept") or ""
        if "application/json" in accept:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return RedirectResponse(
            url=f"/updates/?apply_error={str(exc)[:180]}",
            status_code=303,
        )

    accept = request.headers.get("accept") or ""
    if "application/json" in accept:
        return JSONResponse(result)

    return RedirectResponse(url="/updates/?applied=1", status_code=303)
