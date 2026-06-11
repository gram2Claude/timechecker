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
`timechecker register-project --slug … --repo-dir … --branch … --prefix <IDENT>`.
Задачи проекта публикуются в собственный реестр: `timechecker task import --plan <канон>`
(фаза publish воркфлоу) либо `timechecker task add`. Дальше **ничего делать не нужно**:
ежечасный `collect` подхватит git-коммиты проекта в SQLite, Claude собирается глобально,
`sync` доносит в Supabase, дневной отчёт — в 23:50.

> Задачи планировщика по умолчанию — «только при входе пользователя». Для 24/7 или нескольких
> сотрудников переконфигурируй (`schtasks /Change /RU <user> /RP <pwd>` или per-user задачи).

## Проверка
- `timechecker health` — статус БД, последний `ingest_run` (ok/partial), счётчики, наличие задачи.
- Отчёт за день — `<db_dir>/reports/<date>.md` (аналитика по дням/задачам — запросом к Supabase).

## Безопасность
- БД (`timechecker.db`) и `reports/` — на учётке сотрудника; доступ ограничить Windows-ACL.
- Секреты GitHub/Supabase — в `~/.wgp/secrets.json` (вне репозитория).
- **Метаданные-only**: тела сообщений Claude не хранятся (проверяется `tests/test_security.py`).
- Репозиторий пилота публичный (временно); до боевого запуска сделать приватным.

## Ретеншн
- Сырьё (`activity_event`, `agent_session`, `git_commit`) хранится `TIMECHECKER_RETENTION_DAYS`
  (30) дней; чистится `timechecker prune` (можно отдельной ежедневной задачей Task Scheduler).
- **`task_transition` НЕ прунится** — первичные данные собственного реестра (окна атрибуции,
  локально невосстановимы); хранится бессрочно, как и дневные агрегаты `daily_*` (компактны).

## Диагностика
- `ingest_run.status = partial` + `error` — один коллектор упал (git/claude/codex), остальные собрались.
- Задачи не линкуются с коммитами — проверь, что канон импортирован (`timechecker task list --slug …`)
  и в коммитах есть `TASK-ID` (формат `PREFIX-N`, напр. `TIME-55`).
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

## Мультиагентный учёт (v3, схема `agent_session` + `daily_agent_usage`)

С миграции v3 timechecker учитывает расход **нескольких ИИ-агентов**: Claude Code и
**OpenAI Codex CLI** (`~/.codex/sessions/**/rollout-*.jsonl`, metadata-only). Расход
(сообщения/токены/кэш/стоимость) лежит в `daily_agent_usage` (разрез employee × date × task ×
source; `task_id NULL` = не атрибутировано), сессии всех агентов — в `agent_session`
(ключ `source + session_uid`). Время (active/idle) по-прежнему в `daily_summary`/`daily_task_time`
и считается в минутах.

- **Codex**: гранулярность «итог сессии» (одно событие на сессию, ts = старт). Ограничения:
  сессия через полночь целиком ложится на день старта; totals «дозревают» при пересчёте дня
  старта (`daily` пересчитывает сегодня+вчера); одна точка активности на сессию может «съесть»
  эпизод простоя, а долгая codex-работа без других событий остаётся простоем.
- **Семантика OpenAI** (отличается от Anthropic!): `input_tokens` ВКЛЮЧАЕТ `cached_input_tokens`;
  `reasoning_output_tokens` уже ВХОДИТ в `output_tokens` (не суммировать); cache-write не
  существует. Формула: `(input − cached)·r_in + cached·r_cache_read + output·r_out`.
- **Стоимость = «≈ API-эквивалент»** для ОБОИХ агентов: оценка по API-ставкам
  (LiteLLM-обновление еженедельно, `pricing-refresh`), при подписке это бенчмарк, НЕ реальный счёт.
- Конфиг: `TIMECHECKER_CODEX_SESSIONS_DIR` (дефолт `~/.codex/sessions`),
  `TIMECHECKER_CODEX_SINCE` (нижняя граница истории, дефолт 2026-06-01).
- `health`: ключ статистики `agent_sessions` (бывш. `claude_sessions`).

### Миграция на v3 (выполнено 2026-06-10) и откат
1. Отключить Task Scheduler (`schtasks /Change /TN timechecker-* /DISABLE`) ДО обновления кода:
   editable-инсталл делает рабочую копию «горячим продом» — scheduled collect применил бы
   миграцию неконтролируемо.
2. Бэкап `timechecker.db` → `.bak-v2-<дата>`. Supabase до локальной проверки НЕ синкать
   (остаётся v2 = второй бэкап агрегатов).
3. `initdb` (v3: пересоздание agent_session, бэкфилл daily_agent_usage, дроп claude_*-колонок) →
   проверить суммы против бэкапа → `collect --since <codex_since>` → `metrics --date …` по дням →
   `sync` (мигрирует Supabase при open) → включить Task Scheduler.
4. **Откат**: локально — восстановить `.bak-v2` + старый код (git revert). Облако: старый
   `sync --reset` по v3-схеме НЕ работает (TRUNCATE claude_session упадёт) → psql: DROP TABLE
   всех timechecker-таблиц + schema_migrations, затем старый код `sync --full` пересоздаст
   v2-схему и зальёт всё из локальной v2-БД (облако — реплика, источник правды локальный).

### Известные допущения v3 (из код-ревью)
- **Crash-window миграций**: скрипт миграции и запись версии в `schema_migrations` — разные
  транзакции; смерть процесса между ними клинит повторный запуск («table already exists») —
  лечится восстановлением из бэкапа. Окно миллисекундное; для v4+ рассмотреть stamp в одной
  транзакции со скриптом.
- **Дозревание codex-сессий** ограничено окном collect + 3 дня запаса по каталогам
  (`_PATH_MARGIN_DAYS`): сессия длиннее перестаёт пересобираться, недосчёт замораживается.
- **`tasks_count`** с v3 включает задачи, у которых есть только расход агента (без минут
  и коммитов) — в сравнениях с историей метрика выше старой семантики «задачи со временем».
- **`migrate-db`** — только в пустую целевую схему (см. докстринг `storage/migrate.py`).
