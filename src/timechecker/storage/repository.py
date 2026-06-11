"""Абстрактный repository-интерфейс (TIME-5).

DAO-граница изолирует выбор СУБД: сейчас SQLite, позже серверная БД — реализуется отдельным
классом без изменения вызывающего кода. Коллекторы/метрики/отчёты зависят только от этого
интерфейса.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Repository(ABC):
    """Контракт доступа к данным timechecker. Все ``upsert_*`` идемпотентны по бизнес-ключам."""

    # --- справочники ---
    @abstractmethod
    def upsert_employee(
        self, windows_username: str, *, display_name: str | None = None,
        dev_branch: str | None = None,
    ) -> int: ...

    @abstractmethod
    def upsert_project(
        self, slug: str, *, claude_project_key: str | None = None, repo: str | None = None,
        plane_project_id: str | None = None, plane_identifier: str | None = None,
    ) -> int: ...

    @abstractmethod
    def upsert_task(
        self, project_id: int, plane_identifier: str, *, plane_issue_id: str | None = None,
        canon_task_id: str | None = None, title: str | None = None,
        estimate_h: float | None = None, status: str | None = None,
    ) -> int: ...

    # --- прогоны сбора ---
    @abstractmethod
    def start_ingest_run(
        self, employee_id: int, *, window_from: str | None = None,
        window_to: str | None = None, sources: str | None = None,
    ) -> int: ...

    @abstractmethod
    def finish_ingest_run(
        self, run_id: int, status: str, *, error: str | None = None,
        counts: dict | None = None,
    ) -> None: ...

    # --- сырьё (только метаданные) ---
    @abstractmethod
    def insert_event(
        self, employee_id: int, source: str, event_type: str, ts_utc: str, *,
        project_id: int | None = None, task_id: int | None = None,
        external_id: str | None = None, meta: dict | None = None,
        ingest_run_id: int | None = None,
    ) -> int: ...

    @abstractmethod
    def upsert_agent_session(
        self, employee_id: int, source: str, session_uid: str, **fields: Any
    ) -> int: ...

    @abstractmethod
    def upsert_git_commit(self, employee_id: int, sha: str, **fields: Any) -> int: ...

    @abstractmethod
    def link_commit_task(self, commit_id: int, task_id: int) -> None: ...

    @abstractmethod
    def insert_plane_transition(
        self, task_id: int, *, from_state: str | None = None, to_state: str | None = None,
        ts_utc: str | None = None, external_id: str | None = None,
    ) -> int: ...

    # --- дневные агрегаты ---
    @abstractmethod
    def upsert_daily_summary(self, employee_id: int, work_date: str, **fields: Any) -> int: ...

    @abstractmethod
    def upsert_daily_task_time(
        self, employee_id: int, work_date: str, task_id: int, **fields: Any
    ) -> int: ...

    @abstractmethod
    def insert_daily_idle(
        self, employee_id: int, work_date: str, gap_start: str, gap_end: str, minutes: int
    ) -> int: ...

    @abstractmethod
    def insert_daily_agent_usage(
        self, employee_id: int, work_date: str, task_id: int | None, source: str,
        **fields: Any,
    ) -> int: ...

    @abstractmethod
    def delete_daily_agent_usage(self, employee_id: int, work_date: str) -> None: ...

    @abstractmethod
    def daily_agent_usage(self, employee_id: int, work_date: str) -> list[dict]: ...

    # --- чтение / обслуживание ---
    @abstractmethod
    def get_employee(self, windows_username: str) -> dict | None: ...

    @abstractmethod
    def get_project(self, slug: str) -> dict | None: ...

    @abstractmethod
    def task_id_by_identifier(self, plane_identifier: str) -> int | None: ...

    @abstractmethod
    def all_tasks(self) -> list[dict]: ...

    @abstractmethod
    def all_plane_transitions(self) -> list[dict]: ...

    @abstractmethod
    def commits_between(self, employee_id: int, start_utc: str, end_utc: str) -> list[dict]: ...

    @abstractmethod
    def delete_daily_idle(self, employee_id: int, work_date: str) -> None: ...

    @abstractmethod
    def delete_daily_task_time(self, employee_id: int, work_date: str) -> None: ...

    @abstractmethod
    def events_between(self, employee_id: int, start_utc: str, end_utc: str) -> list[dict]: ...

    @abstractmethod
    def get_daily_summary(self, employee_id: int, work_date: str) -> dict | None: ...

    @abstractmethod
    def daily_task_times(self, employee_id: int, work_date: str) -> list[dict]: ...

    @abstractmethod
    def daily_idles(self, employee_id: int, work_date: str) -> list[dict]: ...

    @abstractmethod
    def last_ingest_run(self) -> dict | None: ...

    @abstractmethod
    def stats(self) -> dict: ...

    @abstractmethod
    def schema_version(self) -> int: ...

    @abstractmethod
    def prune_raw(self, before_utc: str) -> int: ...

    @abstractmethod
    def close(self) -> None: ...
