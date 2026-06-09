"""Слой хранилища timechecker: repository DAO (SQLite/Postgres) + фабрика по конфигу."""

from __future__ import annotations

from typing import Any

from .db import apply_migrations, connect, current_version, init_db
from .repository import Repository
from .sqlite_repository import SqliteRepository


def open_repository(cfg: Any) -> Repository:
    """Открыть репозиторий по конфигу: Postgres при заданном ``db_url``, иначе SQLite."""
    if getattr(cfg, "db_url", None):
        from .postgres_repository import PostgresRepository

        return PostgresRepository.open(cfg.db_url)
    return SqliteRepository.open(cfg.db_path)


__all__ = [
    "connect",
    "init_db",
    "apply_migrations",
    "current_version",
    "Repository",
    "SqliteRepository",
    "open_repository",
]
