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

## Проверка
- `timechecker health` — статус БД, последний `ingest_run` (ok/partial), счётчики, наличие задачи.
- Отчёт за день — `<db_dir>/reports/<date>.md` и `.csv`.

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

## Backend БД: SQLite (по умолчанию) ↔ Postgres/Supabase
Repository-интерфейс (`storage/`) изолирует СУБД: те же коллекторы/метрики/отчёты работают на любом
backend. Выбор — через `open_repository(cfg)`.

- **По умолчанию — SQLite** (`timechecker.db`). Наличие `supabase_db_url` в secrets backend НЕ меняет.
- **Postgres (Supabase) — ЯВНЫЙ opt-in**, одним из способов:
  - `TIMECHECKER_BACKEND=postgres` + `supabase_db_url` в `~/.wgp/secrets.json` (рекомендуется), либо
  - `TIMECHECKER_DB_URL=postgresql://...` (полный DSN, приоритетнее).
  Нужна зависимость `psycopg` (входит в dev; для прод-установки — extra `pip install .[pg]`).
  Для Supabase используй **pooler-строку** (порт 6543, IPv4): `postgresql://postgres.<ref>:<pwd>@aws-...pooler.supabase.com:6543/postgres`.

### Перенос данных SQLite → Supabase
1. Убедись, что Postgres включён (см. выше) и `psycopg` установлен.
2. `timechecker migrate-db` — копирует все таблицы (id сохраняются, идемпотентно).
3. `timechecker health` → `backend: postgres` + статистика из Supabase.
4. Для постоянной работы на Postgres задай `TIMECHECKER_BACKEND=postgres` в окружении
   запланированных задач (`deploy`) — тогда сбор/отчёты идут в Supabase.

> Схема Postgres — `storage/pg_schema.py` (зеркало SQLite; id = IDENTITY). Доступ к БД ограничь
> на стороне Supabase (RLS/роли); DSN с паролем — только в `~/.wgp/secrets.json`, не в репозитории.
