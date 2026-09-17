from datetime import timedelta
from fastapi import APIRouter, Depends, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app import models, schemas, auth
from app.auth import (
    authenticate_user,
    create_access_token,
    get_password_hash,
    create_password_reset_token,
    verify_password_reset_token,
    PASSWORD_RESET_EXPIRE_MINUTES,
)
from app.dependencies import list_department_names
from app.config_env import get_env
from app.utils.email_sender import send_email, smtp_is_configured

router = APIRouter(prefix="/api/auth", tags=["authentication"])
templates = Jinja2Templates(directory="app/templates")


def _auth_page(request: Request, template: str, status_code: int = 200, **ctx):
    return templates.TemplateResponse(
        template,
        {"request": request, **ctx},
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    error: Optional[str] = None,
    success: Optional[str] = None,
):
    """Страница входа"""
    success_msg = None
    if success == "password_reset":
        success_msg = "Пароль успешно изменён. Войдите с новым паролем."
    elif success:
        success_msg = success
    return _auth_page(request, "login.html", error=error, success=success_msg)


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Страница регистрации"""
    return _auth_page(
        request,
        "register.html",
        error=error,
        departments=list_department_names(db, active_only=True),
    )


@router.get("/logout")
async def logout():
    """Выход из системы"""
    response = RedirectResponse(url="/api/auth/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("access_token")
    return response


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Обработка входа в систему"""
    user = authenticate_user(db, username, password)
    if not user:
        return _auth_page(
            request,
            "login.html",
            status_code=status.HTTP_401_UNAUTHORIZED,
            error="Неверное имя пользователя или пароль",
        )

    access_token_expires = timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username, "user_id": user.id},
        expires_delta=access_token_expires,
    )

    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        max_age=1800,
        expires=1800,
    )
    return response


@router.post("/register")
async def register(
    request: Request,
    email: str = Form(...),
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    department: str = Form(...),
    phone_number: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Регистрация: department — строка из DepartmentEntity."""
    departments = list_department_names(db, active_only=True)

    def _reg_error(msg: str):
        return _auth_page(
            request,
            "register.html",
            status_code=status.HTTP_400_BAD_REQUEST,
            error=msg,
            departments=departments,
        )

    if password != confirm_password:
        return _reg_error("Пароли не совпадают")

    if auth.get_user_by_username(db, username):
        return _reg_error("Пользователь с таким именем уже существует")

    if auth.get_user_by_email(db, email):
        return _reg_error("Пользователь с таким email уже существует")

    dept_name = department.strip()
    if dept_name not in departments:
        return _reg_error("Неверный отдел")

    hashed_password = get_password_hash(password)
    new_user = models.User(
        email=email,
        username=username,
        full_name=full_name,
        hashed_password=hashed_password,
        department=dept_name,
        role=models.UserRole.EXECUTOR,
        is_active=True,
        is_admin=False,
        phone_number=phone_number,
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    access_token_expires = timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": new_user.username, "user_id": new_user.id},
        expires_delta=access_token_expires,
    )

    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        max_age=1800,
        expires=1800,
    )
    return response


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(
    request: Request,
    error: Optional[str] = None,
    success: Optional[str] = None,
):
    """Форма запроса ссылки для сброса пароля."""
    return _auth_page(
        request,
        "forgot_password.html",
        error=error,
        success=success,
        smtp_ok=smtp_is_configured(),
    )


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    """Отправить письмо со ссылкой на сброс (ответ одинаковый, чтобы не раскрывать email)."""
    email_norm = (email or "").strip()
    generic_ok = (
        "Если аккаунт с таким email существует, мы отправили ссылку для сброса пароля. "
        "Проверьте почту и папку «Спам»."
    )

    if not smtp_is_configured():
        return _auth_page(
            request,
            "forgot_password.html",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            error="Отправка почты временно недоступна. Обратитесь к администратору.",
            smtp_ok=False,
        )

    user = auth.get_user_by_email(db, email_norm)
    if not user:
        user = (
            db.query(models.User)
            .filter(models.User.email.ilike(email_norm))
            .first()
        )

    if user and user.is_active:
        token = create_password_reset_token(user)
        base = get_env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
        reset_url = f"{base}/api/auth/reset-password?token={token}"
        minutes = PASSWORD_RESET_EXPIRE_MINUTES
        message = (
            f"Вы запросили сброс пароля для аккаунта {user.username}.\n\n"
            f"Нажмите кнопку ниже, чтобы задать новый пароль. "
            f"Ссылка действует {minutes} мин.\n\n"
            f"Если вы не запрашивали сброс — просто проигнорируйте это письмо."
        )
        sent = await send_email(
            user.email,
            "Сброс пароля — NeTvoyoDelo",
            message,
            link_url=reset_url,
            button_label="Сбросить пароль",
        )
        if not sent:
            return _auth_page(
                request,
                "forgot_password.html",
                status_code=status.HTTP_502_BAD_GATEWAY,
                error="Не удалось отправить письмо. Попробуйте позже или обратитесь к администратору.",
                smtp_ok=True,
            )

    return _auth_page(
        request,
        "forgot_password.html",
        success=generic_ok,
        smtp_ok=True,
    )


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(
    request: Request,
    token: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Форма нового пароля по ссылке из письма."""
    user = verify_password_reset_token(token or "", db)
    if not user:
        return _auth_page(
            request,
            "reset_password.html",
            status_code=status.HTTP_400_BAD_REQUEST,
            token=None,
            error="Ссылка недействительна или устарела. Запросите сброс пароля снова.",
        )
    return _auth_page(request, "reset_password.html", token=token, error=None)


@router.post("/reset-password", response_class=HTMLResponse)
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Сохранить новый пароль по токену."""
    user = verify_password_reset_token(token, db)
    if not user:
        return _auth_page(
            request,
            "reset_password.html",
            status_code=status.HTTP_400_BAD_REQUEST,
            token=None,
            error="Ссылка недействительна или устарела. Запросите сброс пароля снова.",
        )

    if password != confirm_password:
        return _auth_page(
            request,
            "reset_password.html",
            status_code=status.HTTP_400_BAD_REQUEST,
            token=token,
            error="Пароли не совпадают",
        )

    if len(password) < 6:
        return _auth_page(
            request,
            "reset_password.html",
            status_code=status.HTTP_400_BAD_REQUEST,
            token=token,
            error="Пароль должен содержать минимум 6 символов",
        )

    user.hashed_password = get_password_hash(password)
    db.commit()

    return RedirectResponse(
        url="/api/auth/login?success=password_reset",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/me", response_model=schemas.UserResponse)
async def read_users_me(current_user: models.User = Depends(auth.get_current_active_user)):
    """Получение информации о текущем пользователе (API)"""
    return current_user
