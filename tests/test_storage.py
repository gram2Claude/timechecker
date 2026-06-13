from timechecker.storage import (
    SqliteRepository,
    apply_migrations,
    current_version,
    init_db,
)


def test_quote_ident():
    """SQL-идентификаторы валидируются и квотируются; мусор → ValueError (анти-инъекция)."""
    import pytest

    from timechecker.storage.base import quote_ident
    assert quote_ident("daily_agent_usage") == '"daily_agent_usage"'
    assert quote_ident("tokens_in") == '"tokens_in"'
    for bad in ('a"; DROP TABLE x;--', "a b", "1col", "col-name", ""):
        with pytest.raises(ValueError):
            quote_ident(bad)


def test_migrations_idempotent(tmp_path):
    conn = init_db(tmp_path / "t.db")
    assert current_version(conn) == 6
    # повторное применение — без ошибок, версия не меняется
    assert apply_migrations(conn) == 6
    assert current_version(conn) == 6
    conn.close()


def test_migration_upgrade_with_data(tmp_path):
    """Апгрейд v2→v4 с данными: пересоздание agent_session (v3), бэкфилл daily_agent_usage (v3),
    переименования plane_* → нейтральные (v4)."""
    from datetime import UTC, datetime

    from timechecker.storage.db import connect
    from timechecker.storage.schema import MIGRATIONS

    conn = connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE schema_migrations "
                 "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    now = datetime.now(UTC).isoformat()
    for version, sql in MIGRATIONS[:2]:  # только v1+v2 — состояние «старого прода»
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations VALUES(?, ?)", (version, now))
    conn.commit()
    conn.execute("INSERT INTO employee(id, windows_username, created_at) VALUES(1, 'Oleg', ?)",
                 (now,))
    conn.execute("INSERT INTO project(id, slug, created_at) VALUES(1, 'p', ?)", (now,))
    conn.execute("INSERT INTO task(id, project_id, plane_identifier) VALUES(7, 1, 'TIME-7')")
    conn.execute(
        "INSERT INTO claude_session(id, employee_id, session_uid, message_count, tokens_in, "
        "tokens_out, cache_read, cache_creation, model) VALUES(3, 1, 's1', 5, 100, 200, 9, 4, "
        "'claude-opus-4-8')")
    # день с задачной строкой и остатком: summary 120 сообщ./50000 ток., задача 100/40000
    conn.execute(
        "INSERT INTO daily_summary(employee_id, work_date, claude_messages, claude_tokens, "
        "claude_cache_read, claude_cache_creation, claude_cost_usd, computed_at) "
        "VALUES(1, '2026-06-09', 120, 50000, 1000, 200, 5.0, ?)", (now,))
    conn.execute(
        "INSERT INTO daily_task_time(employee_id, work_date, task_id, active_minutes, "
        "claude_messages, claude_tokens, claude_cache_read, claude_cache_creation, "
        "claude_cost_usd, computed_at) VALUES(1, '2026-06-09', 7, 60, 100, 40000, 800, 150, "
        "4.0, ?)", (now,))
    # день только с summary (без задачных строк) — остаток не должен потеряться
    conn.execute(
        "INSERT INTO daily_summary(employee_id, work_date, claude_messages, claude_tokens, "
        "claude_cost_usd, computed_at) VALUES(1, '2026-06-08', 10, 3000, 0.5, ?)", (now,))
    conn.commit()

    assert apply_migrations(conn) == 6  # v5 (sprint) + v6 (tg_assistant no-op в SQLite)

    # v4: переименования вступили в силу (без потери данных)
    t = conn.execute("SELECT identifier FROM task WHERE id=7").fetchone()
    assert t["identifier"] == "TIME-7"
    cols_p = {r["name"] for r in conn.execute("PRAGMA table_info(project)").fetchall()}
    assert "identifier_prefix" in cols_p and "plane_project_id" not in cols_p

    s = conn.execute("SELECT * FROM agent_session WHERE session_uid='s1'").fetchone()
    assert s["id"] == 3 and s["source"] == "claude" and s["tokens_out"] == 200
    rows = {(r["work_date"], r["task_id"]): dict(r) for r in conn.execute(
        "SELECT * FROM daily_agent_usage").fetchall()}
    task_row = rows[("2026-06-09", 7)]
    assert task_row["messages"] == 100 and task_row["tokens"] == 40000
    rest = rows[("2026-06-09", None)]  # остаток: 20 сообщ., 10000 ток., $1
    assert rest["messages"] == 20 and rest["tokens"] == 10000
    assert abs(rest["cost_usd"] - 1.0) < 1e-9
    only_summary = rows[("2026-06-08", None)]
    assert only_summary["messages"] == 10 and only_summary["tokens"] == 3000
    # переехавшие колонки удалены, время осталось
    cols_s = {r["name"] for r in conn.execute("PRAGMA table_info(daily_summary)").fetchall()}
    cols_t = {r["name"] for r in conn.execute("PRAGMA table_info(daily_task_time)").fetchall()}
    assert "claude_tokens" not in cols_s and "claude_cost_usd" not in cols_s
    assert "models" in cols_s and "active_minutes" in cols_s
    assert "claude_tokens" not in cols_t and "active_minutes" in cols_t
    tt = conn.execute("SELECT active_minutes FROM daily_task_time").fetchone()
    assert tt["active_minutes"] == 60
    conn.close()


def _repo(tmp_path):
    return SqliteRepository.open(tmp_path / "t.db")


def test_upsert_employee_idempotent(tmp_path):
    r = _repo(tmp_path)
    id1 = r.upsert_employee("Oleg", dev_branch="oleg")
    id2 = r.upsert_employee("Oleg", display_name="Oleg D.")
    assert id1 == id2  # тот же сотрудник, без дубля
    emp = r.get_employee("Oleg")
    assert emp["dev_branch"] == "oleg"
    assert emp["display_name"] == "Oleg D."
    r.close()


def test_event_idempotent_by_external_id(tmp_path):
    r = _repo(tmp_path)
    emp = r.upsert_employee("Oleg")
    e1 = r.insert_event(emp, "claude", "message", "2026-06-09T08:00:00Z",
                        external_id="msg-1", meta={"tokens": 10})
    e2 = r.insert_event(emp, "claude", "message", "2026-06-09T08:00:00Z", external_id="msg-1")
    assert e1 == e2  # повтор не плодит дубли
    evs = r.events_between(emp, "2026-06-09T00:00:00Z", "2026-06-09T23:59:59Z")
    assert len(evs) == 1
    r.close()


def test_full_chain(tmp_path):
    r = _repo(tmp_path)
    emp = r.upsert_employee("Oleg", dev_branch="oleg")
    proj = r.upsert_project("timechecker", repo="gram2Claude/timechecker", identifier_prefix="TIME")
    task = r.upsert_task(proj, "TIME-4", canon_task_id="t1.2.1", title="schema",
                         estimate_h=7.25, status="in_progress")
    run = r.start_ingest_run(emp, sources="claude,git")
    r.insert_event(emp, "claude", "message", "2026-06-09T08:00:00Z", project_id=proj,
                   task_id=task, external_id="m1", meta={"tokens": 10}, ingest_run_id=run)

    sess = r.upsert_agent_session(emp, "claude", "sess-1", project_id=proj, task_id=task,
                                  message_count=5, tokens_in=100, tokens_out=200)
    assert r.upsert_agent_session(emp, "claude", "sess-1", message_count=7) == sess  # идемпотент.
    # тот же uid от другого агента — ОТДЕЛЬНАЯ сессия (ключ source+session_uid)
    assert r.upsert_agent_session(emp, "codex", "sess-1", message_count=1) != sess

    com = r.upsert_git_commit(emp, "abc123", project_id=proj, branch="oleg",
                              subject="feat: x (TIME-4)")
    r.link_commit_task(com, task)
    r.link_commit_task(com, task)  # повтор связи — без ошибки

    r.insert_task_transition(task, from_state="unstarted", to_state="started",
                             ts_utc="2026-06-09T07:00:00Z", external_id="tr-1")
    r.finish_ingest_run(run, "ok", counts={"events": 1})

    r.upsert_daily_summary(emp, "2026-06-09", active_minutes=120, tasks_count=1)
    tt = r.upsert_daily_task_time(emp, "2026-06-09", task, active_minutes=120, est_h=7.25)
    assert r.upsert_daily_task_time(emp, "2026-06-09", task, active_minutes=130) == tt
    r.insert_daily_idle(emp, "2026-06-09", "2026-06-09T10:00:00Z", "2026-06-09T10:45:00Z", 45)

    # daily_agent_usage: delete-replace по дню
    r.insert_daily_agent_usage(emp, "2026-06-09", task, "claude",
                               messages=5, tokens=300, cost_usd=0.1)
    r.insert_daily_agent_usage(emp, "2026-06-09", None, "codex",
                               messages=2, tokens=900, cache_read=100, cost_usd=0.2)
    rows = r.daily_agent_usage(emp, "2026-06-09")
    assert len(rows) == 2
    codex_row = next(x for x in rows if x["source"] == "codex")
    assert codex_row["task_id"] is None and codex_row["tokens"] == 900
    claude_row = next(x for x in rows if x["source"] == "claude")
    assert claude_row["identifier"] == "TIME-4"  # JOIN task для отчёта
    r.delete_daily_agent_usage(emp, "2026-06-09")
    assert r.daily_agent_usage(emp, "2026-06-09") == []
    r.close()


def test_prune_raw(tmp_path):
    r = _repo(tmp_path)
    emp = r.upsert_employee("Oleg")
    r.insert_event(emp, "claude", "message", "2026-01-01T00:00:00Z", external_id="old")
    r.insert_event(emp, "claude", "message", "2026-06-09T00:00:00Z", external_id="new")
    # переходы статусов — первичные данные реестра (E9): prune их НЕ трогает,
    # даже очень старые — иначе окна атрибуции локально невосстановимы
    proj = r.upsert_project("p", identifier_prefix="PR")
    task = r.upsert_task(proj, "PR-1", title="x")
    r.insert_task_transition(task, from_state="Todo", to_state="In Progress",
                             ts_utc="2026-01-01T00:00:00Z", external_id="tr-old")
    deleted = r.prune_raw("2026-05-10T00:00:00Z")  # ~30 дней назад от 09.06
    assert deleted >= 1
    evs = r.events_between(emp, "2026-01-01T00:00:00Z", "2026-12-31T00:00:00Z")
    assert len(evs) == 1  # осталось только свежее
    assert len(r.all_task_transitions()) == 1  # переход старше cutoff пережил prune
    r.close()
