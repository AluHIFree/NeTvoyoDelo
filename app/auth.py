from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from dotenv import load_dotenv
import os

from app.database import get_db
from app import models, schemas

# Загрузка переменных окружения
load_dotenv()

# Настройки для хеширования паролей
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Настройки для JWT
SECRET_KEY = os.getenv("SECRET_KEY", "your-super-secret-key-change-this-1234567890")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))
PASSWORD_RESET_EXPIRE_MINUTES = int(os.getenv("PASSWORD_RESET_EXPIRE_MINUTES", "60"))

# OAuth2 схема для получения токена
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# --- Функции для работы с паролями ---
def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверка пароля"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Хеширование пароля"""
    return pwd_context.hash(password)

# --- Функции для работы с пользователями ---
def get_user_by_username(db: Session, username: str):
    """Получение пользователя по имени"""
    return db.query(models.User).filter(models.User.username == username).first()

def get_user_by_email(db: Session, email: str):
    """Получение пользователя по email"""
    return db.query(models.User).filter(models.User.email == email).first()

def get_user_by_id(db: Session, user_id: int):
    """Получение пользователя по ID"""
    return db.query(models.User).filter(models.User.id == user_id).first()

def authenticate_user(db: Session, username: str, password: str):
    """Аутентификация пользователя"""
    user = get_user_by_username(db, username)
    if not user:
        return False
    if not verify_password(password, user.hashed_password):
        return False
    if not user.is_active:
        return False
    return user

# --- Функции для работы с JWT токенами ---
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Создание JWT токена"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def _password_fingerprint(hashed_password: str) -> str:
    """Короткий отпечаток хеша — ссылка сброса станет недействительной после смены пароля."""
    return (hashed_password or "")[-12:]


def create_password_reset_token(user: models.User) -> str:
    """JWT для сброса пароля (одноразовый по смыслу: привязан к текущему хешу)."""
    return create_access_token(
        data={
            "sub": user.email,
            "user_id": user.id,
            "purpose": "password_reset",
            "pwd_fp": _password_fingerprint(user.hashed_password),
        },
        expires_delta=timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES),
    )


def verify_password_reset_token(token: str, db: Session) -> Optional[models.User]:
    """Проверить токен сброса и вернуть пользователя или None."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None

    if payload.get("purpose") != "password_reset":
        return None

    user_id = payload.get("user_id")
    email = payload.get("sub")
    if user_id is None or not email:
        return None

    user = get_user_by_id(db, user_id=int(user_id))
    if not user or not user.is_active:
        return None
    if (user.email or "").lower() != str(email).lower():
        return None
    if payload.get("pwd_fp") != _password_fingerprint(user.hashed_password):
        return None
    return user

async def get_current_user(
    request: Request,
    db: Session = Depends(get_db)
):
    """Получение текущего пользователя из токена в cookie"""
    # Получаем токен из cookie
    token = request.cookies.get("access_token")
    
    if not token:
        return None
    
    # Убираем префикс "Bearer " если есть
    if token.startswith("Bearer "):
        token = token[7:]
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        user_id: int = payload.get("user_id")
        if username is None or user_id is None:
            return None
    except JWTError:
        return None
    
    user = get_user_by_id(db, user_id=user_id)
    if user is None or not user.is_active:
        return None
    
    return user

async def get_current_active_user(
    request: Request,
    db: Session = Depends(get_db)
):
    """Получение активного пользователя (с проверкой)"""
    current_user = await get_current_user(request, db)
    
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is blocked"
        )
    return current_user

async def get_current_admin_user(
    request: Request,
    db: Session = Depends(get_db)
):
    """Получение текущего пользователя-админа"""
    current_user = await get_current_active_user(request, db)
    
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions. Admin access required."
        )
    return current_user

# --- Функция для создания первого админа ---
def create_first_admin(db: Session):
    """
    Создание первого администратора, если его нет.
    Учётные данные: ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_EMAIL из окружения,
    иначе дефолты с предупреждением сменить пароль (пароль в stdout не печатается).
    """
    admin = db.query(models.User).filter(models.User.is_admin == True).first()
    if admin:
        return

    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD", "admin123")
    email = os.getenv("ADMIN_EMAIL", "admin@example.com")
    from_env = bool(
        os.getenv("ADMIN_USERNAME")
        or os.getenv("ADMIN_PASSWORD")
        or os.getenv("ADMIN_EMAIL")
    )

    admin_user = models.User(
        email=email,
        username=username,
        full_name="System Administrator",
        hashed_password=get_password_hash(password),
        department="Департмент",
        role=models.UserRole.CONTROLLER,
        is_active=True,
        is_admin=True,
        phone_number="+70000000000",
        notify_email=True,
        notify_sms=True,
        notify_browser=True,
        reminder_days_before=5,
    )
    db.add(admin_user)
    db.commit()

    if from_env:
        print(f"Создан администратор из переменных окружения: {username}")
    else:
        print(
            "WARNING: создан администратор по умолчанию (admin). "
            "Смените пароль или задайте ADMIN_USERNAME / ADMIN_PASSWORD / ADMIN_EMAIL."
        )
