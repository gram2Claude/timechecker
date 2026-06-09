# timechecker

Учёт **реального** рабочего времени сотрудников по output-сигналам (Claude Code / git / Plane).
Агент собирает метаданные активности на учётке сотрудника (Windows-сервер, RDP), считает дневные
метрики и формирует отчёт. **Тела сообщений Claude не читаются и не хранятся — только метаданные.**

## Возможности
- **Сбор** output-сигналов: транскрипты Claude (таймстемпы/токены/тул-вызовы), git-коммиты
  (с `PLANE-ID`), переходы статусов Plane. Хранилище — SQLite (нейтрально к серверной БД).
- **Метрики** за день: задачи и время на задачу, простои ≥30 мин, span, active/gap, effort (токены),
  фрагментация, adherence (vs план), гигиена процесса.
- **Отчёт**: markdown + CSV; опционально комментарий в Plane.
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
| `timechecker collect` | собрать output-сигналы в БД (Claude/hooks/git/Plane) |
| `timechecker metrics [--date YYYY-MM-DD]` | посчитать дневные метрики → `daily_*` |
| `timechecker report [--date] [--plane-issue ID]` | дневной отчёт (md+CSV), опц. в Plane |
| `timechecker health` | диагностика (БД, последний сбор, расписание) |
| `timechecker prune [--days N]` | очистить сырьё старше N дней (ретеншн) |
| `timechecker deploy [--every 30] [--report-at 23:50]` | расписание collect + дневной отчёт |
| `timechecker schedule` / `hook` | примитивы планировщика / хуков сессий |

## Конфигурация (env `TIMECHECKER_*`)
Все опциональны (разумные дефолты). Ключевые:
- `TIMECHECKER_DB_PATH` — путь к SQLite (дефолт `~/.claude/timechecker/timechecker.db`)
- `TIMECHECKER_CLAUDE_PROJECTS_DIR` — каталог транскриптов (дефолт `~/.claude/projects`)
- `TIMECHECKER_MONITORED_REPO_DIR` / `_BRANCH` — рабочий git-репозиторий
- `TIMECHECKER_PLANE_PROJECT_ID` / `_PREFIX` — проект Plane для зеркала задач/переходов
- `TIMECHECKER_WGP_SECRETS` — путь к секретам Plane/GitHub (дефолт `~/.wgp/secrets.json`)
- `TIMECHECKER_RETENTION_DAYS` — срок хранения сырья (дефолт 30)

Полный список — в `.env.example`.

## Приватность и безопасность
- **Только метаданные.** Сохраняются таймстемпы, sessionId, счётчики токенов/тул-вызовов,
  ветка/проект, sha/subject коммита, переходы статусов. Тела сообщений Claude (`thinking`/`text`)
  **не читаются**. См. тест `tests/test_security.py`.
- БД и секреты — вне публичного репозитория (`.gitignore`). Подробности — `docs/RUNBOOK.md`.

## Архитектура
`collectors/` (Claude/hooks/git/Plane) → `storage/` (repository DAO + SQLite) → `metrics/` (движок) →
`reporting/` (отчёт) → `ops/` (диагностика). Repository-интерфейс изолирует SQLite от будущей
серверной БД. Планирование/контроль проекта — через `workflow_global_plan` (Plane + merge-гейт).
