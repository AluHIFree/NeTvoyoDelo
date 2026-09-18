"""
Модели данных системы входящей корреспонденции.

ВАЖНО: User.department — строка (String) с русским названием отдела
(например «Школьный отдел»), а не Enum. В шаблонах используйте
current_user.department БЕЗ .value.

Enum Department сохранён для сидирования и обратной совместимости кода,
который перечисляет стандартные отделы.
"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Date,
    DateTime,
    Text,
    Boolean,
    ForeignKey,
    Enum,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

from app.database import Base


# --- Роли пользователей (строковые константы) ---
class UserRole:
    EXECUTOR = "executor"
    CONTROLLER = "controller"
    VIEWER = "viewer"

    ALL = (EXECUTOR, CONTROLLER, VIEWER)


# Перечисление для статуса письма
class CorrespondenceStatus(str, enum.Enum):
    PENDING = "В ожидании"
    IN_PROGRESS = "В работе"
    COMPLETED = "Исполнено"
    EXPIRED = "Просрочено"
    TRANSFERRED = "Передано"


# Перечисление для отделов (сидирование / обратная совместимость)
class Department(str, enum.Enum):
    SCHOOL_DEPARTMENT = "Школьный отдел"
    GIA_DEPARTMENT = "Отдел ГИА"
    DEPARTEMENT = "Департмент"


# Перечисление для типов запросов
class RequestType(str, enum.Enum):
    OWN_PURPOSES = "Для собственных целей"
    MINISTRY_OF_EDUCATION = "Для Министерства просвещения России"


# --- Отделы (админ-настраиваемые) ---
class DepartmentEntity(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# --- Пользователь ---
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=False)
    hashed_password = Column(String, nullable=False)
    # Русское название отдела (строка), например «Школьный отдел»
    department = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    phone_number = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Роль: executor | controller | viewer
    role = Column(String, default=UserRole.EXECUTOR, nullable=False)

    # Настройки уведомлений
    notify_email = Column(Boolean, default=True)
    notify_sms = Column(Boolean, default=True)
    notify_browser = Column(Boolean, default=True)
    quiet_hours_start = Column(Integer, nullable=True)  # час 0–23
    quiet_hours_end = Column(Integer, nullable=True)
    reminder_days_before = Column(Integer, default=5)
    avatar_path = Column(String, nullable=True)

    # Связи
    assigned_correspondences = relationship(
        "Correspondence",
        foreign_keys="Correspondence.executor_id",
        back_populates="executor",
    )
    created_correspondences = relationship(
        "Correspondence",
        foreign_keys="Correspondence.created_by_id",
        back_populates="created_by",
    )
    received_transfers = relationship(
        "CorrespondenceTransfer",
        foreign_keys="CorrespondenceTransfer.transferred_to_id",
        back_populates="transferred_to",
    )
    sent_transfers = relationship(
        "CorrespondenceTransfer",
        foreign_keys="CorrespondenceTransfer.transferred_by_id",
        back_populates="transferred_by",
    )
    notifications = relationship("Notification", back_populates="user")
    notification_reminders = relationship("NotificationReminder", back_populates="user")
    delivery_logs = relationship("NotificationDeliveryLog", back_populates="user")
    history_entries = relationship("CorrespondenceHistory", back_populates="user")
    attachments_uploaded = relationship(
        "CorrespondenceAttachment",
        back_populates="uploaded_by",
    )
    saved_filters = relationship("SavedFilter", back_populates="user")
    work_plan_notes = relationship("WorkPlanNote", back_populates="user")


# --- Входящая корреспонденция ---
class Correspondence(Base):
    __tablename__ = "correspondences"

    id = Column(Integer, primary_key=True, index=True)

    incoming_number = Column(String, nullable=False)
    incoming_date = Column(Date, nullable=False)
    received_date = Column(Date, nullable=False)
    sender = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    deadline = Column(Date, nullable=False)
    status = Column(
        Enum(CorrespondenceStatus),
        default=CorrespondenceStatus.PENDING,
    )

    flow = Column(String, nullable=True, index=True)

    request_type = Column(Enum(RequestType), nullable=True)
    request_note = Column(Text, nullable=True)

    sent_info = Column(String, nullable=True)
    to_whom = Column(String, nullable=True)
    control = Column(String, nullable=True)
    report = Column(String, nullable=True)
    report_date = Column(Date, nullable=True)

    executor_id = Column(Integer, ForeignKey("users.id"))
    created_by_id = Column(Integer, ForeignKey("users.id"))

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    notification_sent = Column(Boolean, default=False)
    completed_at = Column(DateTime, nullable=True)
    completion_note = Column(Text, nullable=True)

    # Ждём внешний ответ (не в личной нагрузке / просрочке)
    waiting_external = Column(Boolean, default=False, nullable=False)
    waiting_external_note = Column(String, nullable=True)

    executor = relationship(
        "User",
        foreign_keys=[executor_id],
        back_populates="assigned_correspondences",
    )
    created_by = relationship(
        "User",
        foreign_keys=[created_by_id],
        back_populates="created_correspondences",
    )
    transfers = relationship("CorrespondenceTransfer", back_populates="correspondence")
    notifications = relationship("Notification", back_populates="correspondence")
    reminders = relationship("NotificationReminder", back_populates="correspondence")
    delivery_logs = relationship("NotificationDeliveryLog", back_populates="correspondence")
    history = relationship("CorrespondenceHistory", back_populates="correspondence")
    attachments = relationship("CorrespondenceAttachment", back_populates="correspondence")
    outgoing_links = relationship(
        "CorrespondenceLink",
        foreign_keys="CorrespondenceLink.source_id",
        back_populates="source",
        cascade="all, delete-orphan",
    )
    incoming_links = relationship(
        "CorrespondenceLink",
        foreign_keys="CorrespondenceLink.target_id",
        back_populates="target",
    )


# --- Связи между письмами (платформа или внешние реквизиты) ---
class CorrespondenceLink(Base):
    """
    Связь письма с другим документом.
    Либо target_id (письмо на платформе), либо external_* (ещё не заведено).
    """

    __tablename__ = "correspondence_links"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("correspondences.id"), nullable=False, index=True)
    target_id = Column(Integer, ForeignKey("correspondences.id"), nullable=True, index=True)

    # Внешние реквизиты (если письма ещё нет в системе)
    external_number = Column(String, nullable=True)
    external_date = Column(Date, nullable=True)
    external_sender = Column(String, nullable=True)
    external_note = Column(Text, nullable=True)

    # related | reply_to | follow_up | supersedes
    link_type = Column(String, default="related", nullable=False)
    note = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    source = relationship(
        "Correspondence",
        foreign_keys=[source_id],
        back_populates="outgoing_links",
    )
    target = relationship(
        "Correspondence",
        foreign_keys=[target_id],
        back_populates="incoming_links",
    )
    created_by = relationship("User")


# --- Передача письма ---
class CorrespondenceTransfer(Base):
    __tablename__ = "correspondence_transfers"

    id = Column(Integer, primary_key=True, index=True)
    correspondence_id = Column(Integer, ForeignKey("correspondences.id"), nullable=False)
    transferred_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    transferred_to_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    transfer_date = Column(DateTime, default=datetime.utcnow)
    note = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)

    correspondence = relationship("Correspondence", back_populates="transfers")
    transferred_by = relationship(
        "User",
        foreign_keys=[transferred_by_id],
        back_populates="sent_transfers",
    )
    transferred_to = relationship(
        "User",
        foreign_keys=[transferred_to_id],
        back_populates="received_transfers",
    )


# --- Уведомления (inbox / логирование) ---
class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    correspondence_id = Column(Integer, ForeignKey("correspondences.id"), nullable=True)
    notification_type = Column(String)  # email, sms, browser
    sent_at = Column(DateTime, default=datetime.utcnow)
    is_read = Column(Boolean, default=False)
    message = Column(Text)

    user = relationship("User", back_populates="notifications")
    correspondence = relationship("Correspondence", back_populates="notifications")


# --- Напоминания (идемпотентность по stage) ---
class NotificationReminder(Base):
    __tablename__ = "notification_reminders"
    __table_args__ = (
        UniqueConstraint(
            "correspondence_id",
            "user_id",
            "stage",
            name="uq_reminder_corr_user_stage",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    correspondence_id = Column(Integer, ForeignKey("correspondences.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    stage = Column(String, nullable=False)  # e.g. "5_days", "1_day", "expired"
    sent_at = Column(DateTime, default=datetime.utcnow)

    correspondence = relationship("Correspondence", back_populates="reminders")
    user = relationship("User", back_populates="notification_reminders")


# --- Лог доставки уведомлений ---
class NotificationDeliveryLog(Base):
    __tablename__ = "notification_delivery_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    correspondence_id = Column(Integer, ForeignKey("correspondences.id"), nullable=True)
    channel = Column(String, nullable=False)  # email, sms, browser
    status = Column(String, nullable=False)  # sent, failed, skipped
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="delivery_logs")
    correspondence = relationship("Correspondence", back_populates="delivery_logs")


# --- История изменений письма ---
class CorrespondenceHistory(Base):
    __tablename__ = "correspondence_history"

    id = Column(Integer, primary_key=True, index=True)
    correspondence_id = Column(Integer, ForeignKey("correspondences.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String, nullable=False)  # created, updated, transferred, completed, ...
    field_name = Column(String, nullable=True)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    correspondence = relationship("Correspondence", back_populates="history")
    user = relationship("User", back_populates="history_entries")


# --- Вложения ---
class CorrespondenceAttachment(Base):
    __tablename__ = "correspondence_attachments"

    id = Column(Integer, primary_key=True, index=True)
    correspondence_id = Column(Integer, ForeignKey("correspondences.id"), nullable=False)
    uploaded_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    filename = Column(String, nullable=False)
    stored_path = Column(String, nullable=False)
    content_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    correspondence = relationship("Correspondence", back_populates="attachments")
    uploaded_by = relationship("User", back_populates="attachments_uploaded")


# --- Сохранённые фильтры ---
class SavedFilter(Base):
    __tablename__ = "saved_filters"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    filters_json = Column(Text, nullable=False)  # JSON-строка
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="saved_filters")


# --- Заметки плана дня (как в todo) ---
class WorkPlanNote(Base):
    __tablename__ = "work_plan_notes"
    __table_args__ = (
        UniqueConstraint("user_id", "correspondence_id", name="uq_work_plan_note_user_corr"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    correspondence_id = Column(
        Integer, ForeignKey("correspondences.id"), nullable=False, index=True
    )
    text = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="work_plan_notes")
    correspondence = relationship("Correspondence")


# --- IMAP-ящик пользователя (парсер почты) ---
class MailboxAccount(Base):
    """Персональные IMAP-настройки. Пароль хранится только в зашифрованном виде."""

    __tablename__ = "mailbox_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    provider = Column(String, nullable=False, default="yandex")  # yandex|mailru|gmail|custom
    email = Column(String, nullable=False)
    imap_host = Column(String, nullable=False)
    imap_port = Column(Integer, nullable=False, default=993)
    use_ssl = Column(Boolean, nullable=False, default=True)
    password_encrypted = Column(Text, nullable=False)
    folder = Column(String, nullable=False, default="INBOX")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_checked_at = Column(DateTime, nullable=True)

    user = relationship("User", backref="mailbox_account")
    messages = relationship(
        "MailMessageCache",
        back_populates="account",
        cascade="all, delete-orphan",
    )


class MailMessageCache(Base):
    """Кэш распарсенных писем для повторного поиска без полной перекачки."""

    __tablename__ = "mail_message_cache"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "folder",
            "message_uid",
            name="uq_mail_cache_account_folder_uid",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("mailbox_accounts.id"), nullable=False, index=True)
    folder = Column(String, nullable=False, default="INBOX")
    message_uid = Column(String, nullable=False)
    message_id_header = Column(String, nullable=True, index=True)
    subject = Column(String, nullable=True)
    from_addr = Column(String, nullable=True)
    sent_at = Column(DateTime, nullable=True, index=True)
    body_text = Column(Text, nullable=True)
    attachments_text = Column(Text, nullable=True)
    attachment_names = Column(Text, nullable=True)  # через ;
    indexed_at = Column(DateTime, default=datetime.utcnow)

    account = relationship("MailboxAccount", back_populates="messages")
