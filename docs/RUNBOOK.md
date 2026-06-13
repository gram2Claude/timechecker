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
   cd C:\Users\Oleg\dev\timechecker
   uv tool install --force ".[pg]"
   uv tool update-shell    # добавить ~/.local/bin в PATH (если ещё нет)
   ```
   С 2026-06-12 (v0.4.0) установка **НЕ editable**: правки дев-клона не попадают в прод до явной
   переустановки — класс рисков «горячего прода» (scheduled collect применяет недописанную
   миграцию) закрыт; после каждого мержа в master повторяй команду установки. Extra `[pg]`
   обязателен — без него `sync` падает на отсутствующем psycopg.
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
(фаза publish воркфлоу; попутно наполняет справочник спринтов) либо `timechecker task add` —
внеплановая задача автоматически попадает в **«Прочие работы»** текущего спринта (явный выбор —
`--sprint sX.Y`, коррекция — `task move`; в кабинете nexus_admin это узел внутри спринта).
После replan/изменения канона повторяй `task import` — он обновит справочник спринтов и
переведёт в плановые задачи, внесённые в канон задним числом. Дальше **ничего делать не нужно**:
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

## Интеграция TG Chat Assistant (схема `tg_assistant`, v6)

С миграции **v6** (спека 12, эпоха E11) на Supabase проекта появляется выделенная схема
`tg_assistant` — **точка обмена** между ботом-конспектором TG-чатов (проект `tg_chat_assistant`)
и личным кабинетом (`nexus_admin`). DDL — строго по разделу 2 контракта TGA-26. Схема живёт
**только в Postgres/Supabase** (в локальном SQLite миграция v6 — no-op: учёт времени её не
использует). 4 таблицы:

| Таблица | Направление | Назначение |
|---|---|---|
| `tg_chat_bindings` | кабинет ПИШЕТ, бот ЧИТАЕТ (poll ≤5 мин) | привязки чат→проект; `project_slug` **NULLABLE** (непривязанный чат = NULL → раздел «Чаты» показывает «неприсвоенные»; unbind = `project_slug=NULL`) |
| `tg_digests` | бот ПИШЕТ, кабинет ЧИТАЕТ | ежедневные дайджесты (md), PK `(project_slug, date)` |
| `tg_topics` | бот ПИШЕТ (полная замена страницы) | темы (md), PK `(project_slug, name)` |
| `tg_journal` | бот ПИШЕТ (append-only) | решения/пожелания; `id bigserial`, дедуп `UNIQUE(project_slug, kind, norm_text)` |

> **Отступление от черновика контракта** (согласовано, решение 6.2): `project_slug` в
> `tg_chat_bindings` — NULLABLE (контракт давал NOT NULL). NULLABLE — надмножество, запись бота
> (всегда со slug) не ломается; зато кабинет/бот могут показывать непривязанные чаты. Бот-сторона
> уведомлена в issue #1 (нужно уметь слать чаты с `project_slug=NULL`).

### Роль бота `tg_assistant_bot` (least-privilege)
Бот ходит в Supabase **отдельной** Postgres-ролью с доступом ТОЛЬКО к схеме `tg_assistant`.
Роль создаётся **не миграцией**, а идемпотентной командой (role-DDL не переживает разбиения
`_executescript` по «;»). По-табличные гранты (минимально необходимые по протоколу §3):

| Объект | Гранты | Почему |
|---|---|---|
| `tg_chat_bindings` | SELECT, INSERT, UPDATE | `fetch_bindings` (SELECT) + `push_binding` upsert |
| `tg_digests` | SELECT, INSERT, UPDATE | `upsert_digest` (ON CONFLICT DO UPDATE) |
| `tg_topics` | SELECT, INSERT, UPDATE | `replace_topic` (ON CONFLICT DO UPDATE) |
| `tg_journal` | SELECT, INSERT | `add_journal` — append-only (ON CONFLICT DO NOTHING) |
| `SEQUENCE tg_journal_id_seq` | USAGE, SELECT | иначе INSERT падает на `nextval` (`id bigserial`) |
| схема `tg_assistant` | USAGE | граница: `public` и прочие схемы — без гранта вовсе |

> **SELECT обязателен на ВСЕХ таблицах** — выявлено приёмкой TIME-70 эмпирически: `INSERT …
> ON CONFLICT` (и DO UPDATE, и DO NOTHING) требует SELECT на таблицу (арбитр конфликта читает
> строку), иначе бот-upsert падает с `42501 permission denied for table`. **Спека 12 §2 (digests
> I/U, journal I — без SELECT) была недостаточна** — исправлено. SELECT здесь техническое
> требование ON CONFLICT, не «бот читает чужое»: `tg_assistant` — схема самого бота.
>
> Гранты **авторитетные**: `setup-bot-role` сперва снимает всё ранее выданное роли (в
> `tg_assistant` и `public`), затем выдаёт ровно набор выше — ре-ран/ротация не оставляют лишних
> прав. **DELETE не выдаётся нигде** (бот «заменяет страницу» per-row upsert'ом, а не delete-
> replace — отступление от спеки §2/ревью #7); **UPDATE на `tg_journal` нет** (append-only). Если
> бот добавит чистку осиротевших тем через DELETE — дописать его в `TABLE_GRANTS` и повторить
> `setup-bot-role`.

Провижининг (одноразово, после мержа v6 в master и переустановки тула):
```powershell
# 1) применить миграцию v6 на Supabase (создаёт схему + 4 таблицы)
$env:TIMECHECKER_BACKEND="postgres"; timechecker initdb; Remove-Item Env:TIMECHECKER_BACKEND
# 2) создать роль + гранты + приёмка + выдать DSN (пароль НЕ в репозитории/SQL — только в env)
$env:TG_ASSISTANT_BOT_PASSWORD="<32+ символов [A-Za-z0-9]>"
timechecker setup-bot-role --print-dsn
Remove-Item Env:TG_ASSISTANT_BOT_PASSWORD
```
`setup-bot-role` идемпотентна (повторный запуск = ротация пароля + до-выдача грантов; роль и
гранты применяются одной транзакцией). С `--print-dsn` печатает в **stdout** DSN роли
(с паролем) — это и есть креды для бота; в логи DSN не попадает. Без `--no-verify` сразу гоняет
**приёмку реальным логином роли** через pooler: позитив (чтение/запись разрешённого, всё с
rollback — мусора в схеме не остаётся) + негатив (`public.task`, чтение/DELETE `tg_digests`, DDL
— ждём отказ `42501`).

### DSN для бота
Тот же Supabase-инстанс, что у timechecker; **transaction-pooler** (порт 6543, `sslmode=require`),
username `tg_assistant_bot.<project_ref>`:
```
postgresql://tg_assistant_bot.<project_ref>:<pwd>@aws-...pooler.supabase.com:6543/postgres?sslmode=require
```
Бот ходит через **asyncpg** — для transaction-pooler на стороне бота обязателен
`statement_cache_size=0` (prepared statements несовместимы с pgbouncer). Кладётся боту в `.env`
(`CABINET_DB_URL`). Service-role ключ Supabase боту НЕ выдаётся.

### Ретенция дайджестов
**Хранить всё, без чистки** (решение §6.3): объём ничтожен (1 md-строка/проект/день), Vault бота
и так хранит всё; точечную чистку можно добавить в `prune` позже при необходимости.

### Гранты кабинетной роли (`nexus_admin_app`)
Чтение/привязка из кабинета — отдельная роль, её гранты на `tg_assistant` живут на стороне
кабинета (`nexus_admin/scripts/db/setup-app-role.mjs`): один владелец грантов у каждой роли, без
второго источника истины.

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
   editable-инсталл (использовался до v0.4.0) делал рабочую копию «горячим продом» — scheduled
   collect применил бы миграцию неконтролируемо. (С v0.4.0 инсталл не-editable, и этот шаг
   нужен только на время самой переустановки тула.)
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
