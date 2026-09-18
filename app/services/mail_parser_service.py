"""IMAP: пресеты провайдеров, синхронизация и умный поиск по письмам/вложениям."""

from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import re
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import Message
from typing import Any, Optional

from sqlalchemy.orm import Session

from app import models
from app.services.attachment_extract import extract_text_from_bytes
from app.services.mail_crypto import decrypt_secret, encrypt_secret

_FETCH_BATCH = 25
_PARSE_WORKERS = 4


PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "yandex": {
        "label": "Яндекс Почта",
        "host": "imap.yandex.ru",
        "port": 993,
        "use_ssl": True,
        "hint": (
            "1) Включите IMAP в настройках Яндекс Почты. "
            "2) Создайте пароль приложения (тип «Почта»). "
            "3) В поле пароля — только его, без пробелов. "
            "Новый пароль иногда активируется через 1–3 часа."
        ),
    },
    "mailru": {
        "label": "Mail.ru",
        "host": "imap.mail.ru",
        "port": 993,
        "use_ssl": True,
        "hint": "Включите IMAP и создайте пароль для внешнего приложения.",
    },
    "gmail": {
        "label": "Gmail",
        "host": "imap.gmail.com",
        "port": 993,
        "use_ssl": True,
        "hint": "Нужен пароль приложения Google (при включённой 2FA).",
    },
    "custom": {
        "label": "Другой IMAP",
        "host": "",
        "port": 993,
        "use_ssl": True,
        "hint": "Укажите хост и порт IMAP вашего провайдера.",
    },
}

_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024  # 8 MB на файл
_MATCH_THRESHOLD = 0.45
_CLOSE_LIMIT = 8


@dataclass
class SearchHit:
    subject: str
    from_addr: str
    sent_at: Optional[datetime]
    score: float
    match_kind: str  # exact | close
    snippet: str
    attachment_names: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # subject|body|attachment


@dataclass
class SearchResult:
    query: str
    exact: list[SearchHit]
    close: list[SearchHit]
    scanned: int
    message: str


def normalize_text(value: str) -> str:
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(r"[^\w\sа-яa-z0-9/-]+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def decode_mime_header(raw: Optional[str]) -> str:
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    out: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            for enc in (charset, "utf-8", "cp1251", "latin-1"):
                if not enc:
                    continue
                try:
                    out.append(chunk.decode(enc, errors="ignore"))
                    break
                except LookupError:
                    continue
            else:
                out.append(chunk.decode("utf-8", errors="ignore"))
        else:
            out.append(str(chunk))
    return " ".join(out).strip()


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</p>", "\n", html)
    html = re.sub(r"(?is)<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"&amp;", "&", html)
    html = re.sub(r"&lt;", "<", html)
    html = re.sub(r"&gt;", ">", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def extract_body_text(msg: Message) -> str:
    texts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = (part.get_content_type() or "").lower()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                raw = payload.decode(charset, errors="ignore")
            except LookupError:
                raw = payload.decode("utf-8", errors="ignore")
            if ctype == "text/html":
                texts.append(_html_to_text(raw))
            else:
                texts.append(raw)
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            raw = payload.decode(charset, errors="ignore")
        except LookupError:
            raw = payload.decode("utf-8", errors="ignore")
        if (msg.get_content_type() or "").lower() == "text/html":
            texts.append(_html_to_text(raw))
        else:
            texts.append(raw)
    return "\n".join(t.strip() for t in texts if t and t.strip())


def extract_attachments(msg: Message) -> tuple[str, list[str]]:
    names: list[str] = []
    texts: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = decode_mime_header(part.get_filename() or "")
        disp = str(part.get("Content-Disposition") or "").lower()
        ctype = part.get_content_type() or ""
        is_attach = "attachment" in disp or bool(filename)
        if not is_attach:
            continue
        if not filename:
            filename = f"attachment.{ctype.split('/')[-1] or 'bin'}"
        names.append(filename)
        payload = part.get_payload(decode=True) or b""
        if len(payload) > _MAX_ATTACHMENT_BYTES:
            continue
        extracted = extract_text_from_bytes(filename, payload, ctype)
        if extracted:
            texts.append(f"[{filename}]\n{extracted}")
    return "\n\n".join(texts), names


def score_message(query: str, subject: str, body: str, attachments: str, names: list[str]) -> tuple[float, list[str], str]:
    q_norm = normalize_text(query)
    if not q_norm:
        return 0.0, [], ""

    tokens = [t for t in q_norm.split() if len(t) > 1]
    if not tokens:
        tokens = q_norm.split()

    fields = {
        "subject": normalize_text(subject),
        "body": normalize_text(body),
        "attachment": normalize_text(attachments + " " + " ".join(names)),
    }

    sources: list[str] = []
    best_snippet = ""
    score = 0.0

    # Полная фраза
    for src, text in fields.items():
        if q_norm and q_norm in text:
            score = max(score, 1.0 if src == "subject" else 0.95)
            sources.append(src)
            best_snippet = _snippet_from(text, q_norm)
            break

    if score < 0.9:
        weights = {"subject": 0.45, "body": 0.35, "attachment": 0.35}
        partial = 0.0
        for src, text in fields.items():
            if not text or not tokens:
                continue
            hits = sum(1 for t in tokens if t in text)
            ratio = hits / len(tokens)
            if ratio > 0:
                partial += weights[src] * ratio
                if ratio >= 0.5:
                    sources.append(src)
                if not best_snippet and hits:
                    best_snippet = _snippet_from(text, tokens[0])
        score = max(score, min(partial, 0.89))

    sources = list(dict.fromkeys(sources))
    return score, sources, best_snippet


def _snippet_from(norm_text: str, needle: str, radius: int = 90) -> str:
    idx = norm_text.find(needle)
    if idx < 0:
        return norm_text[: radius * 2]
    start = max(0, idx - radius)
    end = min(len(norm_text), idx + len(needle) + radius)
    chunk = norm_text[start:end].strip()
    if start > 0:
        chunk = "…" + chunk
    if end < len(norm_text):
        chunk = chunk + "…"
    return chunk


def resolve_connection_settings(
    provider: str,
    email_addr: str,
    password: str,
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
    use_ssl: bool = True,
) -> dict[str, Any]:
    preset = PROVIDER_PRESETS.get(provider) or PROVIDER_PRESETS["custom"]
    host = (imap_host or preset.get("host") or "").strip()
    port = int(imap_port or preset.get("port") or 993)
    ssl_flag = bool(use_ssl if use_ssl is not None else preset.get("use_ssl", True))
    if not host:
        raise ValueError("Укажите IMAP-хост")
    if not email_addr or not password:
        raise ValueError("Укажите email и пароль приложения")
    return {
        "provider": provider if provider in PROVIDER_PRESETS else "custom",
        "email": email_addr.strip(),
        "password": password,
        "imap_host": host,
        "imap_port": port,
        "use_ssl": ssl_flag,
    }


def connect_imap(host: str, port: int, use_ssl: bool, email_addr: str, password: str) -> imaplib.IMAP4:
    # Пароли приложений иногда копируют с пробелами/переносами
    password = (password or "").replace(" ", "").replace("\u00a0", "").strip()
    email_addr = (email_addr or "").strip()
    if use_ssl:
        ctx = ssl.create_default_context()
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
    else:
        client = imaplib.IMAP4(host, port)

    usernames = _login_candidates(email_addr, host)
    last_error: Optional[Exception] = None
    for username in usernames:
        try:
            client.login(username, password)
            return client
        except imaplib.IMAP4.error as exc:
            last_error = exc
            continue

    try:
        client.logout()
    except Exception:
        pass
    raise last_error or imaplib.IMAP4.error(b"LOGIN failed")


def _login_candidates(email_addr: str, host: str) -> list[str]:
    """Яндекс для @yandex.* часто ждёт логин без домена; для бизнес-почты — полный адрес."""
    email_addr = email_addr.strip()
    candidates = [email_addr]
    if "@" in email_addr:
        local, _, domain = email_addr.partition("@")
        domain_l = domain.lower()
        host_l = (host or "").lower()
        is_yandex = (
            "yandex" in host_l
            or "ya.ru" in host_l
            or domain_l.startswith("yandex.")
            or domain_l in ("ya.ru", "yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "yandex.ua")
        )
        if is_yandex and local:
            # Сначала локальная часть — так рекомендует Яндекс для личных ящиков
            candidates = [local, email_addr]
    # уникальные, порядок сохраняем
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def format_imap_auth_error(exc: BaseException, provider: str = "") -> str:
    raw = str(exc)
    lower = raw.lower()
    if "authenticationfailed" in lower or "invalid credentials" in lower or "login" in lower:
        lines = [
            "Яндекс/IMAP отклонил вход: неверный логин/пароль или IMAP выключен.",
            "",
            "Проверьте по шагам:",
            "1) Почта → Настройки → Почтовые программы → включите «С сервера imap.yandex.ru по протоколу IMAP»",
            "   и «Пароли приложений и OAuth-токены» → Сохранить.",
            "2) Яндекс ID → Безопасность → Пароли приложений → создайте пароль типа «Почта».",
            "3) В форме укажите этот пароль приложения (не обычный пароль от Яндекса).",
            "4) Новый пароль приложения иногда активируется через 1–3 часа.",
            "5) Хост: imap.yandex.ru, порт 993, SSL = Да.",
        ]
        if provider and provider != "yandex":
            lines = [
                "IMAP отклонил вход: неверный логин/пароль или доступ по IMAP выключен у провайдера.",
                "Для Gmail/Mail.ru нужен пароль приложения и включённый IMAP в настройках ящика.",
            ]
        return "\n".join(lines)
    return f"Не удалось подключиться: {raw}"


def test_imap_connection(**kwargs) -> str:
    settings = resolve_connection_settings(**kwargs)
    try:
        client = connect_imap(
            settings["imap_host"],
            settings["imap_port"],
            settings["use_ssl"],
            settings["email"],
            settings["password"],
        )
    except Exception as exc:
        raise RuntimeError(format_imap_auth_error(exc, settings.get("provider", ""))) from exc
    try:
        typ, data = client.list()
        if typ != "OK":
            return "Подключение есть, но список папок недоступен"
        folders = 0
        if data:
            folders = len([x for x in data if x])
        return f"OK: вход выполнен ({folders} папок)"
    finally:
        try:
            client.logout()
        except Exception:
            pass


def upsert_mailbox_account(
    db: Session,
    user: models.User,
    *,
    provider: str,
    email_addr: str,
    password: Optional[str],
    imap_host: Optional[str],
    imap_port: Optional[int],
    use_ssl: bool,
    folder: str = "INBOX",
) -> models.MailboxAccount:
    account = (
        db.query(models.MailboxAccount)
        .filter(models.MailboxAccount.user_id == user.id)
        .first()
    )
    preset = PROVIDER_PRESETS.get(provider) or PROVIDER_PRESETS["custom"]
    host = (imap_host or (account.imap_host if account else None) or preset.get("host") or "").strip()
    port = int(imap_port or (account.imap_port if account else None) or preset.get("port") or 993)

    if account is None:
        if not password:
            raise ValueError("Укажите пароль приложения")
        account = models.MailboxAccount(
            user_id=user.id,
            provider=provider,
            email=email_addr.strip(),
            imap_host=host,
            imap_port=port,
            use_ssl=use_ssl,
            password_encrypted=encrypt_secret(password),
            folder=(folder or "INBOX").strip() or "INBOX",
        )
        db.add(account)
    else:
        account.provider = provider
        account.email = email_addr.strip()
        account.imap_host = host
        account.imap_port = port
        account.use_ssl = use_ssl
        account.folder = (folder or account.folder or "INBOX").strip() or "INBOX"
        if password:
            account.password_encrypted = encrypt_secret(password)
        account.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(account)
    return account


def get_account_for_user(db: Session, user_id: int) -> Optional[models.MailboxAccount]:
    return (
        db.query(models.MailboxAccount)
        .filter(models.MailboxAccount.user_id == user_id)
        .first()
    )


def _parse_email_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is None:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _imap_since_date(days: int) -> str:
    since = datetime.utcnow() - timedelta(days=max(1, days))
    # IMAP требует английские сокращения месяцев (не зависит от locale ОС)
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    return f"{since.day:02d}-{months[since.month - 1]}-{since.year}"


def sync_and_search(
    db: Session,
    account: models.MailboxAccount,
    query: str,
    *,
    days: int = 90,
    max_messages: int = 150,
) -> SearchResult:
    """
    Быстрый поиск без потери качества:
    1) UID-список с сервера (SINCE)
    2) уже распарсенные письма — из локального кэша
    3) недостающие — пакетный FETCH + параллельный разбор вложений
    """
    password = decrypt_secret(account.password_encrypted)
    try:
        client = connect_imap(
            account.imap_host,
            account.imap_port,
            bool(account.use_ssl),
            account.email,
            password,
        )
    except Exception as exc:
        raise RuntimeError(format_imap_auth_error(exc, account.provider or "")) from exc
    folder = account.folder or "INBOX"
    scored: list[tuple[float, SearchHit]] = []
    scanned = 0
    from_cache = 0
    fetched_new = 0

    try:
        typ, _ = client.select(f'"{folder}"' if " " in folder else folder, readonly=True)
        if typ != "OK":
            typ, _ = client.select("INBOX", readonly=True)
            if typ != "OK":
                raise RuntimeError("Не удалось открыть папку IMAP")

        since = _imap_since_date(days)
        typ, data = client.uid("search", None, f"(SINCE {since})")
        if typ != "OK" or not data or not data[0]:
            return SearchResult(
                query=query,
                exact=[],
                close=[],
                scanned=0,
                message="В указанном периоде писем не найдено.",
            )

        uids = data[0].split()
        uids = uids[-max_messages:]
        uid_strs = [
            (u.decode() if isinstance(u, bytes) else str(u)) for u in uids
        ]
        # Свежие сверху для UX при частичном таймауте — порядок не критичен для качества

        cached_rows = (
            db.query(models.MailMessageCache)
            .filter(
                models.MailMessageCache.account_id == account.id,
                models.MailMessageCache.folder == folder,
                models.MailMessageCache.message_uid.in_(uid_strs),
            )
            .all()
        )
        cache_by_uid = {r.message_uid: r for r in cached_rows}

        missing = [u for u in uid_strs if u not in cache_by_uid]

        # --- кэш: сразу в скоринг ---
        for uid_s in uid_strs:
            row = cache_by_uid.get(uid_s)
            if row is None:
                continue
            att_names = [n for n in (row.attachment_names or "").split(";") if n]
            score, sources, snippet = score_message(
                query,
                row.subject or "",
                row.body_text or "",
                row.attachments_text or "",
                att_names,
            )
            scanned += 1
            from_cache += 1
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    SearchHit(
                        subject=row.subject or "(без темы)",
                        from_addr=row.from_addr or "",
                        sent_at=row.sent_at,
                        score=score,
                        match_kind="exact" if score >= _MATCH_THRESHOLD else "close",
                        snippet=snippet,
                        attachment_names=att_names,
                        sources=sources,
                    ),
                )
            )

        # --- новые: пакетный FETCH + параллельный parse ---
        parsed_new: list[dict[str, Any]] = []
        for batch in _chunks(missing, _FETCH_BATCH):
            typ, fetched = client.uid(
                "fetch",
                ",".join(batch),
                "(UID BODY.PEEK[])",
            )
            if typ != "OK" or not fetched:
                # fallback по одному (редкие серверы)
                for uid_s in batch:
                    typ1, one = client.uid("fetch", uid_s, "(BODY.PEEK[])")
                    if typ1 != "OK" or not one:
                        continue
                    bodies = _iter_fetch_bodies(one, fallback_uid=uid_s)
                    parsed_new.extend(_parse_bodies_parallel(bodies))
                continue
            bodies = _iter_fetch_bodies(fetched)
            # если UID не распарсился — сопоставить по порядку batch
            fixed: list[tuple[str, bytes]] = []
            for idx, (uid_s, raw) in enumerate(bodies):
                if not uid_s and idx < len(batch):
                    uid_s = batch[idx]
                if uid_s and raw:
                    fixed.append((uid_s, raw))
            parsed_new.extend(_parse_bodies_parallel(fixed))

        for item in parsed_new:
            _upsert_cache(db, account, folder=folder, **item)
            fetched_new += 1
            score, sources, snippet = score_message(
                query,
                item["subject"],
                item["body_text"],
                item["attachments_text"],
                item["_att_names"],
            )
            scanned += 1
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    SearchHit(
                        subject=item["subject"] or "(без темы)",
                        from_addr=item["from_addr"] or "",
                        sent_at=item["sent_at"],
                        score=score,
                        match_kind="exact" if score >= _MATCH_THRESHOLD else "close",
                        snippet=snippet,
                        attachment_names=item["_att_names"],
                        sources=sources,
                    ),
                )
            )

        account.last_checked_at = datetime.utcnow()
        db.commit()
    finally:
        try:
            client.logout()
        except Exception:
            pass

    scored.sort(key=lambda x: x[0], reverse=True)
    exact = [h for s, h in scored if s >= _MATCH_THRESHOLD]
    for h in exact:
        h.match_kind = "exact"
    close = [h for s, h in scored if s < _MATCH_THRESHOLD][:_CLOSE_LIMIT]
    for h in close:
        h.match_kind = "close"

    speed_note = f"кэш {from_cache}, новых {fetched_new}"
    if exact:
        msg = f"Найдено подходящих писем: {len(exact)} (просмотрено {scanned}; {speed_note})."
    elif close:
        msg = (
            f"Точного совпадения в ящике не найдено. "
            f"Показаны ближайшие варианты ({len(close)} из {scanned}; {speed_note})."
        )
    else:
        msg = f"Ничего похожего не найдено среди {scanned} писем ({speed_note})."

    return SearchResult(query=query, exact=exact, close=close, scanned=scanned, message=msg)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _iter_fetch_bodies(
    data: list,
    fallback_uid: str = "",
) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    if not data:
        return out
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        meta, payload = item[0], item[1]
        if not isinstance(payload, (bytes, bytearray)):
            continue
        meta_s = meta.decode("utf-8", errors="ignore") if isinstance(meta, bytes) else str(meta)
        m = re.search(r"\bUID\s+(\d+)\b", meta_s, re.I)
        uid_s = m.group(1) if m else fallback_uid
        out.append((uid_s, bytes(payload)))
    return out


def _parse_one_message(uid_s: str, raw: bytes) -> Optional[dict[str, Any]]:
    try:
        msg = email.message_from_bytes(raw)
        subject = decode_mime_header(msg.get("Subject"))
        from_addr = decode_mime_header(msg.get("From"))
        sent_at = _parse_email_date(msg.get("Date"))
        body = extract_body_text(msg)
        att_text, att_names = extract_attachments(msg)
        message_id = decode_mime_header(msg.get("Message-ID"))
        return {
            "message_uid": uid_s,
            "message_id_header": message_id,
            "subject": subject,
            "from_addr": from_addr,
            "sent_at": sent_at,
            "body_text": body,
            "attachments_text": att_text,
            "attachment_names": ";".join(att_names),
            "_att_names": att_names,
        }
    except Exception:
        return None


def _parse_bodies_parallel(bodies: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    if not bodies:
        return []
    if len(bodies) == 1:
        one = _parse_one_message(bodies[0][0], bodies[0][1])
        return [one] if one else []

    parsed: list[dict[str, Any]] = []
    workers = min(_PARSE_WORKERS, len(bodies))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_parse_one_message, uid_s, raw): uid_s for uid_s, raw in bodies
        }
        for fut in as_completed(futures):
            item = fut.result()
            if item:
                parsed.append(item)
    return parsed


def _upsert_cache(
    db: Session,
    account: models.MailboxAccount,
    **fields,
) -> None:
    # внутренние поля для скоринга не пишем в БД
    fields = {k: v for k, v in fields.items() if not k.startswith("_")}
    row = (
        db.query(models.MailMessageCache)
        .filter(
            models.MailMessageCache.account_id == account.id,
            models.MailMessageCache.folder == fields["folder"],
            models.MailMessageCache.message_uid == fields["message_uid"],
        )
        .first()
    )
    if row is None:
        row = models.MailMessageCache(account_id=account.id, **fields)
        db.add(row)
    else:
        for k, v in fields.items():
            setattr(row, k, v)
        row.indexed_at = datetime.utcnow()
    db.flush()
