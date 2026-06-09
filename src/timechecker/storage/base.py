"""Базовый SQL-репозиторий (E6, TIME-36): вся логика методов на бэкенд-нейтральных примитивах.

SQL пишется с плейсхолдером ``?``; backend-подкласс (`SqliteRepository`/`PostgresRepository`)
реализует примитивы `_exec/_query/_fetchone/_insert/_executescript`, трансляцию плейсхолдеров
(`_q`) и список миграций (`MIGRATIONS`). Контракт — `Repository`. `INSERT … ON CONFLICT` и
`excluded.*` совместимы с SQLite (3.24+) и Postgres (9.5+).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .repository import Repository


class BaseSqlRepository(Repository):
    """Общая SQL-логика. Backend-специфика — в примитивах подкласса."""

    MIGRATIONS: list[tuple[int, str]] = []

    _SESSION_COLS = (
        "project_id", "task_id", "started_at", "ended_at",
        "message_count", "tool_calls", "tokens_in", "tokens_out",
        "cache_read", "cache_creation", "model",
    )
    _COMMIT_COLS = ("project_id", "branch", "ts_utc", "author", "subject")
    _SUMMARY_COLS = (
        "span_start", "span_end", "active_minutes", "gap_minutes",
        "idle_ge30_count", "idle_ge30_minutes", "tasks_count", "switches",
        "longest_focus_min", "commits", "hygiene_score", "models",
    )
    _TASKTIME_COLS = ("active_minutes", "commits", "est_h")
    _USAGE_COLS = ("messages", "tokens", "cache_read", "cache_creation", "cost_usd")

    # ---- примитивы (переопределяются backend-подклассом) ----
    def _q(self, sql: str) -> str:
        return sql

    def _exec(self, sql: str, params: tuple = ()) -> None:
        raise NotImplementedError

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        raise NotImplementedError

    def _fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        raise NotImplementedError

    def _insert(self, sql: str, params: tuple = ()) -> int:
        raise NotImplementedError

    def _executescript(self, sql: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def _scalar(self, sql: str, params: tuple = ()) -> Any:
        row = self._fetchone(sql, params)
        return None if row is None else next(iter(row.values()))

    def _id(self, table: str, where: str, params: tuple) -> int:
        return int(self._scalar(f"SELECT id FROM {table} WHERE {where}", params))

    def _wl_upsert(self, table: str, key_cols: list[str], key_vals: list, allowed: tuple,
                   fields: dict, conflict: str, ts_col: str) -> int:
        cols = [c for c in allowed if c in fields]
        insert_cols = [*key_cols, *cols, ts_col]
        values = [*key_vals, *[fields[c] for c in cols], _now()]
        set_parts = [f"{c}=excluded.{c}" for c in cols] + [f"{ts_col}=excluded.{ts_col}"]
        ph = ",".join(["?"] * len(insert_cols))
        self._exec(
            f"INSERT INTO {table}({','.join(insert_cols)}) VALUES({ph}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {', '.join(set_parts)}", values)
        where = " AND ".join(f"{c}=?" for c in key_cols)
        return self._id(table, where, tuple(key_vals))

    # ---- миграции ----
    def _ensure_migrations_table(self) -> None:
        self._exec("CREATE TABLE IF NOT EXISTS schema_migrations "
                   "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")

    def schema_version(self) -> int:
        self._ensure_migrations_table()
        v = self._scalar("SELECT MAX(version) FROM schema_migrations")
        return int(v) if v is not None else 0

    def apply_migrations(self) -> int:
        self._ensure_migrations_table()
        applied = self.schema_version()
        now = _now()
        for version, sql in self.MIGRATIONS:
            if version > applied:
                self._executescript(sql)
                self._exec("INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                           (version, now))
                applied = version
        return applied

    # ---- справочники ----
    def upsert_employee(self, windows_username, *, display_name=None, dev_branch=None):
        self._exec(
            "INSERT INTO employee(windows_username, display_name, dev_branch, created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(windows_username) DO UPDATE SET "
            "display_name=COALESCE(excluded.display_name, employee.display_name), "
            "dev_branch=COALESCE(excluded.dev_branch, employee.dev_branch)",
            (windows_username, display_name, dev_branch, _now()))
        return self._id("employee", "windows_username=?", (windows_username,))

    def upsert_project(self, slug, *, claude_project_key=None, repo=None,
                       plane_project_id=None, plane_identifier=None):
        self._exec(
            "INSERT INTO project(slug, claude_project_key, repo, plane_project_id, "
            "plane_identifier, created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
            "claude_project_key=COALESCE(excluded.claude_project_key, project.claude_project_key), "
            "repo=COALESCE(excluded.repo, project.repo), "
            "plane_project_id=COALESCE(excluded.plane_project_id, project.plane_project_id), "
            "plane_identifier=COALESCE(excluded.plane_identifier, project.plane_identifier)",
            (slug, claude_project_key, repo, plane_project_id, plane_identifier, _now()))
        return self._id("project", "slug=?", (slug,))

    def upsert_task(self, project_id, plane_identifier, *, plane_issue_id=None,
                    canon_task_id=None, title=None, estimate_h=None, status=None):
        self._exec(
            "INSERT INTO task(project_id, plane_identifier, plane_issue_id, canon_task_id, "
            "title, estimate_h, status, updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(plane_identifier) DO UPDATE SET project_id=excluded.project_id, "
            "plane_issue_id=COALESCE(excluded.plane_issue_id, task.plane_issue_id), "
            "canon_task_id=COALESCE(excluded.canon_task_id, task.canon_task_id), "
            "title=COALESCE(excluded.title, task.title), "
            "estimate_h=COALESCE(excluded.estimate_h, task.estimate_h), "
            "status=COALESCE(excluded.status, task.status), updated_at=excluded.updated_at",
            (project_id, plane_identifier, plane_issue_id, canon_task_id, title, estimate_h,
             status, _now()))
        return self._id("task", "plane_identifier=?", (plane_identifier,))

    # ---- прогоны сбора ----
    def start_ingest_run(self, employee_id, *, window_from=None, window_to=None, sources=None):
        return self._insert(
            "INSERT INTO ingest_run(employee_id, started_at, window_from, window_to, sources, "
            "status) VALUES(?,?,?,?,?, 'running')",
            (employee_id, _now(), window_from, window_to, sources))

    def finish_ingest_run(self, run_id, status, *, error=None, counts=None):
        self._exec(
            "UPDATE ingest_run SET finished_at=?, status=?, error=?, counts_json=? WHERE id=?",
            (_now(), status, error, json.dumps(counts, ensure_ascii=False) if counts else None,
             run_id))

    # ---- сырьё ----
    def insert_event(self, employee_id, source, event_type, ts_utc, *, project_id=None,
                     task_id=None, external_id=None, meta=None, ingest_run_id=None):
        meta_json = json.dumps(meta, ensure_ascii=False) if meta is not None else None
        sql = ("INSERT INTO activity_event(employee_id, project_id, task_id, source, event_type, "
               "ts_utc, external_id, meta_json, ingest_run_id) VALUES(?,?,?,?,?,?,?,?,?) "
               "ON CONFLICT(source, external_id) DO UPDATE SET "
               "task_id=COALESCE(excluded.task_id, activity_event.task_id), "
               "meta_json=COALESCE(excluded.meta_json, activity_event.meta_json)")
        params = (employee_id, project_id, task_id, source, event_type, ts_utc, external_id,
                  meta_json, ingest_run_id)
        if external_id is not None:
            self._exec(sql, params)
            return self._id("activity_event", "source=? AND external_id=?", (source, external_id))
        return self._insert(sql, params)

    def upsert_agent_session(self, employee_id, source, session_uid, **fields):
        return self._wl_upsert("agent_session", ["employee_id", "source", "session_uid"],
                               [employee_id, source, session_uid], self._SESSION_COLS, fields,
                               "source, session_uid", "updated_at")

    def upsert_git_commit(self, employee_id, sha, **fields):
        return self._wl_upsert("git_commit", ["employee_id", "sha"], [employee_id, sha],
                               self._COMMIT_COLS, fields, "sha", "updated_at")

    def link_commit_task(self, commit_id, task_id):
        self._exec("INSERT INTO commit_task(commit_id, task_id) VALUES(?,?) "
                   "ON CONFLICT(commit_id, task_id) DO NOTHING", (commit_id, task_id))

    def insert_plane_transition(self, task_id, *, from_state=None, to_state=None,
                                ts_utc=None, external_id=None):
        sql = ("INSERT INTO plane_transition(task_id, from_state, to_state, ts_utc, external_id) "
               "VALUES(?,?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET "
               "from_state=excluded.from_state, to_state=excluded.to_state, ts_utc=excluded.ts_utc")
        params = (task_id, from_state, to_state, ts_utc, external_id)
        if external_id is not None:
            self._exec(sql, params)
            return self._id("plane_transition", "external_id=?", (external_id,))
        return self._insert(sql, params)

    # ---- дневные агрегаты ----
    def upsert_daily_summary(self, employee_id, work_date, **fields):
        return self._wl_upsert("daily_summary", ["employee_id", "work_date"],
                               [employee_id, work_date], self._SUMMARY_COLS, fields,
                               "employee_id, work_date", "computed_at")

    def upsert_daily_task_time(self, employee_id, work_date, task_id, **fields):
        return self._wl_upsert("daily_task_time", ["employee_id", "work_date", "task_id"],
                               [employee_id, work_date, task_id], self._TASKTIME_COLS, fields,
                               "employee_id, work_date, task_id", "computed_at")

    def insert_daily_idle(self, employee_id, work_date, gap_start, gap_end, minutes):
        return self._insert(
            "INSERT INTO daily_idle(employee_id, work_date, gap_start, gap_end, minutes, "
            "computed_at) VALUES(?,?,?,?,?,?)",
            (employee_id, work_date, gap_start, gap_end, minutes, _now()))

    def insert_daily_agent_usage(self, employee_id, work_date, task_id, source, **fields):
        # идемпотентность — delete-replace по дню (delete_daily_agent_usage), как daily_idle:
        # UNIQUE-ключ невозможен из-за NULL-able task_id
        cols = [c for c in self._USAGE_COLS if c in fields]
        names = ", ".join(["employee_id", "work_date", "task_id", "source", *cols, "computed_at"])
        ph = ",".join(["?"] * (5 + len(cols)))
        return self._insert(
            f"INSERT INTO daily_agent_usage({names}) VALUES({ph})",
            (employee_id, work_date, task_id, source, *[fields[c] for c in cols], _now()))

    def delete_daily_agent_usage(self, employee_id, work_date):
        self._exec("DELETE FROM daily_agent_usage WHERE employee_id=? AND work_date=?",
                   (employee_id, work_date))

    # ---- чтение / обслуживание ----
    def get_employee(self, windows_username):
        return self._fetchone("SELECT * FROM employee WHERE windows_username=?",
                              (windows_username,))

    def task_id_by_identifier(self, plane_identifier):
        v = self._scalar("SELECT id FROM task WHERE plane_identifier=?", (plane_identifier,))
        return int(v) if v is not None else None

    def all_tasks(self):
        return self._query("SELECT * FROM task")

    def all_plane_transitions(self):
        return self._query("SELECT task_id, from_state, to_state, ts_utc FROM plane_transition "
                           "WHERE ts_utc IS NOT NULL")

    def commits_between(self, employee_id, start_utc, end_utc):
        rows = self._query(
            "SELECT id, sha, ts_utc, subject FROM git_commit "
            "WHERE employee_id=? AND ts_utc>=? AND ts_utc<=? ORDER BY ts_utc",
            (employee_id, start_utc, end_utc))
        for r in rows:
            r["task_ids"] = [x["task_id"] for x in self._query(
                "SELECT task_id FROM commit_task WHERE commit_id=?", (r["id"],))]
        return rows

    def delete_daily_idle(self, employee_id, work_date):
        self._exec("DELETE FROM daily_idle WHERE employee_id=? AND work_date=?",
                   (employee_id, work_date))

    def delete_daily_task_time(self, employee_id, work_date):
        self._exec("DELETE FROM daily_task_time WHERE employee_id=? AND work_date=?",
                   (employee_id, work_date))

    def events_between(self, employee_id, start_utc, end_utc):
        return self._query(
            "SELECT * FROM activity_event WHERE employee_id=? AND ts_utc>=? AND ts_utc<=? "
            "ORDER BY ts_utc", (employee_id, start_utc, end_utc))

    def get_daily_summary(self, employee_id, work_date):
        return self._fetchone("SELECT * FROM daily_summary WHERE employee_id=? AND work_date=?",
                              (employee_id, work_date))

    def daily_task_times(self, employee_id, work_date):
        return self._query(
            "SELECT d.*, t.plane_identifier, t.title FROM daily_task_time d "
            "JOIN task t ON t.id = d.task_id "
            "WHERE d.employee_id=? AND d.work_date=? ORDER BY d.active_minutes DESC",
            (employee_id, work_date))

    def daily_idles(self, employee_id, work_date):
        return self._query(
            "SELECT gap_start, gap_end, minutes FROM daily_idle "
            "WHERE employee_id=? AND work_date=? ORDER BY gap_start", (employee_id, work_date))

    def daily_agent_usage(self, employee_id, work_date):
        return self._query(
            "SELECT u.*, t.plane_identifier, t.title FROM daily_agent_usage u "
            "LEFT JOIN task t ON t.id = u.task_id "
            "WHERE u.employee_id=? AND u.work_date=? ORDER BY u.tokens DESC",
            (employee_id, work_date))

    def last_ingest_run(self):
        return self._fetchone(
            "SELECT status, started_at, finished_at, sources, error, counts_json "
            "FROM ingest_run ORDER BY id DESC LIMIT 1")

    def stats(self):
        def n(table: str) -> int:
            return int(self._scalar(f"SELECT COUNT(*) FROM {table}"))
        return {
            "events": n("activity_event"), "agent_sessions": n("agent_session"),
            "git_commits": n("git_commit"), "plane_transitions": n("plane_transition"),
            "tasks": n("task"), "daily_summaries": n("daily_summary"),
        }

    def prune_raw(self, before_utc):
        self._exec("DELETE FROM commit_task WHERE commit_id IN "
                   "(SELECT id FROM git_commit WHERE COALESCE(ts_utc,'') < ?)", (before_utc,))
        total = 0
        for sql in (
            "DELETE FROM activity_event WHERE ts_utc < ?",
            "DELETE FROM agent_session WHERE COALESCE(started_at,'') < ?",
            "DELETE FROM git_commit WHERE COALESCE(ts_utc,'') < ?",
            "DELETE FROM plane_transition WHERE COALESCE(ts_utc,'') < ?",
        ):
            total += self._delete_count(sql, (before_utc,))
        return total

    def _delete_count(self, sql: str, params: tuple) -> int:
        raise NotImplementedError


def _now() -> str:
    return datetime.now(UTC).isoformat()
