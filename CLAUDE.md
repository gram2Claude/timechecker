# CLAUDE.md — timechecker

Учёт реального рабочего времени сотрудников по **output-сигналам** (Claude Code / codex / git +
собственный реестр задач). **Метаданные-only:** тела сообщений Claude НЕ читаются и не хранятся.

## Команды (Python ≥3.12 + uv)
- `uv sync` — зависимости (dev: pytest, ruff, psycopg).
- `uv run pytest` — тесты (offline, SQLite). Postgres/sync-тесты: `TIMECHECKER_PG_TEST=1 uv run pytest`.
- `uv run ruff check .` — линт (line-length 100; select E,F,I,UP,B).
- CLI: `uv run timechecker <cmd>` — `initdb · collect · task · metrics · report · daily · health ·
  prune · deploy · sync · migrate-db · register-project · projects · schedule · hook`.

## Архитектура
`collectors/` (Claude·codex·hooks·git) + `tasks.py` (свой реестр задач: import/add/start/done/list)
→ `storage/` (repository DAO) → `metrics/` → `reporting/` → `ops/`.
- **Repository:** `BaseSqlRepository` (общая SQL на `?`) + тонкие `SqliteRepository`/`PostgresRepository`;
  фабрика `open_repository(cfg)` — СУБД меняется без правок коллекторов/метрик/отчётов.
- **Local-first (боевая модель):** `collect/metrics/report/health` → локальный SQLite (источник правды);
  `sync` инкрементально реплицирует SQLite → Supabase (копия-архив). Backend по умолчанию SQLite —
  **НЕ ставить** `TIMECHECKER_BACKEND`. Конфиг — env `TIMECHECKER_*` (`config.py`).
- Все `ts` — UTC (`…Z`); `work_date` — дата по МСК.

## Правила
- **Метаданные-only** — никогда не сохранять тела сообщений Claude (закреплено `tests/test_security.py`).
- **Идемпотентность** — `INSERT … ON CONFLICT`; `sync` сохраняет id (FK консистентны), конфликт по PK `id`.
- Перед коммитом — **ruff чистый + тесты зелёные**. Стиль/комментарии — как в окружающем коде (рус.).
- **Секреты** (GitHub/Supabase DSN) — только в `~/.wgp/secrets.json`, НЕ в репозитории.

## Процесс (merge-гейт)
Разработка в ветке **`oleg`** (= dev_branch); в `master` напрямую НЕ коммитить (branch protection).
Сигнал «готово к merge: TIME-X» → координатор гоняет `gate-merge.mjs` (конфликты + `uv sync`/`pytest`
→ merge → push → Done в свой реестр: `timechecker task done`). Планирование/задачи —
`workflow_global_plan` + собственный реестр (`timechecker task ...`, префикс `TIME`).

## Глубже
- Эксплуатация / боевой режим / backend / `sync` — `docs/RUNBOOK.md`.
- Обзор и команды — `README.md`. Модель данных/метрики — `work_directory/01_specs/`.

## Учёт работ: план и «Прочие работы» (timechecker)

Любая работа должна существовать в реестре задач timechecker — иначе её не видно ни в план-факте, ни в кабинете nexus_admin (урок 12.06.2026: пласт внеплановых работ amo_looker не попал в учёт).

- **Появился новый план/спека с объёмом работ** → задачи добавляются в канон глобального плана (`work_directory/00_global_plan/00_timechecker_plan.json`) через скилл /workflow_global_plan (режим replan), затем `timechecker task import`. Спека без задач в каноне — не план.
- **Работа вне плана** → ПЕРЕД началом: `timechecker task add --slug timechecker --title "…" --estimate-h N` (печатает ID, спринт прицепится по дате) → `timechecker task start <ID>` → по завершении `timechecker task done <ID>`. Задача появится в узле «Прочие работы» спринта в кабинете.
- ID в коммитах — только выданные реестром (`task add`/`task list`), руками не сочинять: коллизия TIME-N с реестром уже случалась (NEXADM-36/37).
