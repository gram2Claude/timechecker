"""Интеграционный тест Postgres-backend (E6, TIME-39). Opt-in: требует реального Supabase.

Запуск: ``TIMECHECKER_PG_TEST=1 uv run pytest tests/test_postgres.py`` (db_url из
``~/.wgp/secrets.json`` или ``TIMECHECKER_DB_URL``). Работает в изолированной схеме
``timechecker_test`` и удаляет её после. В обычном прогоне гейта — пропускается.
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


def test_postgres_repository_roundtrip():
    import psycopg
    from psycopg.rows import dict_row

    from timechecker.storage.postgres_repository import PostgresRepository

    url = _db_url()
    assert url, "нет supabase_db_url"
    raw = psycopg.connect(url, prepare_threshold=None, autocommit=True)
    raw.execute("DROP SCHEMA IF EXISTS timechecker_test CASCADE")
    raw.execute("CREATE SCHEMA timechecker_test")
    raw.close()

    conn = psycopg.connect(url, prepare_threshold=None, row_factory=dict_row,
                           options="-c search_path=timechecker_test")
    repo = PostgresRepository(conn)
    try:
        repo.apply_migrations()
        assert repo.schema_version() == 5
        assert repo.apply_migrations() == 5  # идемпотентно

        emp = repo.upsert_employee("Oleg", dev_branch="oleg")
        assert repo.upsert_employee("Oleg", display_name="O") == emp  # без дубля
        proj = repo.upsert_project("p", identifier_prefix="TIME")
        repo.upsert_task(proj, "TIME-1", title="x", estimate_h=4.0)

        e1 = repo.insert_event(emp, "claude", "message", "2026-06-09T08:00:00Z",
                               external_id="m1", meta={"tokens_in": 1})
        assert repo.insert_event(emp, "claude", "message", "2026-06-09T08:00:00Z",
                                 external_id="m1") == e1  # идемпотентно по external_id
        run = repo.start_ingest_run(emp, sources="claude")
        repo.finish_ingest_run(run, "ok", counts={"events": 1})
        assert repo.stats()["events"] == 1
        assert repo.last_ingest_run()["status"] == "ok"

        repo.upsert_daily_summary(emp, "2026-06-09", active_minutes=5, tasks_count=1)
        assert repo.get_daily_summary(emp, "2026-06-09")["active_minutes"] == 5
        assert len(repo.events_between(emp, "2026-06-09T00:00:00Z", "2026-06-09T23:59:59Z")) == 1
    finally:
        conn.execute("DROP SCHEMA IF EXISTS timechecker_test CASCADE")
        conn.commit()
        repo.close()


def test_migrate_sqlite_to_postgres(tmp_path):
    import psycopg
    from psycopg.rows import dict_row

    from timechecker.storage import SqliteRepository
    from timechecker.storage.migrate import migrate_sqlite_to_postgres
    from timechecker.storage.postgres_repository import PostgresRepository

    src = SqliteRepository.open(tmp_path / "src.db")
    emp = src.upsert_employee("Oleg", dev_branch="oleg")
    proj = src.upsert_project("p", identifier_prefix="TIME")
    src.upsert_task(proj, "TIME-1", title="x", estimate_h=4.0)
    src.insert_event(emp, "claude", "message", "2026-06-09T08:00:00Z", external_id="m1",
                     meta={"tokens_in": 3})
    src.insert_event(emp, "git", "commit", "2026-06-09T09:00:00Z", external_id="sha1")

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
        counts = migrate_sqlite_to_postgres(src, dst)
        assert counts["employee"] == 1 and counts["activity_event"] == 2 and counts["task"] == 1
        assert dst.stats()["events"] == 2
        assert dst.get_employee("Oleg")["dev_branch"] == "oleg"
        # секвенс выровнен — новый insert не конфликтует по id
        new_emp = dst.upsert_employee("Petr")
        assert new_emp > emp
    finally:
        conn.execute("DROP SCHEMA IF EXISTS timechecker_test CASCADE")
        conn.commit()
        src.close()
        dst.close()
