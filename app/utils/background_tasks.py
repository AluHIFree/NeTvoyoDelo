"""
Устаревший модуль. Единственный источник планировщика — app.scheduler.
Оставлен как тонкий re-export для обратной совместимости импортов.
"""
from app.scheduler import (  # noqa: F401
    get_next_run_time,
    start_scheduler,
    stop_scheduler,
)
