"""SQLite-реализация repository-интерфейса (TIME-5)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .db import init_db
from .repository import Repository


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteRepository(Repository):
    """DAO поверх SQLite. ``upsert_*`` идемпотентны через ``INSERT … ON CONFLICT``."""

    _SESSION_COLS = (
        "project_id", "task_id", "started_at", "ended_at",
        "message_count", "tool_calls", "tokens_in", "tokens_out",
    )
    _COMMIT_COLS = ("project_id", "branch", "ts_utc", "author", "subject")
    _SUMMARY_COLS = (
        "span_start", "span_end", "active_minutes", "gap_minutes",
        "idle_ge30_count", "idle_ge30_minutes", "tasks_count", "switches",
        "longest_focus_min", "claude_messages", "claude_tokens", "commits", "hygiene_score",
    )
    _TASKTIME_COLS = ("active_minutes", "claude_messages", "claude_tokens", "commits", "est_h")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: Any) -> SqliteRepository:
        """Открыть БД по пути (применяет миграции) и вернуть репозиторий."""
        return cls(init_db(path))

    def close(self) -> None:
        self.conn.close()

    # --- helpers ---
    def _id(self, table: str, where: str, params: tuple) -> int:
        row = self.conn.execute(f"SELECT id FROM {table} WHERE {where}", params).fetchone()
        return int(row["id"])

    def _whitelist_upsert(
        self, table: str, key_cols: list[str], key_vals: list, allowed: tuple,
        fields: dict, conflict: str, ts_col: str,
    ) -> int:
        cols = [c for c in allowed if c in fields]
        insert_cols = [*key_cols, *cols, ts_col]
        values = [*key_vals, *[fields[c] for c in cols], _now()]
        set_parts = [f"{c}=excluded.{c}" for c in cols] + [f"{ts_col}=excluded.{ts_col}"]
        ph = ",".join(["?"] * len(insert_cols))
        self.conn.execute(
            f"INSERT INTO {table}({','.join(insert_cols)}) VALUES({ph}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {', '.join(set_parts)}",
            values,
        )
        self.conn.commit()
        where = " AND ".join(f"{c}=?" for c in key_cols)
        return self._id(table, where, tuple(key_vals))

    # --- справочники ---
    def upsert_employee(self, windows_username, *, display_name=None, dev_branch=None):
        self.conn.execute(
            "INSERT INTO employee(windows_username, display_name, dev_branch, created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(windows_username) DO UPDATE SET "
            "display_name=COALESCE(excluded.display_name, employee.display_name), "
            "dev_branch=COALESCE(excluded.dev_branch, employee.dev_branch)",
            (windows_username, display_name, dev_branch, _now()),
        )
        self.conn.commit()
        return self._id("employee", "windows_username=?", (windows_username,))

    def upsert_project(self, slug, *, claude_project_key=None, repo=None,
                       plane_project_id=None, plane_identifier=None):
        self.conn.execute(
            "INSERT INTO project(slug, claude_project_key, repo, plane_project_id, "
            "plane_identifier, created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
            "claude_project_key=COALESCE(excluded.claude_project_key, project.claude_project_key), "
            "repo=COALESCE(excluded.repo, project.repo), "
            "plane_project_id=COALESCE(excluded.plane_project_id, project.plane_project_id), "
            "plane_identifier=COALESCE(excluded.plane_identifier, project.plane_identifier)",
            (slug, claude_project_key, repo, plane_project_id, plane_identifier, _now()),
        )
        self.conn.commit()
        return self._id("project", "slug=?", (slug,))

    def upsert_task(self, project_id, plane_identifier, *, plane_issue_id=None,
                    canon_task_id=None, title=None, estimate_h=None, status=None):
        self.conn.execute(
            "INSERT INTO task(project_id, plane_identifier, plane_issue_id, canon_task_id, "
            "title, estimate_h, status, updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(plane_identifier) DO UPDATE SET project_id=excluded.project_id, "
            "plane_issue_id=COALESCE(excluded.plane_issue_id, task.plane_issue_id), "
            "canon_task_id=COALESCE(excluded.canon_task_id, task.canon_task_id), "
            "title=COALESCE(excluded.title, task.title), "
            "estimate_h=COALESCE(excluded.estimate_h, task.estimate_h), "
            "status=COALESCE(excluded.status, task.status), updated_at=excluded.updated_at",
            (project_id, plane_identifier, plane_issue_id, canon_task_id, title, estimate_h,
             status, _now()),
        )
        self.conn.commit()
        return self._id("task", "plane_identifier=?", (plane_identifier,))

    # --- прогоны сбора ---
    def start_ingest_run(self, employee_id, *, window_from=None, window_to=None, sources=None):
        cur = self.conn.execute(
            "INSERT INTO ingest_run"
            "(employee_id, started_at, window_from, window_to, sources, status) "
            "VALUES(?,?,?,?,?, 'running')",
            (employee_id, _now(), window_from, window_to, sources),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_ingest_run(self, run_id, status, *, error=None, counts=None):
        self.conn.execute(
            "UPDATE ingest_run SET finished_at=?, status=?, error=?, counts_json=? WHERE id=?",
            (_now(), status, error, json.dumps(counts, ensure_ascii=False) if counts else None,
             run_id),
        )
        self.conn.commit()

    # --- сырьё ---
    def insert_event(self, employee_id, source, event_type, ts_utc, *, project_id=None,
                     task_id=None, external_id=None, meta=None, ingest_run_id=None):
        meta_json = json.dumps(meta, ensure_ascii=False) if meta is not None else None
        cur = self.conn.execute(
            "INSERT INTO activity_event(employee_id, project_id, task_id, source, event_type, "
            "ts_utc, external_id, meta_json, ingest_run_id) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source, external_id) DO UPDATE SET "
            "task_id=COALESCE(excluded.task_id, activity_event.task_id), "
            "meta_json=COALESCE(excluded.meta_json, activity_event.meta_json)",
            (employee_id, project_id, task_id, source, event_type, ts_utc, external_id,
             meta_json, ingest_run_id),
        )
        self.conn.commit()
        if external_id is not None:
            return self._id("activity_event", "source=? AND external_id=?", (source, external_id))
        return int(cur.lastrowid)

    def upsert_claude_session(self, employee_id, session_uid, **fields):
        return self._whitelist_upsert(
            "claude_session", ["employee_id", "session_uid"], [employee_id, session_uid],
            self._SESSION_COLS, fields, "session_uid", "updated_at",
        )

    def upsert_git_commit(self, employee_id, sha, **fields):
        return self._whitelist_upsert(
            "git_commit", ["employee_id", "sha"], [employee_id, sha],
            self._COMMIT_COLS, fields, "sha", "updated_at",
        )

    def link_commit_task(self, commit_id, task_id):
        self.conn.execute(
            "INSERT INTO commit_task(commit_id, task_id) VALUES(?,?) "
            "ON CONFLICT(commit_id, task_id) DO NOTHING",
            (commit_id, task_id),
        )
        self.conn.commit()

    def insert_plane_transition(self, task_id, *, from_state=None, to_state=None,
                                ts_utc=None, external_id=None):
        cur = self.conn.execute(
            "INSERT INTO plane_transition(task_id, from_state, to_state, ts_utc, external_id) "
            "VALUES(?,?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET "
            "from_state=excluded.from_state, to_state=excluded.to_state, ts_utc=excluded.ts_utc",
            (task_id, from_state, to_state, ts_utc, external_id),
        )
        self.conn.commit()
        if external_id is not None:
            return self._id("plane_transition", "external_id=?", (external_id,))
        return int(cur.lastrowid)

    # --- дневные агрегаты ---
    def upsert_daily_summary(self, employee_id, work_date, **fields):
        return self._whitelist_upsert(
            "daily_summary", ["employee_id", "work_date"], [employee_id, work_date],
            self._SUMMARY_COLS, fields, "employee_id, work_date", "computed_at",
        )

    def upsert_daily_task_time(self, employee_id, work_date, task_id, **fields):
        return self._whitelist_upsert(
            "daily_task_time", ["employee_id", "work_date", "task_id"],
            [employee_id, work_date, task_id], self._TASKTIME_COLS, fields,
            "employee_id, work_date, task_id", "computed_at",
        )

    def insert_daily_idle(self, employee_id, work_date, gap_start, gap_end, minutes):
        cur = self.conn.execute(
            "INSERT INTO daily_idle"
            "(employee_id, work_date, gap_start, gap_end, minutes, computed_at) "
            "VALUES(?,?,?,?,?,?)",
            (employee_id, work_date, gap_start, gap_end, minutes, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # --- чтение / обслуживание ---
    def get_employee(self, windows_username):
        row = self.conn.execute(
            "SELECT * FROM employee WHERE windows_username=?", (windows_username,)
        ).fetchone()
        return dict(row) if row is not None else None

    def events_between(self, employee_id, start_utc, end_utc):
        rows = self.conn.execute(
            "SELECT * FROM activity_event WHERE employee_id=? AND ts_utc>=? AND ts_utc<=? "
            "ORDER BY ts_utc",
            (employee_id, start_utc, end_utc),
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_raw(self, before_utc):
        c = self.conn
        c.execute(
            "DELETE FROM commit_task WHERE commit_id IN "
            "(SELECT id FROM git_commit WHERE COALESCE(ts_utc,'') < ?)",
            (before_utc,),
        )
        total = 0
        for sql in (
            "DELETE FROM activity_event WHERE ts_utc < ?",
            "DELETE FROM claude_session WHERE COALESCE(started_at,'') < ?",
            "DELETE FROM git_commit WHERE COALESCE(ts_utc,'') < ?",
            "DELETE FROM plane_transition WHERE COALESCE(ts_utc,'') < ?",
        ):
            total += c.execute(sql, (before_utc,)).rowcount
        c.commit()
        return total
