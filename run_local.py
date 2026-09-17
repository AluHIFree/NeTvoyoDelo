"""
Локальный запуск для портативной сборки (только localhost, без reload).
Читает .env из папки приложения.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config_env import load_project_env, get_env

load_project_env(override=True)

HOST = get_env("HOST", "127.0.0.1") or "127.0.0.1"
PORT = int(get_env("PORT", "8000") or "8000")


def main() -> None:
    import uvicorn

    print("=" * 50)
    print("  NeTvoyoDelo — локальный сервис (SQLite)")
    from app.database import DB_PATH
    print(f"  База: {DB_PATH}")
    print(f"  Откройте в браузере: http://{HOST}:{PORT}")
    print("  Остановка: закройте это окно или STOP.bat")
    print("=" * 50)
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
