"""
Единый SMTP-отправитель (smtplib): SSL и STARTTLS.
Читает настройки из .env через config_env (SMTP_USERNAME / SMTP_USER и т.д.).
"""
from __future__ import annotations

import asyncio
import mimetypes
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from email.utils import formataddr
from pathlib import Path
from typing import List, Optional, Tuple, Union

from app.config_env import get_env


def _smtp_settings() -> dict:
    """Актуальные SMTP-настройки (с алиасами под старые имена переменных)."""
    host = get_env("SMTP_HOST", "smtp.yandex.ru")
    port_raw = get_env("SMTP_PORT", "587") or "587"
    try:
        port = int(port_raw)
    except ValueError:
        port = 587

    user = get_env("SMTP_USERNAME") or get_env("SMTP_USER")
    password = get_env("SMTP_PASSWORD")
    from_email = get_env("SMTP_FROM_EMAIL") or get_env("SMTP_FROM") or user
    from_name = get_env("SMTP_FROM_NAME") or "NeTvoyoDelo"

    use_tls_raw = get_env("SMTP_USE_TLS")
    use_ssl_raw = get_env("SMTP_USE_SSL")

    if use_tls_raw:
        use_tls = use_tls_raw.lower() in ("1", "true", "yes")
        use_ssl = (
            False
            if use_tls
            else (use_ssl_raw.lower() in ("1", "true", "yes") if use_ssl_raw else False)
        )
    elif use_ssl_raw:
        use_ssl = use_ssl_raw.lower() in ("1", "true", "yes")
        use_tls = not use_ssl and port == 587
    else:
        use_ssl = port == 465
        use_tls = not use_ssl

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_email": from_email,
        "from_name": from_name,
        "use_ssl": use_ssl,
        "use_tls": use_tls,
        "public_base_url": get_env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
    }


PUBLIC_BASE_URL = get_env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Вложения для пересылки карточки письма
ALLOWED_EMAIL_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".txt",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
}
MAX_EMAIL_ATTACHMENT_BYTES = 15 * 1024 * 1024  # 15 MB


def _build_html(
    message: str,
    link_url: Optional[str] = None,
    brand: str = "NeTvoyoDelo",
    button_label: str = "Перейти в систему",
) -> str:
    settings = _smtp_settings()
    button_href = link_url or settings["public_base_url"]
    safe_message = (
        (message or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )
    safe_brand = (
        (brand or settings["from_name"] or "Уведомление")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    safe_btn = (
        (button_label or "Перейти в систему")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.6; color: #333; }}
    .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
    .header {{ background: #1e3a5f; color: white; padding: 20px; text-align: center; border-radius: 8px 8px 0 0; }}
    .content {{ background: #f9fafb; padding: 20px; border-radius: 0 0 8px 8px; }}
    .message-box {{ background: white; padding: 15px; border-radius: 6px; border-left: 4px solid #1e3a5f; margin: 15px 0; }}
    .button {{ display: inline-block; padding: 10px 20px; background: #1e3a5f; color: white !important;
               text-decoration: none; border-radius: 5px; margin-top: 15px; }}
    .footer {{ text-align: center; padding: 15px; font-size: 12px; color: #666; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h2>{safe_brand}</h2>
      <p>Автоматическое уведомление</p>
    </div>
    <div class="content">
      <h3>Уважаемый сотрудник!</h3>
      <div class="message-box"><p>{safe_message}</p></div>
      <p style="text-align: center;">
        <a href="{button_href}" class="button">{safe_btn}</a>
      </p>
    </div>
    <div class="footer">
      <p>Это автоматическое сообщение, пожалуйста, не отвечайте на него.</p>
    </div>
  </div>
</body>
</html>"""


def _from_header(from_email: str, from_name: str) -> str:
    if from_name:
        return formataddr((from_name, from_email))
    return from_email


AttachmentInput = Union[
    Tuple[str, bytes, Optional[str]],  # (filename, data, content_type)
    Tuple[str, Path],  # (filename, path)
]


def _attach_file(msg: MIMEMultipart, filename: str, data: bytes, content_type: Optional[str] = None) -> None:
    guessed, _ = mimetypes.guess_type(filename)
    ctype = content_type or guessed or "application/octet-stream"
    maintype, _, subtype = ctype.partition("/")
    if not subtype:
        maintype, subtype = "application", "octet-stream"

    if maintype == "text":
        try:
            part = MIMEText(data.decode("utf-8"), _subtype=subtype or "plain", _charset="utf-8")
        except UnicodeDecodeError:
            part = MIMEApplication(data, Name=filename)
            part["Content-Disposition"] = f'attachment; filename="{filename}"'
            msg.attach(part)
            return
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)
        return

    if maintype == "image":
        part = MIMEImage(data, _subtype=subtype or "jpeg")
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)
        return

    if maintype == "application":
        part = MIMEApplication(data, Name=filename)
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(part)
        return

    part = MIMEBase(maintype, subtype)
    part.set_payload(data)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)


def _send_smtp_sync(
    to_email: str,
    subject: str,
    html_body: str,
    attachments: Optional[List[Tuple[str, bytes, Optional[str]]]] = None,
) -> bool:
    cfg = _smtp_settings()
    if not cfg["host"] or not cfg["user"] or not cfg["password"]:
        print("[WARN] SMTP не настроен. Пропускаем отправку email.")
        return False
    if not cfg["from_email"]:
        print("[WARN] SMTP_FROM_EMAIL не задан. Пропускаем отправку email.")
        return False

    if attachments:
        msg = MIMEMultipart("mixed")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
        for filename, data, ctype in attachments:
            _attach_file(msg, filename, data, ctype)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    msg["From"] = _from_header(cfg["from_email"], cfg["from_name"])
    msg["To"] = to_email
    msg["Subject"] = subject

    context = ssl.create_default_context()
    if cfg["use_ssl"]:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=context, timeout=30) as server:
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
            server.ehlo()
            if cfg["use_tls"]:
                server.starttls(context=context)
                server.ehlo()
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
    return True


async def send_email(
    to_email: str,
    subject: str,
    message: str,
    link_url: Optional[str] = None,
    button_label: str = "Перейти в систему",
) -> bool:
    """Отправка HTML-письма. Кнопка ведёт на link_url или на главную."""
    if not to_email:
        return False

    cfg = _smtp_settings()
    global PUBLIC_BASE_URL
    PUBLIC_BASE_URL = cfg["public_base_url"]

    html_body = _build_html(
        message,
        link_url=link_url,
        brand=cfg["from_name"],
        button_label=button_label,
    )
    try:
        ok = await asyncio.to_thread(_send_smtp_sync, to_email, subject, html_body, None)
    except Exception as e:
        print(f"[ERR] Ошибка отправки email на {to_email}: {e}")
        return False
    if ok:
        print(f"[OK] Email отправлен на {to_email}")
    return ok


async def send_html_email(
    to_email: str,
    subject: str,
    html_body: str,
    attachments: Optional[List[Tuple[str, bytes, Optional[str]]]] = None,
) -> bool:
    """Отправка готового HTML с опциональными вложениями."""
    if not to_email or not html_body:
        return False
    try:
        ok = await asyncio.to_thread(
            _send_smtp_sync, to_email, subject, html_body, attachments or None
        )
    except Exception as e:
        print(f"[ERR] Ошибка отправки HTML email на {to_email}: {e}")
        return False
    if ok:
        print(f"[OK] HTML email отправлен на {to_email}")
    return ok


def smtp_is_configured() -> bool:
    cfg = _smtp_settings()
    return bool(cfg["host"] and cfg["user"] and cfg["password"] and cfg["from_email"])


def describe_smtp() -> Tuple[str, int, bool, bool]:
    """(host, port, use_tls, use_ssl) — для диагностики."""
    cfg = _smtp_settings()
    return cfg["host"], cfg["port"], cfg["use_tls"], cfg["use_ssl"]


def is_allowed_email_attachment(filename: str) -> bool:
    ext = Path(filename or "").suffix.lower()
    return ext in ALLOWED_EMAIL_EXTENSIONS
