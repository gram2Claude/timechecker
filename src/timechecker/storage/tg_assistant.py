"""Роль `tg_assistant_bot` и гранты на схему `tg_assistant` (E11, TIME-69/70).

Роль создаётся ОТДЕЛЬНО от миграций (CLI `setup-bot-role`), а не строкой в pg_schema:
``_executescript`` режет скрипт по «;», и role-DDL/условные блоки этого не переживут (ревью
плана #2). Гранты — ПО-ТАБЛИЧНЫЕ (least-privilege, ревью #7): бот получает ровно то, что нужно
его реальным SQL-паттернам (см. tg_chat_assistant/src/cabinet/client.py), и не видит ни public,
ни других схем, ни DDL. Набор грантов АВТОРИТЕТНЫЙ: каждый запуск сперва снимает всё ранее
выданное роли, затем выдаёт текущий набор — ре-ран/ротация не оставляют лишних прав (ревью codex).

Точка обмена — Supabase проекта timechecker. Бот логинится через transaction-pooler (6543)
под именем ``<role>.<project_ref>`` (формат Supavisor). Роль создаётся через session-pooler
(5432): role/grant-DDL надёжнее на закреплённом backend, чем на transaction-pooler, где
последовательные стейтменты ложатся на случайные backend'ы (паттерн nexus_admin, ревью 3.1).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

ROLE = "tg_assistant_bot"
SCHEMA = "tg_assistant"

# По-табличные гранты под РЕАЛЬНЫЕ SQL-паттерны бота (client.py), минимально-функциональный набор.
# КЛЮЧЕВОЕ (выявлено приёмкой TIME-70 эмпирически): `INSERT … ON CONFLICT` требует SELECT на
# таблицу — арбитр конфликта читает существующую строку; это верно даже для DO NOTHING. Бот
# upsert'ит во ВСЕ таблицы (ON CONFLICT), поэтому SELECT нужен везде, иначе INSERT падает с
# 42501 «permission denied for table». Спека 12 §2 (digests I/U, journal I — без SELECT) была
# недостаточна; это исправление зафиксировано в RUNBOOK и ответе в issue #1.
#   bindings — fetch_bindings (SELECT) + push_binding (ON CONFLICT … WHERE bound_via): S/I/U
#   digests  — upsert_digest (ON CONFLICT DO UPDATE): S/I/U
#   topics   — replace_topic (ON CONFLICT DO UPDATE): S/I/U
#   journal  — add_journal (ON CONFLICT DO NOTHING, append-only): S/I (без UPDATE/DELETE)
# DELETE не выдаётся нигде (бот «заменяет страницу» per-row upsert'ом, а не delete-replace;
# отступление от спеки §2/ревью #7); UPDATE на journal нет (append-only). SELECT здесь —
# техническое требование ON CONFLICT, а не «бот читает чужое»: tg_assistant — схема самого бота.
TABLE_GRANTS: dict[str, str] = {
    "tg_chat_bindings": "SELECT, INSERT, UPDATE",
    "tg_digests": "SELECT, INSERT, UPDATE",
    "tg_topics": "SELECT, INSERT, UPDATE",
    "tg_journal": "SELECT, INSERT",
}
# tg_journal.id — bigserial: INSERT под ботом дёргает nextval, нужен USAGE,SELECT на sequence
# (без него INSERT падает на nextval — ревью #1).
JOURNAL_SEQUENCE = "tg_journal_id_seq"

# Пароль идёт в SQL-литерал (ALTER ROLE … PASSWORD '…' — DDL не параметризуется): строго
# ограничиваем алфавит и длину, чтобы исключить инъекцию/спецсимволы (паттерн nexus_admin).
_PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{24,}$")


def _session_dsn(admin_dsn: str) -> str:
    """Переключить DSN на session-pooler (6543 → 5432) для role/grant-DDL; иначе вернуть как есть.

    Реконструируем netloc из разобранных частей (а не строковой заменой ':6543'), чтобы случайное
    совпадение в пароле не сломало порт.
    """
    parts = urlsplit(admin_dsn)
    if parts.port != 6543:
        return admin_dsn
    host = parts.hostname or ""
    auth = ""
    if parts.username:
        auth = parts.username
        if parts.password:
            auth += f":{parts.password}"
        auth += "@"
    netloc = f"{auth}{host}:5432"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def build_bot_dsn(admin_dsn: str, password: str, *, role: str = ROLE) -> str:
    """Собрать DSN бота из admin-DSN: тот же хост/порт (transaction-pooler 6543) и query
    (``sslmode=require``), но username ``<role>.<project_ref>`` и пароль роли.

    Бот ходит через asyncpg — pooler-DSN ему подходит; для transaction-pooler бот ставит
    ``statement_cache_size=0`` на своей стороне (prepared statements несовместимы с pgbouncer).
    """
    parts = urlsplit(admin_dsn)
    admin_user = parts.username or "postgres"
    ref = admin_user.split(".", 1)[1] if "." in admin_user else None
    new_user = f"{role}.{ref}" if ref else role
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{new_user}:{password}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def setup_bot_role(admin_dsn: str, password: str) -> dict:
    """Идемпотентно создать роль бота и выдать АВТОРИТЕТНЫЙ по-табличный набор грантов на схему
    ``tg_assistant`` (предварительно сняв всё ранее выданное роли — least-privilege на ре-ране).

    ``admin_dsn`` — DSN владельца схемы (postgres). Роль создаётся, если её ещё нет; пароль
    выставляется всегда (ротация). Возвращает сводку прав. Верификацию см. в ``verify_bot_role``.
    """
    if not _PASSWORD_RE.match(password or ""):
        raise ValueError("пароль роли: минимум 24 символа [A-Za-z0-9] (идёт в SQL-литерал DDL)")

    import psycopg

    # autocommit=False → роль, пароль и гранты применяются ОДНОЙ транзакцией: если шаг упадёт
    # (напр. таблицы ещё нет), роль/пароль/частичные гранты не закоммитятся — никаких
    # полу-настроенных логинов (ревью). CREATE ROLE/ALTER ROLE/GRANT/REVOKE транзакционны.
    conn = psycopg.connect(_session_dsn(admin_dsn), prepare_threshold=None, autocommit=False)
    try:
        with conn.cursor() as cur:
            # порядок зависимостей (t11.1.2 после t11.1.1): без схемы/таблиц гранты бессмысленны —
            # явная ошибка лучше, чем половина грантов на несуществующие объекты
            cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                        (SCHEMA,))
            if cur.fetchone() is None:
                raise ValueError(
                    f"схемы {SCHEMA} нет — сначала примени миграцию v6 "
                    "(TIMECHECKER_BACKEND=postgres timechecker initdb)")
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                        (SCHEMA,))
            have = {r[0] for r in cur.fetchall()}
            missing = set(TABLE_GRANTS) - have
            if missing:
                raise ValueError(f"в схеме {SCHEMA} нет таблиц {sorted(missing)} — примени v6")

            # идемпотентное создание роли — без DO-блока (отдельные стейтменты, ревью #2)
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE ROLE {ROLE} LOGIN")
            # ROLE/SCHEMA/таблицы — константы кода (не пользовательский ввод); пароль валидирован
            cur.execute(f"ALTER ROLE {ROLE} LOGIN PASSWORD '{password}'")

            # АВТОРИТЕТНЫЙ набор: снять всё ранее выданное роли в обеих схемах (no-op, если
            # ничего не было), затем выдать ровно нужное — ре-ран/старая версия setup не оставят
            # лишних прав на public/tg_assistant/sequence/DDL (ревью codex, blocker)
            for stmt in (
                f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM {ROLE}",
                f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {SCHEMA} FROM {ROLE}",
                f"REVOKE ALL ON SCHEMA {SCHEMA} FROM {ROLE}",
                f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE}",
                f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {ROLE}",
                f"REVOKE ALL ON SCHEMA public FROM {ROLE}",
            ):
                cur.execute(stmt)

            # граница: USAGE только на tg_assistant; public и прочие схемы — без гранта вовсе
            cur.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {ROLE}")
            for table, privs in TABLE_GRANTS.items():
                cur.execute(f"GRANT {privs} ON {SCHEMA}.{table} TO {ROLE}")
            cur.execute(
                f"GRANT USAGE, SELECT ON SEQUENCE {SCHEMA}.{JOURNAL_SEQUENCE} TO {ROLE}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"role": ROLE, "schema": SCHEMA, "grants": dict(TABLE_GRANTS),
            "sequence": JOURNAL_SEQUENCE}


# Приёмочные пробы (TIME-70). ПОЗИТИВ — ТОЧНЫЕ SQL-паттерны бота (client.py): upsert'ы с
# ON CONFLICT. Каждая обкатывается в транзакции и откатывается (мусора в боевой схеме не остаётся).
# Кортеж: (label, sql, [(table, priv) — какие гранты доказывает]). Покрытие сверяется тестом с
# TABLE_GRANTS (каждый грант имеет позитивную пробу — ревью codex, major).
# ВНИМАНИЕ: проба journal — реальный INSERT (end-to-end проверка nextval, ревью #1). bigserial-
# sequence продвигается на каждом verify даже при rollback (sequences НЕ транзакционны) — это
# допустимо: id журнала не несёт смысла (дедуп по norm_text), bigint-пространство огромно.
_POSITIVE_PROBES = [
    ("read bindings (fetch_bindings)",
     f"SELECT chat_id, project_slug, chat_title, bound_via, active "
     f"FROM {SCHEMA}.tg_chat_bindings",
     [("tg_chat_bindings", "SELECT")]),
    ("upsert binding (push_binding)",
     f"INSERT INTO {SCHEMA}.tg_chat_bindings(chat_id, project_slug, chat_title, bound_via, "
     "updated_at) VALUES (-1, 'verify-probe', 't', 'bot', now()) "
     "ON CONFLICT (chat_id) DO UPDATE SET project_slug = excluded.project_slug, "
     "chat_title = excluded.chat_title, updated_at = now() "
     "WHERE tg_chat_bindings.bound_via = 'bot'",
     [("tg_chat_bindings", "INSERT"), ("tg_chat_bindings", "UPDATE")]),
    # upsert digest/topic — ON CONFLICT DO UPDATE доказывает SELECT+INSERT+UPDATE разом
    ("upsert digest (upsert_digest)",
     f"INSERT INTO {SCHEMA}.tg_digests(project_slug, date, content_md) "
     "VALUES ('verify-probe', '2026-01-01', 'c') "
     "ON CONFLICT (project_slug, date) DO UPDATE "
     "SET content_md = excluded.content_md, created_at = now()",
     [("tg_digests", "SELECT"), ("tg_digests", "INSERT"), ("tg_digests", "UPDATE")]),
    ("upsert topic (replace_topic)",
     f"INSERT INTO {SCHEMA}.tg_topics(project_slug, name, content_md) "
     "VALUES ('verify-probe', 't', 'c') "
     "ON CONFLICT (project_slug, name) DO UPDATE "
     "SET content_md = excluded.content_md, updated_at = now()",
     [("tg_topics", "SELECT"), ("tg_topics", "INSERT"), ("tg_topics", "UPDATE")]),
    # add_journal — ON CONFLICT DO NOTHING доказывает SELECT+INSERT (+nextval на sequence)
    ("insert journal + nextval seq (add_journal)",
     f"INSERT INTO {SCHEMA}.tg_journal(project_slug, kind, date, text, norm_text) "
     "VALUES ('verify-probe', 'decision', '2026-01-01', 'probe', 'probe') "
     "ON CONFLICT (project_slug, kind, norm_text) DO NOTHING",
     [("tg_journal", "SELECT"), ("tg_journal", "INSERT")]),
]
# НЕГАТИВ — что должно быть запрещено: засчитываем закрытым ТОЛЬКО код 42501
# (insufficient_privilege); иное (FK/constraint/undefined) = false-green и считается провалом.
# WHERE с несуществующим значением — чтобы даже при ошибочно выданном праве проба ничего не задела.
_NEGATIVE_PROBES = [
    ("read public.task (чужая схема)", "SELECT count(*) FROM public.task"),
    ("delete digests (нет DELETE-гранта)",
     f"DELETE FROM {SCHEMA}.tg_digests WHERE project_slug = '__no_such__'"),
    ("delete topics (нет DELETE-гранта)",
     f"DELETE FROM {SCHEMA}.tg_topics WHERE project_slug = '__no_such__'"),
    ("update journal (append-only, нет UPDATE)",
     f"UPDATE {SCHEMA}.tg_journal SET text = 'x' WHERE project_slug = '__no_such__'"),
    ("ddl create table (нет CREATE)", f"CREATE TABLE {SCHEMA}.probe_ddl (x int)"),
]


def verify_bot_role(bot_dsn: str) -> dict:
    """Приёмка границ роли РЕАЛЬНЫМ логином бота через pooler.

    SET ROLE на Supavisor рвёт соединение, поэтому проверяем как приложение — отдельным
    подключением под ролью бота. Возвращает ``{"ok": bool, "positive": [...], "negative": [...],
    "leaks": [...]}``; ``ok=False`` если хоть одна разрешённая операция упала или запрещённая
    не была отклонена кодом 42501.
    """
    import psycopg
    from psycopg import errors

    result: dict = {"ok": True, "positive": [], "negative": [], "leaks": []}
    conn = psycopg.connect(bot_dsn, prepare_threshold=None, autocommit=False)
    try:
        for label, sql, _covers in _POSITIVE_PROBES:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                result["positive"].append({"probe": label, "ok": True})
            except psycopg.Error as e:
                result["positive"].append({"probe": label, "ok": False, "code": e.sqlstate})
                result["ok"] = False
            finally:
                conn.rollback()  # ничего не коммитим — схема чистая
        for label, sql in _NEGATIVE_PROBES:
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                result["negative"].append({"probe": label, "denied": False})
                result["leaks"].append(label)
                result["ok"] = False
            except errors.InsufficientPrivilege:
                result["negative"].append({"probe": label, "denied": True, "code": "42501"})
            except psycopg.Error as e:
                # не privilege-отказ → false-green: считаем приёмку непройденной
                result["negative"].append({"probe": label, "denied": False, "code": e.sqlstate})
                result["ok"] = False
            finally:
                conn.rollback()
    finally:
        conn.close()
    return result
