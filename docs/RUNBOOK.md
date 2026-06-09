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

## Перенос на серверную БД (будущая эпоха)
Repository-интерфейс (`storage/repository.py`) изолирует выбор СУБД: серверная реализация
добавляется отдельным классом без изменения коллекторов/метрик/отчётов.
