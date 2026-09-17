from pydantic import BaseModel, EmailStr, Field, ConfigDict
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from enum import Enum


# Перечисления для Pydantic
class DepartmentEnum(str, Enum):
    """Стандартные отделы (для валидации/форм). Пользователь хранит str."""
    SCHOOL_DEPARTMENT = "Школьный отдел"
    GIA_DEPARTMENT = "Отдел ГИА"
    DEPARTEMENT = "Департмент"


class UserRoleEnum(str, Enum):
    EXECUTOR = "executor"
    CONTROLLER = "controller"
    VIEWER = "viewer"


class CorrespondenceStatusEnum(str, Enum):
    PENDING = "В ожидании"
    IN_PROGRESS = "В работе"
    COMPLETED = "Исполнено"
    EXPIRED = "Просрочено"
    TRANSFERRED = "Передано"


# --- Схемы для пользователей ---
class UserBase(BaseModel):
    email: EmailStr
    username: str
    full_name: str
    department: str  # русское название отдела
    phone_number: Optional[str] = None


class UserCreate(UserBase):
    password: str
    role: UserRoleEnum = UserRoleEnum.EXECUTOR


class NotificationPrefsUpdate(BaseModel):
    notify_email: Optional[bool] = None
    notify_sms: Optional[bool] = None
    notify_browser: Optional[bool] = None
    quiet_hours_start: Optional[int] = Field(None, ge=0, le=23)
    quiet_hours_end: Optional[int] = Field(None, ge=0, le=23)
    reminder_days_before: Optional[int] = Field(None, ge=0, le=90)


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    username: Optional[str] = None
    full_name: Optional[str] = None
    department: Optional[str] = None
    phone_number: Optional[str] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    role: Optional[UserRoleEnum] = None
    notify_email: Optional[bool] = None
    notify_sms: Optional[bool] = None
    notify_browser: Optional[bool] = None
    quiet_hours_start: Optional[int] = Field(None, ge=0, le=23)
    quiet_hours_end: Optional[int] = Field(None, ge=0, le=23)
    reminder_days_before: Optional[int] = Field(None, ge=0, le=90)
    avatar_path: Optional[str] = None


class UserResponse(UserBase):
    id: int
    is_active: bool
    is_admin: bool
    role: str = UserRoleEnum.EXECUTOR.value
    notify_email: bool = True
    notify_sms: bool = True
    notify_browser: bool = True
    quiet_hours_start: Optional[int] = None
    quiet_hours_end: Optional[int] = None
    reminder_days_before: int = 5
    avatar_path: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserLogin(BaseModel):
    username: str
    password: str


# --- Отделы ---
class DepartmentBase(BaseModel):
    name: str
    is_active: bool = True


class DepartmentCreate(DepartmentBase):
    pass


class DepartmentUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


class DepartmentResponse(DepartmentBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Схемы для корреспонденции ---
class CorrespondenceBase(BaseModel):
    incoming_number: str = Field(..., description="Входящий номер, например: 642/07-25")
    incoming_date: date = Field(..., description="Дата входящего документа")
    received_date: date = Field(..., description="Дата поступления")
    sender: str = Field(..., description="Отправитель")
    content: str = Field(..., description="Содержание")
    deadline: date = Field(..., description="Срок исполнения")
    status: CorrespondenceStatusEnum = CorrespondenceStatusEnum.PENDING
    sent_info: Optional[str] = Field(None, description="Отправлено")
    to_whom: Optional[str] = Field(None, description="Кому")
    control: Optional[str] = Field(None, description="Контроль")
    report: Optional[str] = Field(None, description="Отчет")
    report_date: Optional[date] = Field(None, description="Дата отчета")
    executor_id: Optional[int] = Field(None, description="ID исполнителя")
    completion_note: Optional[str] = Field(None, description="Примечание при исполнении")
    waiting_external: bool = Field(False, description="Ждём внешний ответ")
    waiting_external_note: Optional[str] = Field(None, description="Комментарий к ожиданию")


class CorrespondenceCreate(CorrespondenceBase):
    pass


class CorrespondenceUpdate(BaseModel):
    incoming_number: Optional[str] = None
    incoming_date: Optional[date] = None
    received_date: Optional[date] = None
    sender: Optional[str] = None
    content: Optional[str] = None
    deadline: Optional[date] = None
    status: Optional[CorrespondenceStatusEnum] = None
    sent_info: Optional[str] = None
    to_whom: Optional[str] = None
    control: Optional[str] = None
    report: Optional[str] = None
    report_date: Optional[date] = None
    executor_id: Optional[int] = None
    completed_at: Optional[datetime] = None
    completion_note: Optional[str] = None
    waiting_external: Optional[bool] = None
    waiting_external_note: Optional[str] = None


class CorrespondenceResponse(CorrespondenceBase):
    id: int
    created_by_id: int
    created_at: datetime
    updated_at: datetime
    notification_sent: bool
    completed_at: Optional[datetime] = None
    executor: Optional[UserResponse] = None
    created_by: Optional[UserResponse] = None

    model_config = ConfigDict(from_attributes=True)


# --- Схемы для передачи писем ---
class CorrespondenceTransferBase(BaseModel):
    correspondence_id: int
    transferred_to_id: int
    note: Optional[str] = None


class CorrespondenceTransferCreate(CorrespondenceTransferBase):
    pass


class CorrespondenceTransferResponse(CorrespondenceTransferBase):
    id: int
    transferred_by_id: int
    transfer_date: datetime
    is_active: bool
    transferred_by: Optional[UserResponse] = None
    transferred_to: Optional[UserResponse] = None
    correspondence: Optional[CorrespondenceResponse] = None

    model_config = ConfigDict(from_attributes=True)


# --- Вложения ---
class AttachmentResponse(BaseModel):
    id: int
    correspondence_id: int
    uploaded_by_id: int
    filename: str
    stored_path: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    uploaded_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- История ---
class HistoryEntryResponse(BaseModel):
    id: int
    correspondence_id: int
    user_id: Optional[int] = None
    action: str
    field_name: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Сохранённые фильтры ---
class SavedFilterBase(BaseModel):
    name: str
    filters_json: str


class SavedFilterCreate(SavedFilterBase):
    pass


class SavedFilterUpdate(BaseModel):
    name: Optional[str] = None
    filters_json: Optional[str] = None


class SavedFilterResponse(SavedFilterBase):
    id: int
    user_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Схемы для уведомлений ---
class NotificationBase(BaseModel):
    user_id: int
    correspondence_id: Optional[int] = None
    notification_type: str
    message: str


class NotificationResponse(NotificationBase):
    id: int
    sent_at: datetime
    is_read: bool

    model_config = ConfigDict(from_attributes=True)


class DeliveryLogResponse(BaseModel):
    id: int
    user_id: int
    correspondence_id: Optional[int] = None
    channel: str
    status: str
    detail: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Схемы для статистики и отчетов ---
class DashboardStats(BaseModel):
    total_correspondences: int
    pending_count: int
    in_progress_count: int
    completed_count: int
    expired_count: int
    near_deadline_count: int  # письма, у которых срок истекает через 5 дней


class UserStatistics(BaseModel):
    user: UserResponse
    assigned_count: int
    completed_count: int
    transferred_count: int


class ReportingStats(BaseModel):
    """Сводка для отчётности / дашборда контролёра."""
    total: int
    by_status: Dict[str, int]
    by_department: Dict[str, int]
    by_executor: Dict[str, int]
    overdue_count: int
    completed_on_time_count: int
    average_days_to_complete: Optional[float] = None
    period_from: Optional[date] = None
    period_to: Optional[date] = None
    extras: Optional[Dict[str, Any]] = None


# --- Схемы для токенов ---
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: Optional[str] = None
    user_id: Optional[int] = None
