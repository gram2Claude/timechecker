"""Интеграционный тест репликации SQLite → Supabase (E8, sync). Opt-in.

Запуск: ``TIMECHECKER_PG_TEST=1 uv run pytest tests/test_sync.py``. Изолированная схема.
"""

import json
import os
import pathlib

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TIMECHECKER_PG_TEST"),
    reason="Postgres integration: set TIMECHECKER_PG_TEST=1 (+ supabase_db_url)",
)


def _db_url():
    u = os.environ.get("TIMECHECKER_DB_URL")
    if u:
        return u
    p = pathlib.Path(os.path.expanduser("~/.wgp/secrets.json"))
    return json.loads(p.read_text(encoding="utf-8")).get("supabase_db_url")


def test_sync_local_first(tmp_path):
    import psycopg
    from psycopg.rows import dict_row

    from timechecker.storage import SqliteRepository
    from timechecker.storage.postgres_repository import PostgresRepository
    from timechecker.storage.sync import sync_to_postgres

    src = SqliteRepository.open(tmp_path / "src.db")
    emp = src.upsert_employee("Oleg", dev_branch="oleg")
    proj = src.upsert_project("p", identifier_prefix="TIME")
    t1 = src.upsert_task(proj, "TIME-1", title="x")
    src.insert_event(emp, "claude", "message", "2026-06-09T08:00:00Z", external_id="m1")
    src.insert_event(emp, "claude", "message", "2026-06-09T08:01:00Z")  # без external_id
    src.upsert_daily_summary(emp, "2026-06-09", active_minutes=5, tasks_count=1)
    src.insert_daily_idle(emp, "2026-06-09", "2026-06-09T10:00:00Z", "2026-06-09T10:50:00Z", 50)

    url = _db_url()
    raw = psycopg.connect(url, prepare_threshold=None, autocommit=True)
    raw.execute("DROP SCHEMA IF EXISTS timechecker_test CASCADE")
    raw.execute("CREATE SCHEMA timechecker_test")
    raw.close()
    conn = psycopg.connect(url, prepare_threshold=None, row_factory=dict_row,
                           options="-c search_path=timechecker_test")
    dst = PostgresRepository(conn)
    try:
        dst.apply_migrations()
        sync_to_postgres(src, dst, full=True)
        assert dst.stats()["events"] == 2
        assert dst.get_daily_summary(emp, "2026-06-09")["active_minutes"] == 5
        assert len(dst.daily_idles(emp, "2026-06-09")) == 1
        # id сохранён → FK консистентен
        ev = dst._fetchone("SELECT employee_id FROM activity_event WHERE external_id=?", ("m1",))
        assert ev["employee_id"] == emp

        # инкремент: backfill task_id на старое событие + новое событие
        src.insert_event(emp, "claude", "message", "2026-06-09T08:00:00Z",
                         external_id="m1", task_id=t1)
        src.insert_event(emp, "claude", "message", "2026-06-09T09:00:00Z", external_id="m3")
        sync_to_postgres(src, dst, full=False, lookback_days=3650)
        assert dst.stats()["events"] == 3  # новое событие долито
        row = dst._fetchone("SELECT task_id FROM activity_event WHERE external_id=?", ("m1",))
        assert row["task_id"] == t1  # backfill подхвачен окном ts

        # идемпотентность: повторный sync не плодит
        sync_to_postgres(src, dst, full=False, lookback_days=3650)
        assert dst.stats()["events"] == 3

        # пересчёт дня (меньше эпизодов) → delete-replace
        src.delete_daily_idle(emp, "2026-06-09")
        sync_to_postgres(src, dst, full=False, lookback_days=3650)
        assert len(dst.daily_idles(emp, "2026-06-09")) == 0  # устаревший эпизод убран

        # reset → чистый ресед
        sync_to_postgres(src, dst, reset=True)
        assert dst.stats()["events"] == 3
        # повторный --full идемпотентен даже для NULL-external (конфликт по id, не PK-дубль)
        sync_to_postgres(src, dst, full=True)
        assert dst.stats()["events"] == 3
    finally:
        conn.execute("DROP SCHEMA IF EXISTS timechecker_test CASCADE")
        conn.commit()
        src.close()
        dst.close()
