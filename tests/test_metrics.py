from timechecker.metrics import compute_day
from timechecker.metrics.engine import attribute, build_task_windows, msk_date_of, msk_day_window
from timechecker.storage import SqliteRepository


def test_msk_date_and_window():
    assert msk_date_of("2026-06-08T22:00:00Z") == "2026-06-09"  # 01:00 МСК след. дня
    assert msk_date_of("2026-06-08T20:00:00Z") == "2026-06-08"  # 23:00 МСК тот же день
    w0, w1 = msk_day_window("2026-06-09")
    assert w0 == "2026-06-08T21:00:00Z"
    assert w1 == "2026-06-09T20:59:59Z"


def test_task_windows_and_attribution():
    trs = [
        {"task_id": 1, "to_state": "In Progress", "ts_utc": "2026-06-09T05:00:00Z"},
        {"task_id": 1, "to_state": "Done", "ts_utc": "2026-06-09T09:00:00Z"},
    ]
    win = build_task_windows(trs)
    assert len(win) == 1
    from datetime import UTC, datetime
    assert attribute(datetime(2026, 6, 9, 6, tzinfo=UTC), win) == 1
    assert attribute(datetime(2026, 6, 9, 10, tzinfo=UTC), win) is None


def _setup(tmp_path):
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = r.upsert_employee("Oleg", dev_branch="oleg")
    proj = r.upsert_project("timechecker", plane_identifier="TIME")
    t1 = r.upsert_task(proj, "TIME-1", title="x", estimate_h=4.0)
    r.insert_plane_transition(t1, from_state="Backlog", to_state="In Progress",
                              ts_utc="2026-06-09T05:00:00Z", external_id="tr1")
    r.insert_plane_transition(t1, from_state="In Progress", to_state="Done",
                              ts_utc="2026-06-09T09:00:00Z", external_id="tr2")
    for ts in ("06:00:00", "06:05:00", "06:10:00", "07:00:00", "07:05:00"):
        full = f"2026-06-09T{ts}Z"
        r.insert_event(emp, "claude", "message", full, external_id=full,
                       meta={"tokens_in": 10, "tokens_out": 20})
    cid = r.upsert_git_commit(emp, "sha1", ts_utc="2026-06-09T06:30:00Z", subject="feat (TIME-1)")
    r.link_commit_task(cid, t1)
    return r, emp, t1


def test_compute_day_metrics(tmp_path):
    r, emp, t1 = _setup(tmp_path)
    res = compute_day(r, emp, "2026-06-09")
    assert res == {"tasks": 1, "idle_episodes": 1, "active_minutes": 15}

    summ = dict(r.conn.execute(
        "SELECT * FROM daily_summary WHERE employee_id=? AND work_date=?",
        (emp, "2026-06-09")).fetchone())
    assert summ["active_minutes"] == 15  # 5+5+5 (между событиями <30мин)
    assert summ["gap_minutes"] == 50
    assert summ["idle_ge30_count"] == 1
    assert summ["idle_ge30_minutes"] == 50
    assert summ["claude_messages"] == 5
    assert summ["claude_tokens"] == 150
    assert summ["commits"] == 1
    assert summ["hygiene_score"] == 1.0  # коммит с PLANE-ID
    assert summ["tasks_count"] == 1

    dtt = dict(r.conn.execute(
        "SELECT * FROM daily_task_time WHERE task_id=?", (t1,)).fetchone())
    assert dtt["active_minutes"] == 15
    assert dtt["claude_tokens"] == 150
    assert dtt["commits"] == 1
    assert dtt["est_h"] == 4.0

    idle = r.conn.execute("SELECT minutes FROM daily_idle WHERE employee_id=?", (emp,)).fetchall()
    assert len(idle) == 1 and idle[0]["minutes"] == 50

    # идемпотентность: повторный пересчёт не плодит daily_idle/daily_task_time
    compute_day(r, emp, "2026-06-09")
    assert r.conn.execute("SELECT COUNT(*) FROM daily_idle WHERE employee_id=?",
                          (emp,)).fetchone()[0] == 1
    r.close()


def test_compute_day_mixed_ts_order(tmp_path):
    # события с разным форматом ts (офсет +03:00 vs Z) — строковая сортировка спутала бы порядок
    # и дала отрицательное время; сортировка по распарсенному UTC даёт корректный результат.
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = r.upsert_employee("Oleg")
    # фактический UTC-порядок: 06:00 (через +03:00), 06:20, 06:25
    r.insert_event(emp, "claude", "message", "2026-06-09T09:00:00+03:00", external_id="a")
    r.insert_event(emp, "claude", "message", "2026-06-09T06:20:00Z", external_id="b")
    r.insert_event(emp, "claude", "message", "2026-06-09T06:25:00Z", external_id="c")
    res = compute_day(r, emp, "2026-06-09")
    assert res["active_minutes"] == 25  # 20 + 5; НЕ отрицательное
    assert res["idle_episodes"] == 0
    r.close()


def test_compute_day_empty(tmp_path):
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = r.upsert_employee("Oleg")
    res = compute_day(r, emp, "2026-06-09")
    assert res == {"tasks": 0, "idle_episodes": 0, "active_minutes": 0}
    r.close()
