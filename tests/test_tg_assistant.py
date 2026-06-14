"""Тесты роли tg_assistant_bot и схемы tg_assistant (E11, TIME-68/69/70).

Офлайн-юниты (DSN/гранты/валидация пароля) гоняются в обычном гейте. Интеграционная проба
схемы v6 — opt-in (как test_postgres): ``TIMECHECKER_PG_TEST=1`` + реальный Supabase.
"""

import json
import os
import pathlib

import pytest

from timechecker.storage.tg_assistant import (
    _NEGATIVE_PROBES,
    _POSITIVE_PROBES,
    JOURNAL_SEQUENCE,
    ROLE,
    SCHEMA,
    TABLE_GRANTS,
    _session_dsn,
    build_bot_dsn,
    setup_bot_role,
)

_ADMIN = ("postgresql://postgres.xwidbdfwvhqftwgbgaac:adminpw@"
          "aws-1-eu-central-1.pooler.supabase.com:6543/postgres?sslmode=require")
_PW = "A" * 32  # 32 символа [A-Za-z0-9] — проходит валидацию


def test_build_bot_dsn_pooler_username():
    """DSN бота: username <role>.<project_ref>, пароль роли, тот же хост/порт/query (sslmode)."""
    dsn = build_bot_dsn(_ADMIN, _PW)
    assert dsn == (f"postgresql://{ROLE}.xwidbdfwvhqftwgbgaac:{_PW}@"
                   "aws-1-eu-central-1.pooler.supabase.com:6543/postgres?sslmode=require")


def test_build_bot_dsn_direct_no_ref():
    """Прямой DSN (username без .ref) → роль без суффикса проекта."""
    direct = "postgresql://postgres:pw@db.example.supabase.co:5432/postgres?sslmode=require"
    dsn = build_bot_dsn(direct, _PW)
    assert f"{ROLE}:{_PW}@db.example.supabase.co:5432" in dsn
    assert f"{ROLE}." not in dsn  # нет суффикса <ref>


def test_session_dsn_switches_pooler_port():
    """Для role-DDL: transaction-pooler 6543 → session-pooler 5432; пароль/хост/query целы."""
    sess = _session_dsn(_ADMIN)
    assert ":5432/postgres" in sess and ":6543" not in sess
    assert "postgres.xwidbdfwvhqftwgbgaac:adminpw@" in sess
    assert sess.endswith("?sslmode=require")


def test_session_dsn_noop_when_not_6543(monkeypatch):
    monkeypatch.delenv("DB_SESSION_DBNAME", raising=False)
    direct = "postgresql://postgres:pw@db.example.supabase.co:5432/postgres"
    assert _session_dsn(direct) == direct


def test_session_dsn_selfhost_session_db(monkeypatch):
    """self-host (E12): порт не 6543, но DB_SESSION_DBNAME → переключить на session-логическую БД
    тем же портом (PgBouncer), а не хардкодом :5432. Хост/порт/пароль/query целы."""
    monkeypatch.setenv("DB_SESSION_DBNAME", "postgres_session")
    dsn = "postgresql://app:pw@185.221.22.174:6432/postgres?sslmode=verify-ca"
    sess = _session_dsn(dsn)
    assert "/postgres_session" in sess and ":6432" in sess and ":5432" not in sess
    assert "app:pw@185.221.22.174" in sess
    assert sess.endswith("?sslmode=verify-ca")


def test_session_dsn_selfhost_noop_without_optin(monkeypatch):
    """self-host без явного opt-in DB_SESSION_DBNAME → DSN не трогаем."""
    monkeypatch.delenv("DB_SESSION_DBNAME", raising=False)
    dsn = "postgresql://app:pw@185.221.22.174:6432/postgres?sslmode=verify-ca"
    assert _session_dsn(dsn) == dsn


def test_setup_bot_role_rejects_weak_password():
    """Слабый пароль отбивается ДО подключения (идёт в SQL-литерал DDL)."""
    for bad in ("short", "a" * 23, "A" * 24 + "!", "A" * 23 + " ", ""):
        with pytest.raises(ValueError):
            setup_bot_role(_ADMIN, bad)


def test_grants_are_minimal_functional():
    """Гранты — минимально-функциональный набор под реальные bot-upsert'ы (ON CONFLICT требует
    SELECT, выявлено приёмкой TIME-70). bindings/digests/topics S/I/U; journal S/I (append-only).
    DELETE не выдаётся нигде; UPDATE на journal нет."""
    assert TABLE_GRANTS == {
        "tg_chat_bindings": "SELECT, INSERT, UPDATE",
        "tg_digests": "SELECT, INSERT, UPDATE",
        "tg_topics": "SELECT, INSERT, UPDATE",
        "tg_journal": "SELECT, INSERT",
    }
    for t in TABLE_GRANTS:                       # DELETE не выдан ни на одной bot-таблице
        assert "DELETE" not in TABLE_GRANTS[t]
    assert "UPDATE" not in TABLE_GRANTS["tg_journal"]  # append-only
    assert SCHEMA == "tg_assistant" and ROLE == "tg_assistant_bot"
    assert JOURNAL_SEQUENCE == "tg_journal_id_seq"


def test_positive_probes_cover_every_grant():
    """Каждый выданный (таблица, право) имеет позитивную пробу приёмки (ревью codex, major):
    приёмка не может пройти зелёной, если бот-операция на деле сломана."""
    expected = {(table, priv.strip())
                for table, privs in TABLE_GRANTS.items()
                for priv in privs.split(",")}
    covered = {pair for _label, _sql, covers in _POSITIVE_PROBES for pair in covers}
    assert covered == expected


def test_negative_probes_check_boundary():
    """Негативные пробы покрывают границу: чужая схема (public), отсутствие SELECT/DELETE на
    bot-таблицах и запрет DDL."""
    sqls = " ".join(sql for _label, sql in _NEGATIVE_PROBES).lower()
    assert "public.task" in sqls            # чужая схема
    assert f"delete from {SCHEMA}.tg_digests".lower() in sqls
    assert f"delete from {SCHEMA}.tg_topics".lower() in sqls
    assert "create table" in sqls           # DDL


# ---- opt-in: реальный Supabase ----

pg = pytest.mark.skipif(
    not os.environ.get("TIMECHECKER_PG_TEST"),
    reason="Postgres integration: set TIMECHECKER_PG_TEST=1 (+ supabase_db_url)",
)


def _db_url():
    u = os.environ.get("TIMECHECKER_DB_URL")
    if u:
        return u
    p = pathlib.Path(os.path.expanduser("~/.wgp/secrets.json"))
    return json.loads(p.read_text(encoding="utf-8")).get("supabase_db_url")


@pg
def test_v6_tg_assistant_schema_created():
    """Миграция v6 создаёт схему tg_assistant + 4 таблицы по DDL §2; project_slug NULLABLE;
    у tg_journal есть IDENTITY-sequence (bigserial). Схема глобальная — НЕ дропаем её (боевая)."""
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
        assert repo.schema_version() == 7

        tables = {r["table_name"] for r in repo._query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            ("tg_assistant",))}
        assert {"tg_chat_bindings", "tg_digests", "tg_topics", "tg_journal"} <= tables

        # решение 6.2: project_slug в bindings — NULLABLE (отступление от контракта согласовано)
        nullable = repo._fetchone(
            "SELECT is_nullable FROM information_schema.columns WHERE table_schema = %s "
            "AND table_name = %s AND column_name = %s",
            ("tg_assistant", "tg_chat_bindings", "project_slug"))
        assert nullable["is_nullable"] == "YES"

        # tg_journal.id — bigserial → есть default nextval(...tg_journal_id_seq)
        col = repo._fetchone(
            "SELECT column_default FROM information_schema.columns WHERE table_schema = %s "
            "AND table_name = %s AND column_name = %s",
            ("tg_assistant", "tg_journal", "id"))
        assert col["column_default"] and "tg_journal_id_seq" in col["column_default"]

        # v7 (TIME-80): CHECK-лимиты длины контента навешены
        checks = {r["conname"] for r in repo._query(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_namespace n ON n.oid = con.connamespace "
            "WHERE n.nspname = %s AND con.contype = 'c'", ("tg_assistant",))}
        assert {"ck_tg_digests_content_md_len", "ck_tg_topics_content_md_len",
                "ck_tg_journal_text_len", "ck_tg_chat_bindings_title_len"} <= checks
    finally:
        conn.execute("DROP SCHEMA IF EXISTS timechecker_test CASCADE")
        conn.commit()
        repo.close()
