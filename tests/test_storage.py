from timechecker.storage import (
    SqliteRepository,
    apply_migrations,
    current_version,
    init_db,
)


def test_migrations_idempotent(tmp_path):
    conn = init_db(tmp_path / "t.db")
    assert current_version(conn) == 1
    # повторное применение — без ошибок, версия не меняется
    assert apply_migrations(conn) == 1
    assert current_version(conn) == 1
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
    proj = r.upsert_project("timechecker", repo="gram2Claude/timechecker", plane_identifier="TIME")
    task = r.upsert_task(proj, "TIME-4", canon_task_id="t1.2.1", title="schema",
                         estimate_h=7.25, status="in_progress")
    run = r.start_ingest_run(emp, sources="claude,git")
    r.insert_event(emp, "claude", "message", "2026-06-09T08:00:00Z", project_id=proj,
                   task_id=task, external_id="m1", meta={"tokens": 10}, ingest_run_id=run)

    sess = r.upsert_claude_session(emp, "sess-1", project_id=proj, task_id=task,
                                   message_count=5, tokens_in=100, tokens_out=200)
    assert r.upsert_claude_session(emp, "sess-1", message_count=7) == sess  # идемпотентно

    com = r.upsert_git_commit(emp, "abc123", project_id=proj, branch="oleg",
                              subject="feat: x (TIME-4)")
    r.link_commit_task(com, task)
    r.link_commit_task(com, task)  # повтор связи — без ошибки

    r.insert_plane_transition(task, from_state="unstarted", to_state="started",
                              ts_utc="2026-06-09T07:00:00Z", external_id="tr-1")
    r.finish_ingest_run(run, "ok", counts={"events": 1})

    r.upsert_daily_summary(emp, "2026-06-09", active_minutes=120, tasks_count=1, claude_tokens=300)
    tt = r.upsert_daily_task_time(emp, "2026-06-09", task, active_minutes=120, est_h=7.25)
    assert r.upsert_daily_task_time(emp, "2026-06-09", task, active_minutes=130) == tt
    r.insert_daily_idle(emp, "2026-06-09", "2026-06-09T10:00:00Z", "2026-06-09T10:45:00Z", 45)
    r.close()


def test_prune_raw(tmp_path):
    r = _repo(tmp_path)
    emp = r.upsert_employee("Oleg")
    r.insert_event(emp, "claude", "message", "2026-01-01T00:00:00Z", external_id="old")
    r.insert_event(emp, "claude", "message", "2026-06-09T00:00:00Z", external_id="new")
    deleted = r.prune_raw("2026-05-10T00:00:00Z")  # ~30 дней назад от 09.06
    assert deleted >= 1
    evs = r.events_between(emp, "2026-01-01T00:00:00Z", "2026-12-31T00:00:00Z")
    assert len(evs) == 1  # осталось только свежее
    r.close()
