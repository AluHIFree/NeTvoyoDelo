"""

Генерация отчёта о проделанной работе через Cloudflare Workers AI

(OpenAI-compatible /chat/completions).

"""



from __future__ import annotations



from datetime import date, datetime

from typing import List, Optional



import httpx

from sqlalchemy.orm import Session



from app import models

from app.config_env import get_env, is_cloudflare_configured





DEFAULT_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"





def _api_key() -> str:

    return get_env("CLOUDFLARE_API_KEY")





def _account_id() -> str:

    return get_env("CLOUDFLARE_ACCOUNT_ID")





def _model() -> str:

    return get_env("CLOUDFLARE_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL





def _chat_completions_url() -> str:

    account = _account_id()

    return (

        f"https://api.cloudflare.com/client/v4/accounts/{account}"

        f"/ai/v1/chat/completions"

    )





MONTH_NAMES_RU = {

    1: "январь",

    2: "февраль",

    3: "март",

    4: "апрель",

    5: "май",

    6: "июнь",

    7: "июль",

    8: "август",

    9: "сентябрь",

    10: "октябрь",

    11: "ноябрь",

    12: "декабрь",

}



MONTH_NAMES_GENITIVE = {

    1: "января",

    2: "февраля",

    3: "марта",

    4: "апреля",

    5: "мая",

    6: "июня",

    7: "июля",

    8: "августа",

    9: "сентября",

    10: "октября",

    11: "ноября",

    12: "декабря",

}



ROLE_LABELS = {

    "executor": "исполнитель",

    "controller": "контролёр",

    "viewer": "наблюдатель",

}





def period_title(date_from: date, date_to: date) -> str:

    """Заголовок периода: «май 2026 года» или «01.05.2026 — 31.05.2026»."""

    if date_from.year == date_to.year and date_from.month == date_to.month:

        if date_from.day == 1:

            return f"{MONTH_NAMES_RU[date_from.month]} {date_from.year} года"

        return (

            f"{date_from.day}–{date_to.day} "

            f"{MONTH_NAMES_GENITIVE[date_from.month]} {date_from.year} года"

        )

    return (

        f"{date_from.strftime('%d.%m.%Y')} — {date_to.strftime('%d.%m.%Y')}"

    )





def _completion_date(c: models.Correspondence) -> Optional[date]:

    if c.completed_at:

        return c.completed_at.date() if hasattr(c.completed_at, "date") else c.completed_at

    if c.updated_at and c.status == models.CorrespondenceStatus.COMPLETED:

        return c.updated_at.date()

    return None





def get_completed_letters_for_period(

    db: Session,

    user_id: int,

    date_from: date,

    date_to: date,

) -> List[models.Correspondence]:

    """Закрытые письма пользователя за период (по дате исполнения / обновления)."""

    letters = (

        db.query(models.Correspondence)

        .filter(

            models.Correspondence.executor_id == user_id,

            models.Correspondence.status == models.CorrespondenceStatus.COMPLETED,

        )

        .order_by(models.Correspondence.completed_at.asc(), models.Correspondence.incoming_date.asc())

        .all()

    )

    result = []

    for c in letters:

        done = _completion_date(c)

        if done and date_from <= done <= date_to:

            result.append(c)

    return result





def letters_to_context(letters: List[models.Correspondence]) -> str:

    if not letters:

        return "За указанный период закрытых писем нет."

    lines = []

    for i, c in enumerate(letters, 1):

        done = _completion_date(c)

        content = (c.content or "").strip().replace("\n", " ")

        if len(content) > 400:

            content = content[:400] + "…"

        report = (c.report or "").strip().replace("\n", " ")

        if len(report) > 500:

            report = report[:500] + "…"

        note = (c.completion_note or "").strip().replace("\n", " ")

        lines.append(

            f"{i}. № {c.incoming_number} | дата док.: "

            f"{c.incoming_date.strftime('%d.%m.%Y') if c.incoming_date else '—'} | "

            f"поступило: {c.received_date.strftime('%d.%m.%Y') if c.received_date else '—'} | "

            f"исполнено: {done.strftime('%d.%m.%Y') if done else '—'} | "

            f"срок: {c.deadline.strftime('%d.%m.%Y') if c.deadline else '—'}\n"

            f"   Отправитель: {c.sender or '—'}\n"

            f"   Содержание: {content or '—'}\n"

            f"   Примечание об исполнении: {note or '—'}\n"

            f"   Отчёт/результат: {report or '—'}"

        )

    return "\n\n".join(lines)





def build_system_prompt() -> str:

    return (

        "Ты — помощник государственного служащего для составления официального "

        "отчёта о проделанной работе. Пиши на русском языке, деловым стилем, "

        "в духе внутренней служебной отчётности органа исполнительной власти.\n\n"

        "Структура отчёта строго такая:\n"

        "ОТЧЕТ\n"

        "о проделанной работе за <период>\n"

        "(ФИО – должность / отдел)\n\n"

        "Далее — модули по хронологии или тематике (Модуль 1, Модуль 2, …).\n"

        "Внутри модулей — нумерованные пункты с кратким заголовком темы и "

        "описанием сделанного. Подпункты оформляй через «—».\n\n"

        "Правила:\n"

        "1) Опирайся ТОЛЬКО на переданные закрытые письма и их содержание/результаты.\n"

        "2) Группируй схожие письма в тематические блоки, не перечисляй каждое "

        "письмо отдельным сухим пунктом «письмо №…», а обобщай содержательно.\n"

        "3) Не выдумывай факты, которых нет во входных данных (ВКС, платформы, "

        "приказы и т.п. — только если это следует из текстов писем/отчётов).\n"

        "4) Если писем мало — сделай один модуль; если много — 2–3 модуля по периодам "

        "или темам.\n"

        "5) Не добавляй вступлений вроде «Конечно» и не используй markdown-разметку "

        "(без ###, **, ```).\n"

        "6) Итог — готовый текст отчёта, который можно скопировать в Word."

    )





def build_user_prompt(

    user: models.User,

    date_from: date,

    date_to: date,

    letters: List[models.Correspondence],

    position: Optional[str] = None,

    extra_notes: Optional[str] = None,

) -> str:

    role = ROLE_LABELS.get(getattr(user, "role", None) or "", "сотрудник")

    pos = (position or "").strip()

    if not pos:

        pos = f"{role} {user.department}"

    title = period_title(date_from, date_to)

    parts = [

        f"Составь отчёт о проделанной работе за {title}.",

        f"Сотрудник: {user.full_name}",

        f"Должность/подпись: {pos}",

        f"Отдел: {user.department}",

        f"Период: {date_from.strftime('%d.%m.%Y')} — {date_to.strftime('%d.%m.%Y')}",

        f"Количество закрытых писем: {len(letters)}",

        "",

        "Данные по закрытым письмам:",

        letters_to_context(letters),

    ]

    if extra_notes and extra_notes.strip():

        parts.extend([

            "",

            "Дополнительные указания сотрудника (учти, если не противоречат письмам):",

            extra_notes.strip(),

        ])

    return "\n".join(parts)





def _extract_content(data: dict) -> str:

    """Достать текст ответа из OpenAI-compatible или нативного формата CF."""

    choices = data.get("choices") or []

    if choices:

        msg = choices[0].get("message") or {}

        content = msg.get("content")

        if isinstance(content, str) and content.strip():

            return content.strip()

        # иногда content — список частей

        if isinstance(content, list):

            parts = []

            for part in content:

                if isinstance(part, str):

                    parts.append(part)

                elif isinstance(part, dict) and part.get("text"):

                    parts.append(str(part["text"]))

            joined = "".join(parts).strip()

            if joined:

                return joined

        # legacy: choices[0].text

        text = choices[0].get("text")

        if isinstance(text, str) and text.strip():

            return text.strip()



    result = data.get("result") or {}

    if isinstance(result, dict):

        for key in ("response", "text", "output"):

            val = result.get(key)

            if isinstance(val, str) and val.strip():

                return val.strip()

    return ""





async def generate_work_report(

    db: Session,

    user: models.User,

    date_from: date,

    date_to: date,

    position: Optional[str] = None,

    extra_notes: Optional[str] = None,

) -> dict:

    """

    Возвращает dict: report, letters_count, period_title, error (optional).

    """

    if not is_cloudflare_configured():

        return {

            "ok": False,

            "error": (

                "Cloudflare AI не настроен. Укажите CLOUDFLARE_API_KEY и "

                "CLOUDFLARE_ACCOUNT_ID в .env."

            ),

            "report": "",

            "letters_count": 0,

            "period_title": period_title(date_from, date_to),

        }



    if date_from > date_to:

        return {

            "ok": False,

            "error": "Дата начала периода не может быть позже даты окончания.",

            "report": "",

            "letters_count": 0,

            "period_title": period_title(date_from, date_to),

        }



    letters = get_completed_letters_for_period(db, user.id, date_from, date_to)

    ptitle = period_title(date_from, date_to)



    if not letters:

        return {

            "ok": False,

            "error": (

                f"За период {date_from.strftime('%d.%m.%Y')} — "

                f"{date_to.strftime('%d.%m.%Y')} нет закрытых вами писем. "

                "Отчёт можно сформировать только по исполненным документам."

            ),

            "report": "",

            "letters_count": 0,

            "period_title": ptitle,

        }



    payload = {

        "model": _model(),

        "temperature": 0.4,

        "max_tokens": 4096,

        "messages": [

            {"role": "system", "content": build_system_prompt()},

            {

                "role": "user",

                "content": build_user_prompt(

                    user, date_from, date_to, letters, position, extra_notes

                ),

            },

        ],

    }



    try:

        async with httpx.AsyncClient(timeout=120.0) as client:

            resp = await client.post(

                _chat_completions_url(),

                headers={

                    "Authorization": f"Bearer {_api_key()}",

                    "Content-Type": "application/json",

                },

                json=payload,

            )

        if resp.status_code >= 400:

            detail = resp.text[:500]

            return {

                "ok": False,

                "error": f"Ошибка Cloudflare AI ({resp.status_code}): {detail}",

                "report": "",

                "letters_count": len(letters),

                "period_title": ptitle,

            }

        data = resp.json()

        content = _extract_content(data if isinstance(data, dict) else {})

        if not content:

            return {

                "ok": False,

                "error": "Модель вернула пустой ответ. Попробуйте ещё раз.",

                "report": "",

                "letters_count": len(letters),

                "period_title": ptitle,

            }

        return {

            "ok": True,

            "error": None,

            "report": content,

            "letters_count": len(letters),

            "period_title": ptitle,

            "generated_at": datetime.utcnow().isoformat() + "Z",

            "provider": "cloudflare",

            "model": _model(),

        }

    except httpx.TimeoutException:

        return {

            "ok": False,

            "error": "Таймаут запроса к Cloudflare AI. Попробуйте позже.",

            "report": "",

            "letters_count": len(letters),

            "period_title": ptitle,

        }

    except Exception as e:

        return {

            "ok": False,

            "error": f"Не удалось сформировать отчёт: {e}",

            "report": "",

            "letters_count": len(letters),

            "period_title": ptitle,

        }


