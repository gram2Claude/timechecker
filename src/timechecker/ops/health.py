"""Диагностика агента (E5, TIME-30): состояние БД, последнего сбора, расписания."""

from __future__ import annotations

from typing import Any

from ..collectors.scheduler import task_exists
from ..storage.db import current_version


def health_check(repo: Any, cfg: Any) -> dict:
    """Сводка здоровья агента: БД, схема, статистика, последний сбор, задача планировщика."""
    return {
        "db_path": str(cfg.db_path),
        "db_exists": cfg.db_path.exists(),
        "schema_version": current_version(repo.conn),
        "stats": repo.stats(),
        "last_ingest": repo.last_ingest_run(),
        "collect_task_scheduled": task_exists("timechecker-collect"),
        "retention_days": cfg.retention_days,
    }
