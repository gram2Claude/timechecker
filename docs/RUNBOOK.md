# RUNBOOK — timechecker (эксплуатация)

## Установка на учётку сотрудника (Windows-сервер)
1. Клонировать репозиторий, выполнить `uv sync`.
2. `timechecker initdb` — создать БД.
3. Настроить env (`.env` или системные переменные) — см. README.
4. `timechecker deploy` — зарегистрировать расписание Task Scheduler:
   - `timechecker-collect` — сбор каждые 30 мин;
   - `timechecker-report` — ежедневно в 23:50 (metrics + report).
5. (Опционально) подключить **хуки сессий** в `.claude/settings.json` сотрудника: на
   `SessionStart`/`SessionEnd`/`Stop` вызывать `timechecker hook <event>`. Даёт точные границы
   сессий; не конфликтует с хуками памяти (добавляется к ним).

## Боевой режим (production) — local-first + автозапуск
Модель: агент пишет в **локальный SQLite** (источник правды), команда `sync` реплицирует в **Supabase**
(копия-архив). Одноразовая настройка, после которой каждый новый проект подхватывается автоматически:

1. **Глобально установить агент** (на PATH, с драйвером Postgres для sync):
   ```powershell
   uv tool install --force --editable "C:\Users\Oleg\dev\timechecker" --with "psycopg[binary]"
   uv tool update-shell    # добавить ~/.local/bin в PATH (если ещё нет)
   ```
   Backend по умолчанию — **SQLite**; флаг `TIMECHECKER_BACKEND` НЕ ставим. DSN Supabase для sync —
   в `~/.wgp/secrets.json` (`supabase_db_url`), читается отдельно (`cfg.supabase_dsn()`).
2. **Схема + расписание:**
   ```powershell
   timechecker initdb                                       # локальный SQLite
   timechecker deploy --every 60 --sync-every 60 --report-at 23:50
   ```
   `deploy` создаёт задачи Task Scheduler с АБСОЛЮТНЫМ путём к `timechecker.exe`: `timechecker-collect`
   (→ SQLite), `timechecker-sync` (SQLite → Supabase), `timechecker-report` (`daily`).
3. **Baseline облака** (зеркало Supabase ← SQLite):
   ```powershell
   timechecker collect --full   # актуализировать SQLite из источников
   timechecker sync --reset      # TRUNCATE Supabase + чистый ресед (id-консистентно)
   ```
4. **Проверка:** `timechecker health` → `"backend": "sqlite"`, `"collect_task_scheduled": true`;
   счётчики SQLite == Supabase.

### Новый проект подключается автоматически
При `/workflow_create_env` шаг 2.7 спрашивает про учёт времени; на «Да» выполняется
`timechecker register-project --slug … --repo-dir … --plane-project-id … --plane-prefix …`.
Дальше **ничего делать не нужно**: ежечасный `collect` подхватит проект (git/Plane) в SQLite, Claude
собирается глобально, `sync` доносит в Supabase, дневной отчёт — в 23:50.

> Задачи планировщика по умолчанию — «только при входе пользователя». Для 24/7 или нескольких
> сотрудников переконфигурируй (`schtasks /Change /RU <user> /RP <pwd>` или per-user задачи).

## Проверка
- `timechecker health` — статус БД, последний `ingest_run` (ok/partial), счётчики, наличие задачи.
- Отчёт за день — `<db_dir>/reports/<date>.md` (аналитика по дням/задачам — запросом к Supabase).

## Безопасность
- БД (`timechecker.db`) и `reports/` — на учётке сотрудника; доступ ограничить Windows-ACL.
- Секреты Plane/GitHub — в `~/.wgp/secrets.json` (вне репозитория).
- **Метаданные-only**: тела сообщений Claude не хранятся (проверяется `tests/test_security.py`).
- Репозиторий пилота публичный (временно); до боевого запуска сделать приватным.

## Ретеншн
- Сырьё (`activity_event` и типизированные таблицы) хранится `TIMECHECKER_RETENTION_DAYS` (30) дней;
  чистится `timechecker prune` (можно отдельной ежедневной задачей Task Scheduler).
- Дневные агрегаты `daily_*` — бессрочно (компактны).

## Диагностика
- `ingest_run.status = partial` + `error` — один коллектор упал (Plane/git), остальные собрались.
- Plane 403 — в запросах должен быть `User-Agent` (обход Cloudflare 1010) — уже в коде.
- git: 0 коммитов — проверь `TIMECHECKER_MONITORED_REPO_DIR` и ветку (есть fallback на HEAD).

## Backend: local-first (SQLite → Supabase)
Repository-интерфейс (`storage/`) изолирует СУБД (SQLite/Postgres) одним контрактом. Боевая модель —
**local-first**: агент работает на **локальном SQLite** (источник правды), а `sync` реплицирует в **Supabase**.

- **Агент** (`collect/metrics/report/health`) — всегда SQLite (`timechecker.db`); флаг
  `TIMECHECKER_BACKEND` НЕ ставим. (Прямой Postgres-режим возможен через `TIMECHECKER_BACKEND=postgres`
  или `TIMECHECKER_DB_URL`, но в local-first не используется.)
- **`timechecker sync`** — инкрементальная репликация SQLite → Supabase (DSN из secrets
  `supabase_db_url`, читается `cfg.supabase_dsn()` независимо от backend). Нужна `psycopg` (в dev; прод —
  extra `.[pg]`). Supabase — **копия-архив** (superset): локальный `prune` не реплицируется.
  `sync --reset` = `TRUNCATE … RESTART IDENTITY CASCADE` + чистый ресед (id-консистентный baseline).
- Supabase DSN — **pooler-строка** (порт 6543, IPv4): `postgresql://postgres.<ref>:<pwd>@aws-...pooler.supabase.com:6543/postgres`.

> Схема Postgres — `storage/pg_schema.py` (зеркало SQLite; id = IDENTITY, sync сохраняет id → FK
> консистентны). Доступ к Supabase ограничь (RLS/роли); DSN с паролем — только в `~/.wgp/secrets.json`.
> `migrate-db` — для разового полного копирования; `sync` — для регулярной инкрементальной репликации.
