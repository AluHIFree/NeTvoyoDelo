"""
Красивая HTML-карточка письма для отправки по email.
"""
from __future__ import annotations

from datetime import date
from html import escape
from typing import Optional

from app import models
from app.config_env import get_env
from app.services.productivity import STAGE_LABELS, deadline_stage, stage_hint


def _esc(value) -> str:
    if value is None:
        return "—"
    text = str(value).strip()
    return escape(text) if text else "—"


def _fmt_date(d) -> str:
    if not d:
        return "—"
    if hasattr(d, "strftime"):
        return d.strftime("%d.%m.%Y")
    return str(d)


STATUS_COLORS = {
    "В ожидании": ("#FEF3C7", "#B45309"),
    "В работе": ("#DBEAFE", "#1D4ED8"),
    "Исполнено": ("#D1FAE5", "#047857"),
    "Просрочено": ("#FEE2E2", "#B91C1C"),
    "Передано": ("#E2E8F0", "#475569"),
}

STAGE_COLORS = {
    "early": ("#E2E8F0", "#475569"),
    "start": ("#DBEAFE", "#1D4ED8"),
    "critical": ("#FEF3C7", "#B45309"),
    "overdue": ("#FEE2E2", "#B91C1C"),
    "waiting": ("#EDE9FE", "#6D28D9"),
    "done": ("#D1FAE5", "#047857"),
}


def build_correspondence_email_html(
    corr: models.Correspondence,
    *,
    sent_by_name: str,
    attachment_name: Optional[str] = None,
    attachment_names: Optional[list] = None,
    personal_note: Optional[str] = None,
) -> str:
    """Полный HTML письма с контекстом документа."""
    brand = get_env("SMTP_FROM_NAME") or "NeTvoyoDelo"
    base = (get_env("PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/")
    link = f"{base}/api/correspondences/{corr.id}"
    today = date.today()

    status = corr.status.value if corr.status else "—"
    st_bg, st_fg = STATUS_COLORS.get(status, ("#E2E8F0", "#334155"))

    stage = deadline_stage(
        corr.deadline,
        today=today,
        status=corr.status,
        waiting_external=bool(getattr(corr, "waiting_external", False)),
    )
    sg_bg, sg_fg = STAGE_COLORS.get(stage, ("#E2E8F0", "#334155"))
    stage_label = STAGE_LABELS.get(stage, stage)
    hint = stage_hint(
        stage,
        (corr.deadline - today).days if corr.deadline else None,
    )

    executor = corr.executor.full_name if corr.executor else "Не назначен"
    dept = corr.executor.department if corr.executor else ""
    created_by = corr.created_by.full_name if corr.created_by else "—"

    request_type = "—"
    if corr.request_type:
        request_type = corr.request_type.value

    names: list = []
    if attachment_names:
        names.extend([n for n in attachment_names if n])
    elif attachment_name:
        names.append(attachment_name)

    note_block = ""
    if personal_note and personal_note.strip():
        note_block = f"""
        <div style="margin: 0 0 18px; padding: 14px 16px; background: #FFFBEB; border: 1px solid #FDE68A; border-radius: 10px;">
          <div style="font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: #92400E; margin-bottom: 6px;">Комментарий отправителя</div>
          <div style="font-size: 14px; color: #78350F; line-height: 1.5;">{_esc(personal_note)}</div>
        </div>
        """

    attach_block = ""
    if names:
        items = "".join(
            f'<div style="margin:4px 0;">📎 <strong>{_esc(n)}</strong></div>' for n in names
        )
        attach_block = f"""
        <div style="margin: 0 0 18px; padding: 12px 16px; background: #F0F9FF; border: 1px solid #BAE6FD; border-radius: 10px; font-size: 13px; color: #0C4A6E;">
          <div style="font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 6px;">Вложения ({len(names)})</div>
          {items}
        </div>
        """

    waiting_row = ""
    if getattr(corr, "waiting_external", False):
        waiting_note = corr.waiting_external_note or "ожидаем внешний ответ"
        waiting_row = f"""
          <tr>
            <td style="padding: 10px 0; color: #64748B; width: 38%; vertical-align: top;">Ожидание</td>
            <td style="padding: 10px 0; color: #6D28D9; font-weight: 600;">Жду внешнего ответа — {_esc(waiting_note)}</td>
          </tr>
        """

    completion_row = ""
    if corr.completion_note:
        completion_row = f"""
          <tr>
            <td style="padding: 10px 0; color: #64748B; vertical-align: top;">Примечание об исполнении</td>
            <td style="padding: 10px 0; color: #1E293B;">{_esc(corr.completion_note)}</td>
          </tr>
        """

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Письмо №{_esc(corr.incoming_number)}</title>
</head>
<body style="margin:0; padding:0; background:#EEF2F7; font-family:'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#EEF2F7; padding: 28px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="640" cellspacing="0" cellpadding="0" style="max-width:640px; width:100%; background:#FFFFFF; border-radius:14px; overflow:hidden; box-shadow:0 8px 24px rgba(15,23,42,0.08);">
          <tr>
            <td style="background: linear-gradient(135deg, #1A4C8C 0%, #2563A8 100%); padding: 26px 28px; color:#fff;">
              <div style="font-size:12px; letter-spacing:0.08em; text-transform:uppercase; opacity:0.85; margin-bottom:8px;">{_esc(brand)}</div>
              <div style="font-size:22px; font-weight:700; letter-spacing:-0.02em; margin-bottom:6px;">Входящее № {_esc(corr.incoming_number)}</div>
              <div style="font-size:14px; opacity:0.9;">Отправил(а): {_esc(sent_by_name)}</div>
            </td>
          </tr>
          <tr>
            <td style="padding: 22px 28px 8px;">
              <table role="presentation" cellspacing="0" cellpadding="0" style="margin-bottom:16px;">
                <tr>
                  <td style="padding-right:8px;">
                    <span style="display:inline-block; padding:6px 12px; border-radius:999px; background:{st_bg}; color:{st_fg}; font-size:12px; font-weight:700;">{_esc(status)}</span>
                  </td>
                  <td>
                    <span style="display:inline-block; padding:6px 12px; border-radius:999px; background:{sg_bg}; color:{sg_fg}; font-size:12px; font-weight:700;">{_esc(stage_label)}</span>
                  </td>
                </tr>
              </table>
              {note_block}
              {attach_block}
              <div style="font-size:13px; color:#64748B; margin-bottom:18px; line-height:1.45;">{_esc(hint)}</div>

              <div style="font-size:11px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; color:#94A3B8; margin-bottom:10px;">Содержание</div>
              <div style="background:#F8FAFC; border:1px solid #E2E8F0; border-radius:12px; padding:16px 18px; font-size:15px; line-height:1.55; color:#0F172A; margin-bottom:22px;">
                {_esc(corr.content)}
              </div>

              <div style="font-size:11px; font-weight:700; letter-spacing:0.06em; text-transform:uppercase; color:#94A3B8; margin-bottom:6px;">Реквизиты</div>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="font-size:14px; border-collapse:collapse;">
                <tr>
                  <td style="padding:10px 0; color:#64748B; border-bottom:1px solid #F1F5F9;">Отправитель</td>
                  <td style="padding:10px 0; color:#0F172A; font-weight:600; border-bottom:1px solid #F1F5F9;">{_esc(corr.sender)}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; color:#64748B; border-bottom:1px solid #F1F5F9;">Дата документа</td>
                  <td style="padding:10px 0; color:#0F172A; border-bottom:1px solid #F1F5F9;">{_fmt_date(corr.incoming_date)}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; color:#64748B; border-bottom:1px solid #F1F5F9;">Дата поступления</td>
                  <td style="padding:10px 0; color:#0F172A; border-bottom:1px solid #F1F5F9;">{_fmt_date(corr.received_date)}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; color:#64748B; border-bottom:1px solid #F1F5F9;">Срок исполнения</td>
                  <td style="padding:10px 0; color:#0F172A; font-weight:700; border-bottom:1px solid #F1F5F9;">{_fmt_date(corr.deadline)}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; color:#64748B; border-bottom:1px solid #F1F5F9;">Исполнитель</td>
                  <td style="padding:10px 0; color:#0F172A; border-bottom:1px solid #F1F5F9;">{_esc(executor)}{" · " + _esc(dept) if dept else ""}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; color:#64748B; border-bottom:1px solid #F1F5F9;">Автор записи</td>
                  <td style="padding:10px 0; color:#0F172A; border-bottom:1px solid #F1F5F9;">{_esc(created_by)}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; color:#64748B; border-bottom:1px solid #F1F5F9;">Поток</td>
                  <td style="padding:10px 0; color:#0F172A; border-bottom:1px solid #F1F5F9;">{_esc(corr.flow or "—")}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; color:#64748B; border-bottom:1px solid #F1F5F9;">Тип запроса</td>
                  <td style="padding:10px 0; color:#0F172A; border-bottom:1px solid #F1F5F9;">{_esc(request_type)}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; color:#64748B; border-bottom:1px solid #F1F5F9;">Кому / контроль</td>
                  <td style="padding:10px 0; color:#0F172A; border-bottom:1px solid #F1F5F9;">{_esc(corr.to_whom or "—")} / {_esc(corr.control or "—")}</td>
                </tr>
                <tr>
                  <td style="padding:10px 0; color:#64748B;">Отчёт</td>
                  <td style="padding:10px 0; color:#0F172A;">{_esc(corr.report or "—")}{" · " + _fmt_date(corr.report_date) if corr.report_date else ""}</td>
                </tr>
                {waiting_row}
                {completion_row}
              </table>

              <div style="text-align:center; margin: 28px 0 8px;">
                <a href="{link}" style="display:inline-block; padding:12px 22px; background:#1A4C8C; color:#FFFFFF !important; text-decoration:none; border-radius:10px; font-weight:700; font-size:14px;">
                  Открыть в системе
                </a>
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding: 16px 28px 22px; background:#F8FAFC; border-top:1px solid #E2E8F0; text-align:center; font-size:12px; color:#94A3B8;">
              Сообщение из системы учёта входящей корреспонденции {_esc(brand)}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
