# timechecker

Учёт **реального** рабочего времени сотрудников по output-сигналам (Claude Code / codex / git +
собственный реестр задач). Агент собирает метаданные активности на учётке сотрудника
(Windows-сервер, RDP), считает дневные метрики и формирует отчёт. **Тела сообщений Claude
не читаются и не хранятся — только метаданные.**

## Возможности
- **Сбор** output-сигналов: транскрипты Claude (таймстемпы/токены/тул-вызовы), сессии codex,
  git-коммиты (с `TASK-ID`). Хранилище — SQLite (нейтрально к серверной БД).
- **Собственный реестр задач** (`timechecker task import/add/start/done/list`): задачи и переходы
  статусов пишутся напрямую в БД; переходы дают «окна в работе» для атрибуции времени.
- **Метрики** за день: задачи и время на задачу, простои ≥30 мин, span, active/gap, effort (токены),
  фрагментация, adherence (vs план), гигиена процесса.
- **Отчёт**: markdown (аналитика — запросом к Supabase).
- **Эксплуатация**: расписание (Task Scheduler), диагностика, ретеншн.

## Установка
Требуется Python ≥3.12 и [uv](https://docs.astral.sh/uv/).
```
uv sync
```

## Команды
| Команда | Описание |
|---|---|
| `timechecker initdb` | создать/мигрировать БД (применить схему) |
| `timechecker collect` | собрать output-сигналы в БД (Claude/codex/hooks/git) |
| `timechecker task import/add/start/done/list` | собственный реестр задач (канон → БД, переходы статусов) |
| `timechecker metrics [--date YYYY-MM-DD]` | посчитать дневные метрики → `daily_*` |
| `timechecker report [--date]` | дневной отчёт (markdown) |
| `timechecker health` | диагностика (БД, последний сбор, расписание) |
| `timechecker prune [--days N]` | очистить сырьё старше N дней (ретеншн) |
| `timechecker deploy [--every 30] [--report-at 23:50]` | расписание collect + дневной отчёт |
| `timechecker migrate-db` | разовый полный перенос SQLite → Postgres/Supabase |
| `timechecker sync [--full] [--reset]` | инкрементальная репликация SQLite → Supabase (local-first) |
| `timechecker pricing-refresh` | обновить ставки токенов из LiteLLM → `~/.wgp/pricing.json` |
| `timechecker register-project --slug … --repo-dir … [--prefix ID]` | привязать проект к учёту (git + задачи) |
| `timechecker schedule` / `hook` / `projects` | примитивы планировщика / хуков / список проектов |

## Конфигурация (env `TIMECHECKER_*`)
Все опциональны (разумные дефолты). Ключевые:
- `TIMECHECKER_DB_PATH` — путь к SQLite (дефолт `~/.claude/timechecker/timechecker.db`)
- `TIMECHECKER_CLAUDE_PROJECTS_DIR` — каталог транскриптов (дефолт `~/.claude/projects`)
- `TIMECHECKER_MONITORED_REPO_DIR` / `_BRANCH` — рабочий git-репозиторий
- `TIMECHECKER_TASK_PREFIX` — префикс readable-ID задач для env-проекта (напр. `TIME`)
- `TIMECHECKER_WGP_SECRETS` — путь к секретам GitHub/Supabase (дефолт `~/.wgp/secrets.json`)
- `TIMECHECKER_RETENTION_DAYS` — срок хранения сырья (дефолт 30)
- **Стоимость токенов** — оценка `≈ API-эквивалент` (model-aware, по семействам opus/sonnet/haiku);
  ставки: дефолт + override `~/.wgp/pricing.json` (или `TIMECHECKER_PRICING`), обновляются
  `pricing-refresh` из LiteLLM (еженедельно через `deploy`). При подписке это бенчмарк, не счёт.
- **Local-first** (боевая модель): агент пишет в локальный SQLite, `timechecker sync` реплицирует в
  Supabase (DSN `supabase_db_url` в secrets). Прямой Postgres-backend — `TIMECHECKER_BACKEND=postgres`
  или `TIMECHECKER_DB_URL` (в local-first не используется). См. `docs/RUNBOOK.md`.

Полный список — в `.env.example`.

## Приватность и безопасность
- **Только метаданные.** Сохраняются таймстемпы, sessionId, счётчики токенов/тул-вызовов,
  ветка/проект, sha/subject коммита, переходы статусов. Тела сообщений Claude (`thinking`/`text`)
  **не читаются**. См. тест `tests/test_security.py`.
- БД и секреты — вне публичного репозитория (`.gitignore`). Подробности — `docs/RUNBOOK.md`.

## Архитектура
**Local-first:** `collectors/` (Claude/codex/hooks/git) + `tasks.py` (реестр задач) →
`storage/` (repository DAO, локальный SQLite) →
`metrics/` (движок) → `reporting/` (отчёт) → `ops/` (диагностика); поверх — `sync` (репликация SQLite →
Supabase). Repository-интерфейс (`BaseSqlRepository` + `SqliteRepository`/`PostgresRepository`) позволяет
менять СУБД без правок коллекторов/метрик/отчётов. Планирование/контроль — через `workflow_global_plan`.
