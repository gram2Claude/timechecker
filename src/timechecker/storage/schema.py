"""SQLite-схема timechecker (TIME-4) — forward-only миграции.

Каждая миграция — ``(version, sql)``. Раннер (``db.apply_migrations``) применяет недостающие
версии и фиксирует их в ``schema_migrations`` (её создаёт сам раннер). Типы переносимы к серверной
БД (Postgres позже): INTEGER/TEXT/REAL; время — TEXT ISO-8601 UTC; JSON — TEXT.
"""

from __future__ import annotations

_V1 = """
CREATE TABLE employee (
  id               INTEGER PRIMARY KEY,
  windows_username TEXT NOT NULL UNIQUE,
  display_name     TEXT,
  dev_branch       TEXT,
  active           INTEGER NOT NULL DEFAULT 1,
  created_at       TEXT NOT NULL
);

CREATE TABLE project (
  id                 INTEGER PRIMARY KEY,
  slug               TEXT NOT NULL UNIQUE,
  claude_project_key TEXT,
  repo               TEXT,
  plane_project_id   TEXT,
  plane_identifier   TEXT,
  created_at         TEXT NOT NULL
);

CREATE TABLE task (
  id               INTEGER PRIMARY KEY,
  project_id       INTEGER NOT NULL REFERENCES project(id),
  plane_issue_id   TEXT UNIQUE,
  plane_identifier TEXT UNIQUE,
  canon_task_id    TEXT,
  title            TEXT,
  estimate_h       REAL,
  status           TEXT,
  updated_at       TEXT
);

CREATE TABLE ingest_run (
  id          INTEGER PRIMARY KEY,
  employee_id INTEGER NOT NULL REFERENCES employee(id),
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  window_from TEXT,
  window_to   TEXT,
  sources     TEXT,
  status      TEXT NOT NULL DEFAULT 'running',
  error       TEXT,
  counts_json TEXT
);

CREATE TABLE activity_event (
  id            INTEGER PRIMARY KEY,
  employee_id   INTEGER NOT NULL REFERENCES employee(id),
  project_id    INTEGER REFERENCES project(id),
  task_id       INTEGER REFERENCES task(id),
  source        TEXT NOT NULL,
  event_type    TEXT NOT NULL,
  ts_utc        TEXT NOT NULL,
  external_id   TEXT,
  meta_json     TEXT,
  ingest_run_id INTEGER REFERENCES ingest_run(id),
  UNIQUE(source, external_id)
);
CREATE INDEX ix_event_emp_ts  ON activity_event(employee_id, ts_utc);
CREATE INDEX ix_event_task_ts ON activity_event(task_id, ts_utc);

CREATE TABLE claude_session (
  id            INTEGER PRIMARY KEY,
  employee_id   INTEGER NOT NULL REFERENCES employee(id),
  project_id    INTEGER REFERENCES project(id),
  task_id       INTEGER REFERENCES task(id),
  session_uid   TEXT NOT NULL UNIQUE,
  started_at    TEXT,
  ended_at      TEXT,
  message_count INTEGER DEFAULT 0,
  tool_calls    INTEGER DEFAULT 0,
  tokens_in     INTEGER DEFAULT 0,
  tokens_out    INTEGER DEFAULT 0,
  updated_at    TEXT
);
CREATE INDEX ix_session_emp ON claude_session(employee_id, started_at);

CREATE TABLE git_commit (
  id          INTEGER PRIMARY KEY,
  employee_id INTEGER NOT NULL REFERENCES employee(id),
  project_id  INTEGER REFERENCES project(id),
  sha         TEXT NOT NULL UNIQUE,
  branch      TEXT,
  ts_utc      TEXT,
  author      TEXT,
  subject     TEXT,
  updated_at  TEXT
);
CREATE INDEX ix_commit_emp ON git_commit(employee_id, ts_utc);

CREATE TABLE commit_task (
  commit_id INTEGER NOT NULL REFERENCES git_commit(id),
  task_id   INTEGER NOT NULL REFERENCES task(id),
  PRIMARY KEY (commit_id, task_id)
);

CREATE TABLE plane_transition (
  id          INTEGER PRIMARY KEY,
  task_id     INTEGER NOT NULL REFERENCES task(id),
  from_state  TEXT,
  to_state    TEXT,
  ts_utc      TEXT,
  external_id TEXT UNIQUE
);

CREATE TABLE daily_summary (
  id                INTEGER PRIMARY KEY,
  employee_id       INTEGER NOT NULL REFERENCES employee(id),
  work_date         TEXT NOT NULL,
  span_start        TEXT,
  span_end          TEXT,
  active_minutes    INTEGER DEFAULT 0,
  gap_minutes       INTEGER DEFAULT 0,
  idle_ge30_count   INTEGER DEFAULT 0,
  idle_ge30_minutes INTEGER DEFAULT 0,
  tasks_count       INTEGER DEFAULT 0,
  switches          INTEGER DEFAULT 0,
  longest_focus_min INTEGER DEFAULT 0,
  claude_messages   INTEGER DEFAULT 0,
  claude_tokens     INTEGER DEFAULT 0,
  commits           INTEGER DEFAULT 0,
  hygiene_score     REAL,
  computed_at       TEXT,
  UNIQUE(employee_id, work_date)
);

CREATE TABLE daily_task_time (
  id              INTEGER PRIMARY KEY,
  employee_id     INTEGER NOT NULL REFERENCES employee(id),
  work_date       TEXT NOT NULL,
  task_id         INTEGER NOT NULL REFERENCES task(id),
  active_minutes  INTEGER DEFAULT 0,
  claude_messages INTEGER DEFAULT 0,
  claude_tokens   INTEGER DEFAULT 0,
  commits         INTEGER DEFAULT 0,
  est_h           REAL,
  computed_at     TEXT,
  UNIQUE(employee_id, work_date, task_id)
);

CREATE TABLE daily_idle (
  id          INTEGER PRIMARY KEY,
  employee_id INTEGER NOT NULL REFERENCES employee(id),
  work_date   TEXT NOT NULL,
  gap_start   TEXT NOT NULL,
  gap_end     TEXT NOT NULL,
  minutes     INTEGER NOT NULL,
  computed_at TEXT
);
CREATE INDEX ix_idle_emp_date ON daily_idle(employee_id, work_date);
"""

_V2 = """
ALTER TABLE claude_session ADD COLUMN cache_read INTEGER DEFAULT 0;
ALTER TABLE claude_session ADD COLUMN cache_creation INTEGER DEFAULT 0;
ALTER TABLE claude_session ADD COLUMN model TEXT;
ALTER TABLE daily_summary ADD COLUMN claude_cache_read INTEGER DEFAULT 0;
ALTER TABLE daily_summary ADD COLUMN claude_cache_creation INTEGER DEFAULT 0;
ALTER TABLE daily_summary ADD COLUMN claude_cost_usd REAL DEFAULT 0;
ALTER TABLE daily_summary ADD COLUMN models TEXT;
ALTER TABLE daily_task_time ADD COLUMN claude_cache_read INTEGER DEFAULT 0;
ALTER TABLE daily_task_time ADD COLUMN claude_cache_creation INTEGER DEFAULT 0;
ALTER TABLE daily_task_time ADD COLUMN claude_cost_usd REAL DEFAULT 0;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, _V1),
    (2, _V2),
]
