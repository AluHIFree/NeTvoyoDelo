"""
Проверка и установка обновлений с GitHub.

База данных, .env и пользовательские файлы (вложения, аватары)
никогда не перезаписываются.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from app.config_env import PROJECT_ROOT, get_env
from app.version import __version__ as APP_VERSION

META_FILE = PROJECT_ROOT / ".update_meta.json"

# Пути относительно корня установки, которые НИКОГДА не трогаем
PRESERVE_EXACT = {
    "correspondences.db",
    "correspondences.db-journal",
    "correspondences.db-wal",
    "correspondences.db-shm",
    ".env",
    ".update_meta.json",
}

PRESERVE_PREFIXES = (
    "app/static/uploads/",
    "app/static/avatars/",
    "python/",
    "venv/",
    ".venv/",
    ".git/",
    "release/",
    "__pycache__/",
)

# Что копируем из архива GitHub в установку
UPDATE_TOP_FILES = (
    "VERSION",
    "CHANGELOG.md",
    "requirements.txt",
    "run.py",
    "run_local.py",
    "LICENSE",
    "README.md",
    ".env.example",
    ".gitignore",
)

UPDATE_DIRS = (
    "app",
    "portable",
    "scripts",
)


@dataclass
class UpdateInfo:
    update_available: bool
    local_version: str
    remote_version: str
    remote_sha: str
    release_name: str
    release_url: str
    published_at: str
    changelog: str
    source: str  # "release" | "commit" | "none"
    error: Optional[str] = None
    commits: list[dict[str, str]] = field(default_factory=list)


def _repo_slug() -> str:
    return get_env("GITHUB_REPO", "AluHIFree/NeTvoyoDelo") or "AluHIFree/NeTvoyoDelo"


def _branch() -> str:
    return get_env("GITHUB_UPDATE_BRANCH", "main") or "main"


def _api_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"NeTvoyoDelo-Updater/{APP_VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = get_env("GITHUB_TOKEN") or get_env("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def load_meta() -> dict[str, Any]:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "version": APP_VERSION,
        "commit_sha": "",
        "updated_at": None,
        "last_check_at": None,
    }


def save_meta(data: dict[str, Any]) -> None:
    META_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def local_version() -> str:
    version_file = PROJECT_ROOT / "VERSION"
    if version_file.exists():
        text = version_file.read_text(encoding="utf-8").strip()
        if text:
            return text.splitlines()[0].strip()
    meta = load_meta()
    return str(meta.get("version") or APP_VERSION)


def _parse_version(value: str) -> tuple[int, ...]:
    cleaned = value.strip().lstrip("vV")
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def version_gt(remote: str, local: str) -> bool:
    try:
        return _parse_version(remote) > _parse_version(local)
    except Exception:
        return remote.strip().lstrip("vV") != local.strip().lstrip("vV")


def _should_preserve(rel_posix: str) -> bool:
    name = rel_posix.replace("\\", "/")
    base = Path(name).name
    if base in PRESERVE_EXACT or name in PRESERVE_EXACT:
        return True
    for prefix in PRESERVE_PREFIXES:
        if name == prefix.rstrip("/") or name.startswith(prefix):
            return True
    if "/__pycache__/" in f"/{name}/" or name.endswith(".pyc"):
        return True
    return False


async def check_for_updates() -> UpdateInfo:
    local_ver = local_version()
    meta = load_meta()
    local_sha = str(meta.get("commit_sha") or "")
    repo = _repo_slug()
    branch = _branch()
    headers = _api_headers()

    try:
        async with httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True) as client:
            # 1) Releases
            rel_resp = await client.get(f"https://api.github.com/repos/{repo}/releases/latest")
            if rel_resp.status_code == 200:
                rel = rel_resp.json()
                tag = str(rel.get("tag_name") or "")
                body = str(rel.get("body") or "").strip()
                name = str(rel.get("name") or tag)
                html_url = str(rel.get("html_url") or "")
                published = str(rel.get("published_at") or "")
                # target commitish / resolve tag sha
                sha = ""
                tag_ref = await client.get(f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}")
                if tag_ref.status_code == 200:
                    obj = tag_ref.json().get("object") or {}
                    sha = str(obj.get("sha") or "")
                    if obj.get("type") == "tag":
                        tag_obj = await client.get(obj.get("url"))
                        if tag_obj.status_code == 200:
                            sha = str((tag_obj.json().get("object") or {}).get("sha") or sha)

                remote_ver = tag.lstrip("v") if tag else local_ver
                available = version_gt(tag, local_ver)

                changelog = body
                if not changelog:
                    changelog = f"Релиз {tag}: подробности на GitHub."

                meta["last_check_at"] = datetime.now(timezone.utc).isoformat()
                save_meta(meta)

                return UpdateInfo(
                    update_available=available,
                    local_version=local_ver,
                    remote_version=remote_ver,
                    remote_sha=sha,
                    release_name=name,
                    release_url=html_url,
                    published_at=published,
                    changelog=changelog,
                    source="release",
                )

            # 2) Fallback: latest commit on branch + compare
            branch_resp = await client.get(f"https://api.github.com/repos/{repo}/commits/{branch}")
            if branch_resp.status_code != 200:
                err = f"GitHub API: HTTP {branch_resp.status_code}"
                if branch_resp.status_code == 403:
                    err += " (лимит запросов или нужен GITHUB_TOKEN)"
                elif branch_resp.status_code == 404:
                    err += f" (репозиторий {repo} не найден или приватный)"
                return UpdateInfo(
                    update_available=False,
                    local_version=local_ver,
                    remote_version=local_ver,
                    remote_sha="",
                    release_name="",
                    release_url=f"https://github.com/{repo}",
                    published_at="",
                    changelog="",
                    source="none",
                    error=err,
                )

            head = branch_resp.json()
            remote_sha = str(head.get("sha") or "")
            commit = head.get("commit") or {}
            message = str((commit.get("message") or "")).strip()
            date = str(((commit.get("author") or {}).get("date")) or "")

            commits: list[dict[str, str]] = []
            available = False
            changelog = ""

            if local_sha and remote_sha and local_sha[:40] != remote_sha[:40]:
                cmp = await client.get(
                    f"https://api.github.com/repos/{repo}/compare/{local_sha[:40]}...{remote_sha[:40]}"
                )
                if cmp.status_code == 200:
                    data = cmp.json()
                    ahead = int(data.get("ahead_by") or 0)
                    available = ahead > 0
                    for c in (data.get("commits") or [])[-15:]:
                        msg = str(((c.get("commit") or {}).get("message") or "")).split("\n")[0]
                        commits.append(
                            {
                                "sha": str(c.get("sha") or "")[:7],
                                "message": msg,
                            }
                        )
                    if commits:
                        changelog = "\n".join(f"- `{c['sha']}` {c['message']}" for c in reversed(commits))
                else:
                    available = True
                    changelog = message
            elif not local_sha:
                # Первая проверка: считаем, что на ветке может быть новее —
                # сравниваем VERSION из репозитория
                raw = await client.get(
                    f"https://raw.githubusercontent.com/{repo}/{branch}/VERSION"
                )
                remote_ver = local_ver
                if raw.status_code == 200:
                    remote_ver = raw.text.strip().splitlines()[0].strip()
                    available = version_gt(remote_ver, local_ver)
                else:
                    # Нет локального sha — предлагаем синхронизацию с описанием последнего коммита
                    available = False
                    remote_ver = local_ver
                changelog = message
                meta["last_check_at"] = datetime.now(timezone.utc).isoformat()
                save_meta(meta)
                return UpdateInfo(
                    update_available=available,
                    local_version=local_ver,
                    remote_version=remote_ver,
                    remote_sha=remote_sha,
                    release_name=f"Ветка {branch}",
                    release_url=f"https://github.com/{repo}/commits/{branch}",
                    published_at=date,
                    changelog=changelog or "Нет описания.",
                    source="commit",
                    commits=commits,
                )
            else:
                changelog = "Установлена актуальная версия."

            meta["last_check_at"] = datetime.now(timezone.utc).isoformat()
            save_meta(meta)

            return UpdateInfo(
                update_available=available,
                local_version=local_ver,
                remote_version=local_ver if not available else (message.split("\n")[0][:40] or remote_sha[:7]),
                remote_sha=remote_sha,
                release_name=f"Ветка {branch}",
                release_url=f"https://github.com/{repo}/tree/{remote_sha[:7]}" if remote_sha else f"https://github.com/{repo}",
                published_at=date,
                changelog=changelog or message,
                source="commit",
                commits=commits,
            )
    except httpx.HTTPError as exc:
        return UpdateInfo(
            update_available=False,
            local_version=local_ver,
            remote_version=local_ver,
            remote_sha="",
            release_name="",
            release_url=f"https://github.com/{_repo_slug()}",
            published_at="",
            changelog="",
            source="none",
            error=f"Сеть: {exc}",
        )
    except Exception as exc:
        return UpdateInfo(
            update_available=False,
            local_version=local_ver,
            remote_version=local_ver,
            remote_sha="",
            release_name="",
            release_url=f"https://github.com/{_repo_slug()}",
            published_at="",
            changelog="",
            source="none",
            error=str(exc),
        )


def _copy_tree_safe(src_dir: Path, dst_root: Path) -> tuple[int, int]:
    """Копирует файлы из распакованного репо в установку. Возвращает (copied, skipped)."""
    copied = 0
    skipped = 0

    # Top-level files
    for name in UPDATE_TOP_FILES:
        src = src_dir / name
        if not src.is_file():
            continue
        if _should_preserve(name):
            skipped += 1
            continue
        dst = dst_root / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    # Directories
    for dirname in UPDATE_DIRS:
        src_root = src_dir / dirname
        if not src_root.is_dir():
            continue
        for path in src_root.rglob("*"):
            if path.is_dir():
                continue
            rel = path.relative_to(src_dir).as_posix()
            if _should_preserve(rel):
                skipped += 1
                continue
            dst = dst_root / rel
            # Не затирать существующие пользовательские файлы в preserve-зонах
            # (уже отфильтрованы), и не трогать чужие uploads
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst)
            copied += 1

    # Гарантируем пустые каталоги uploads/avatars
    for sub in ("app/static/uploads", "app/static/avatars"):
        (dst_root / sub).mkdir(parents=True, exist_ok=True)

    return copied, skipped


async def apply_update(*, ref: Optional[str] = None) -> dict[str, Any]:
    """
    Скачивает zipball с GitHub и аккуратно накатывает файлы.
    Не трогает БД, .env, uploads, avatars, встроенный python/.
    """
    repo = _repo_slug()
    branch = _branch()
    target_ref = (ref or "").strip() or branch
    headers = _api_headers()
    info = await check_for_updates()

    if info.source == "release" and info.update_available and not ref:
        # Prefer tag from release
        meta_check = info.remote_version
        if meta_check and not meta_check.startswith("v"):
            # try tag as returned
            pass
        # Re-fetch latest tag name
        async with httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True) as client:
            rel_resp = await client.get(f"https://api.github.com/repos/{repo}/releases/latest")
            if rel_resp.status_code == 200:
                tag = str(rel_resp.json().get("tag_name") or "")
                if tag:
                    target_ref = tag

    zip_url = f"https://api.github.com/repos/{repo}/zipball/{target_ref}"

    with tempfile.TemporaryDirectory(prefix="netvoyodelo-update-") as tmp:
        tmp_path = Path(tmp)
        archive_path = tmp_path / "update.zip"
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()

        async with httpx.AsyncClient(timeout=120.0, headers=headers, follow_redirects=True) as client:
            async with client.stream("GET", zip_url) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"Не удалось скачать обновление (HTTP {resp.status_code}). "
                        f"Проверьте доступ к GitHub и параметр GITHUB_REPO."
                    )
                with archive_path.open("wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)

            # Resolve commit sha for meta
            commit_resp = await client.get(
                f"https://api.github.com/repos/{repo}/commits/{target_ref}"
            )
            sha = ""
            if commit_resp.status_code == 200:
                sha = str(commit_resp.json().get("sha") or "")

        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(extract_dir)

        roots = [p for p in extract_dir.iterdir() if p.is_dir()]
        if not roots:
            raise RuntimeError("Архив обновления пуст или повреждён.")
        src_root = roots[0]

        # Backup VERSION before copy for rollback message
        prev_version = local_version()
        copied, skipped = _copy_tree_safe(src_root, PROJECT_ROOT)

        new_version = local_version()
        # If VERSION didn't change in archive, keep remote tag/version from info
        if new_version == prev_version and info.remote_version:
            version_path = PROJECT_ROOT / "VERSION"
            # only write if release tag looks like a version
            if re.match(r"^v?\d+(\.\d+)*", info.remote_version):
                version_path.write_text(info.remote_version.lstrip("v") + "\n", encoding="utf-8")
                new_version = info.remote_version.lstrip("v")

        meta = load_meta()
        meta.update(
            {
                "version": new_version,
                "commit_sha": sha or info.remote_sha,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "previous_version": prev_version,
                "last_ref": target_ref,
            }
        )
        save_meta(meta)

        pip_note = ""
        req = PROJECT_ROOT / "requirements.txt"
        py_exe = PROJECT_ROOT / "python" / "python.exe"
        if req.exists() and py_exe.exists():
            pip_note = (
                "При необходимости зависимости можно обновить командой "
                "python\\python.exe -m pip install -r requirements.txt"
            )

        return {
            "ok": True,
            "copied": copied,
            "skipped": skipped,
            "previous_version": prev_version,
            "version": new_version,
            "commit_sha": sha or info.remote_sha,
            "ref": target_ref,
            "preserved": [
                "correspondences.db",
                ".env",
                "app/static/uploads/",
                "app/static/avatars/",
                "python/",
            ],
            "restart_required": True,
            "pip_note": pip_note,
            "changelog": info.changelog,
        }
