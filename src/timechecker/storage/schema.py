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

# v3 (E8, codex_usage): мультиагентная схема — claude_session → agent_session (ключ
# (source, session_uid) — inline-UNIQUE по одному session_uid не расширить ALTER'ом, поэтому
# пересоздание), обобщённая daily_agent_usage (токены/стоимость по агентам; идемпотентность —
# delete-replace по дню, UNIQUE невозможен из-за NULL-able task_id), бэкфилл claude_* агрегатов
# и дроп переехавших колонок. Время (active/idle) остаётся в daily_summary/daily_task_time.
_V3 = """
BEGIN;
CREATE TABLE agent_session (
  id            INTEGER PRIMARY KEY,
  employee_id   INTEGER NOT NULL REFERENCES employee(id),
  project_id    INTEGER REFERENCES project(id),
  task_id       INTEGER REFERENCES task(id),
  source        TEXT NOT NULL DEFAULT 'claude',
  session_uid   TEXT NOT NULL,
  started_at    TEXT,
  ended_at      TEXT,
  message_count INTEGER DEFAULT 0,
  tool_calls    INTEGER DEFAULT 0,
  tokens_in     INTEGER DEFAULT 0,
  tokens_out    INTEGER DEFAULT 0,
  cache_read    INTEGER DEFAULT 0,
  cache_creation INTEGER DEFAULT 0,
  model         TEXT,
  updated_at    TEXT,
  UNIQUE(source, session_uid)
);
INSERT INTO agent_session (id, employee_id, project_id, task_id, source, session_uid,
  started_at, ended_at, message_count, tool_calls, tokens_in, tokens_out,
  cache_read, cache_creation, model, updated_at)
SELECT id, employee_id, project_id, task_id, 'claude', session_uid,
  started_at, ended_at, message_count, tool_calls, tokens_in, tokens_out,
  cache_read, cache_creation, model, updated_at
FROM claude_session;
DROP TABLE claude_session;
CREATE INDEX ix_agent_session_emp ON agent_session(employee_id, started_at);

CREATE TABLE daily_agent_usage (
  id             INTEGER PRIMARY KEY,
  employee_id    INTEGER NOT NULL REFERENCES employee(id),
  work_date      TEXT    NOT NULL,
  task_id        INTEGER REFERENCES task(id),
  source         TEXT    NOT NULL DEFAULT 'claude',
  messages       INTEGER NOT NULL DEFAULT 0,
  tokens         INTEGER NOT NULL DEFAULT 0,
  cache_read     INTEGER NOT NULL DEFAULT 0,
  cache_creation INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL    NOT NULL DEFAULT 0,
  computed_at    TEXT
);
CREATE INDEX ix_agent_usage_emp_date ON daily_agent_usage(employee_id, work_date);

INSERT INTO daily_agent_usage (employee_id, work_date, task_id, source, messages, tokens,
  cache_read, cache_creation, cost_usd, computed_at)
SELECT employee_id, work_date, task_id, 'claude',
  COALESCE(claude_messages, 0), COALESCE(claude_tokens, 0),
  COALESCE(claude_cache_read, 0), COALESCE(claude_cache_creation, 0),
  COALESCE(claude_cost_usd, 0), computed_at
FROM daily_task_time
WHERE COALESCE(claude_messages, 0) + COALESCE(claude_tokens, 0)
      + COALESCE(claude_cost_usd, 0) > 0;

INSERT INTO daily_agent_usage (employee_id, work_date, task_id, source, messages, tokens,
  cache_read, cache_creation, cost_usd, computed_at)
SELECT s.employee_id, s.work_date, NULL, 'claude',
  MAX(0, COALESCE(s.claude_messages, 0) - COALESCE(t.m, 0)),
  MAX(0, COALESCE(s.claude_tokens, 0) - COALESCE(t.tok, 0)),
  MAX(0, COALESCE(s.claude_cache_read, 0) - COALESCE(t.cr, 0)),
  MAX(0, COALESCE(s.claude_cache_creation, 0) - COALESCE(t.cc, 0)),
  MAX(0, COALESCE(s.claude_cost_usd, 0) - COALESCE(t.cost, 0)),
  s.computed_at
FROM daily_summary s
LEFT JOIN (
  SELECT employee_id, work_date,
    SUM(COALESCE(claude_messages, 0)) m, SUM(COALESCE(claude_tokens, 0)) tok,
    SUM(COALESCE(claude_cache_read, 0)) cr, SUM(COALESCE(claude_cache_creation, 0)) cc,
    SUM(COALESCE(claude_cost_usd, 0)) cost
  FROM daily_task_time GROUP BY employee_id, work_date
) t ON t.employee_id = s.employee_id AND t.work_date = s.work_date
WHERE COALESCE(s.claude_messages, 0) > COALESCE(t.m, 0)
   OR COALESCE(s.claude_tokens, 0) > COALESCE(t.tok, 0)
   OR COALESCE(s.claude_cache_read, 0) > COALESCE(t.cr, 0)
   OR COALESCE(s.claude_cache_creation, 0) > COALESCE(t.cc, 0)
   OR COALESCE(s.claude_cost_usd, 0) > COALESCE(t.cost, 0) + 1e-9;

ALTER TABLE daily_summary DROP COLUMN claude_messages;
ALTER TABLE daily_summary DROP COLUMN claude_tokens;
ALTER TABLE daily_summary DROP COLUMN claude_cache_read;
ALTER TABLE daily_summary DROP COLUMN claude_cache_creation;
ALTER TABLE daily_summary DROP COLUMN claude_cost_usd;
ALTER TABLE daily_task_time DROP COLUMN claude_messages;
ALTER TABLE daily_task_time DROP COLUMN claude_tokens;
ALTER TABLE daily_task_time DROP COLUMN claude_cache_read;
ALTER TABLE daily_task_time DROP COLUMN claude_cache_creation;
ALTER TABLE daily_task_time DROP COLUMN claude_cost_usd;
COMMIT;
"""

# v4 (E9.1, plane_exit): Plane выведен из контура — нейтральные имена. Таблица переходов
# и readable-ID теперь принадлежат собственному реестру задач (CLI `timechecker task`);
# plane_issue_id оставлен как external_uid (историческая привязка к внешним трекерам).
_V4 = """
BEGIN;
ALTER TABLE plane_transition RENAME TO task_transition;
ALTER TABLE task RENAME COLUMN plane_identifier TO identifier;
ALTER TABLE task RENAME COLUMN plane_issue_id TO external_uid;
ALTER TABLE project RENAME COLUMN plane_identifier TO identifier_prefix;
ALTER TABLE project DROP COLUMN plane_project_id;
COMMIT;
"""

# v5 (спека 11, misc_works): справочник спринтов канона + привязка задач к спринту.
# sprint наполняется при `task import` (ord = порядок обхода канона: даты done-спринтов
# заморожены «в будущем» и для сортировки непригодны; status done/open — из статусов
# обычных задач канона). task.sprint_ext_id: у плановых задач — спринт канона, у
# внеплановых (canon_task_id IS NULL) — резолв по дате при `task add` / `task move`.
_V5 = """
BEGIN;
CREATE TABLE sprint (
  id         INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES project(id),
  ext_id     TEXT NOT NULL,
  name       TEXT,
  ord        INTEGER,
  status     TEXT,
  start_date TEXT,
  end_date   TEXT,
  UNIQUE(project_id, ext_id)
);
ALTER TABLE task ADD COLUMN sprint_ext_id TEXT;
COMMIT;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, _V1),
    (2, _V2),
    (3, _V3),
    (4, _V4),
    (5, _V5),
]
