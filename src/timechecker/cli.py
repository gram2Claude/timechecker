"""CLI timechecker: entrypoint + подкоманды (initdb / hook / collect / schedule / report)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .collectors.hooks import HOOK_EVENTS, append_hook_event
from .collectors.orchestrator import collect_all
from .collectors.scheduler import register_daily_task, register_task, register_weekly_task
from .config import Config
from .logging_setup import get_logger, setup_logging
from .metrics import compute_day
from .ops import health_check
from .registry import load_projects, register_project, registry_path
from .reporting import build_daily_report
from .storage import SqliteRepository, open_repository

log = get_logger("timechecker.cli")


def _cmd_initdb(args: argparse.Namespace, cfg: Config) -> int:
    repo = open_repository(cfg)
    try:
        log.info("initdb: схема применена (версия %s), backend=%s",
                 repo.schema_version(), "postgres" if cfg.db_url else "sqlite")
    finally:
        repo.close()
    return 0


def _cmd_hook(args: argparse.Namespace, cfg: Config) -> int:
    spool = cfg.db_path.parent / "hooks.jsonl"
    append_hook_event(spool, args.event, session_uid=args.session, project_key=args.project)
    log.info("hook: %s записан → %s", args.event, spool)
    return 0


def _cmd_collect(args: argparse.Namespace, cfg: Config) -> int:
    counts = collect_all(cfg, since=getattr(args, "since", None), full=getattr(args, "full", False))
    log.info("collect: %s → %s", counts, cfg.db_path)
    return 0


def _cmd_metrics(args: argparse.Namespace, cfg: Config) -> int:
    date = args.date or (datetime.now(UTC) + timedelta(hours=3)).date().isoformat()
    repo = open_repository(cfg)
    try:
        emp = repo.upsert_employee(cfg.employee_username, dev_branch=cfg.dev_branch)
        res = compute_day(repo, emp, date)
        log.info("metrics %s: %s → %s", date, res, cfg.db_path)
    finally:
        repo.close()
    return 0


def _cmd_schedule(args: argparse.Namespace, cfg: Config) -> int:
    rc = register_task(args.name, args.command, args.every)
    log.info("schedule: '%s' каждые %s мин (schtasks rc=%s)", args.name, args.every, rc)
    return 0 if rc == 0 else 1


def _cmd_report(args: argparse.Namespace, cfg: Config) -> int:
    date = args.date or (datetime.now(UTC) + timedelta(hours=3)).date().isoformat()
    repo = open_repository(cfg)
    try:
        emp = repo.upsert_employee(cfg.employee_username, dev_branch=cfg.dev_branch)
        rep = build_daily_report(repo, emp, date)
        out_dir = cfg.db_path.parent / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"{date}.md"
        md_path.write_text(rep["markdown"], encoding="utf-8")
        log.info("report %s → %s", date, md_path)
    finally:
        repo.close()
    return 0


def _cmd_health(args: argparse.Namespace, cfg: Config) -> int:
    repo = open_repository(cfg)
    try:
        log.info("health: %s", json.dumps(health_check(repo, cfg), ensure_ascii=False))
    finally:
        repo.close()
    return 0


def _cmd_prune(args: argparse.Namespace, cfg: Config) -> int:
    days = args.days if args.days is not None else cfg.retention_days
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = open_repository(cfg)
    try:
        deleted = repo.prune_raw(cutoff)
        log.info("prune: удалено %s сырых записей старше %s (%s дн)", deleted, cutoff, days)
    finally:
        repo.close()
    return 0


def _cmd_daily(args: argparse.Namespace, cfg: Config) -> int:
    """Дневной прогон: метрики за вчера+сегодня + отчёт за сегодня (для планировщика).

    Вчера пересчитывается, потому что totals codex-сессий «дозревают» на дне старта сессии,
    а поздние Claude-сообщения через полночь меняют вчерашние агрегаты.
    """
    date = getattr(args, "date", None)
    rc0 = 0
    if date is None:
        today = (datetime.now(UTC) + timedelta(hours=3)).date()
        yns = argparse.Namespace(date=(today - timedelta(days=1)).isoformat())
        rc0 = _cmd_metrics(yns, cfg) or _cmd_report(yns, cfg)  # вчерашний md тоже актуализируем
        date = today.isoformat()
    ns = argparse.Namespace(date=date)
    rc1 = _cmd_metrics(ns, cfg)
    rc2 = _cmd_report(ns, cfg)
    return rc0 or rc1 or rc2


def _cmd_deploy(args: argparse.Namespace, cfg: Config) -> int:
    exe = shutil.which("timechecker") or "timechecker"
    rc1 = register_task("timechecker-collect", f'"{exe}" collect', args.every)
    rc2 = register_daily_task("timechecker-report", f'"{exe}" daily', args.report_at)
    rc3 = 0
    has_cloud = bool(cfg.supabase_dsn())
    if has_cloud:  # local-first: collect/metrics/report → SQLite, sync → Supabase
        rc3 = register_task("timechecker-sync", f'"{exe}" sync', args.sync_every)
    rc4 = register_weekly_task("timechecker-pricing-refresh", f'"{exe}" pricing-refresh')
    log.info("deploy rc: collect=%s daily=%s sync=%s pricing=%s | exe=%s", rc1, rc2, rc3, rc4, exe)
    log.info("deploy: SQLite-агент + sync→Supabase + pricing weekly. Проверь 'health'.")
    return 0 if rc1 == 0 and rc2 == 0 and rc3 == 0 and rc4 == 0 else 1


def _cmd_pricing_refresh(args: argparse.Namespace, cfg: Config) -> int:
    from .pricing import refresh_rates
    try:
        new = refresh_rates()
    except Exception as e:
        log.error("pricing-refresh: не удалось (%s); текущие ставки сохранены", e)
        return 1
    log.info("pricing-refresh: ставки обновлены из LiteLLM → %s", new)
    return 0


def _cmd_task(args: argparse.Namespace, cfg: Config) -> int:
    """Собственный реестр задач (E9): запись задач/переходов без Plane."""
    from .tasks import (
        DONE_STATE,
        STARTED_STATE,
        add_task,
        backfill_sprints,
        import_canon,
        list_tasks,
        move_task,
        transition,
    )
    repo = open_repository(cfg)
    try:
        if args.task_command == "import":
            res = import_canon(repo, Path(args.plan), slug=args.slug)
            for w in res.get("warnings", []):
                log.warning("task import: %s", w)
            log.info("task import: %s", json.dumps(
                {k: v for k, v in res.items() if k != "warnings"}, ensure_ascii=False))
        elif args.task_command == "add":
            ident = add_task(repo, args.slug, args.title, estimate_h=args.estimate_h,
                             prefix=args.prefix, sprint=args.sprint)
            task = next((t for t in repo.all_tasks() if t.get("identifier") == ident), {})
            sp = task.get("sprint_ext_id")
            log.info("task add: %s — %s → прочие работы %s", ident, args.title,
                     sp or "(вне спринтов — канон не импортирован)")
        elif args.task_command in ("start", "done"):
            emp = repo.upsert_employee(cfg.employee_username, dev_branch=cfg.dev_branch)
            to = STARTED_STATE if args.task_command == "start" else DONE_STATE
            res = transition(repo, emp, args.identifier, to, at=args.at)
            log.info("task %s: %s", args.task_command, json.dumps(res, ensure_ascii=False))
        elif args.task_command == "move":
            res = move_task(repo, args.identifier, args.sprint)
            log.info("task move: %s → %s", res["identifier"], res["sprint_ext_id"])
        elif args.task_command == "backfill-sprints":
            res = backfill_sprints(repo, slug=args.slug)
            log.info("task backfill-sprints: %s", json.dumps(res, ensure_ascii=False))
        elif args.task_command == "list":
            rows = list_tasks(repo, slug=args.slug, open_only=args.open)
            for t in rows:
                misc = "" if t.get("canon_task_id") else " [прочие]"
                log.info("%-12s %-12s %-9s %s%s", t.get("identifier"),
                         t.get("status") or "-", t.get("sprint_ext_id") or "-",
                         t.get("title") or "", misc)
            log.info("task list: %s задач", len(rows))
    except (ValueError, OSError,  # OSError: нет файла канона/нет прав — rc=1, не traceback
            TypeError, KeyError, AttributeError) as e:  # битый по форме канон (security-ревью)
        log.error("task %s: %s", args.task_command, e)
        return 1
    finally:
        repo.close()
    return 0


def _cmd_register_project(args: argparse.Namespace, cfg: Config) -> int:
    projects = register_project(
        cfg.db_path, slug=args.slug, repo_dir=args.repo_dir, branch=args.branch,
        prefix=args.prefix)
    log.info("register-project: '%s' привязан; проектов: %s → %s",
             args.slug, len(projects), registry_path(cfg.db_path))
    return 0


def _cmd_projects(args: argparse.Namespace, cfg: Config) -> int:
    projects = load_projects(cfg.db_path)
    log.info("projects (%s): %s", len(projects), json.dumps(projects, ensure_ascii=False))
    return 0


def _cmd_migrate_db(args: argparse.Namespace, cfg: Config) -> int:
    if not cfg.db_url:
        log.error("migrate-db: Postgres не включён — задай TIMECHECKER_BACKEND=postgres "
                  "(+ supabase_db_url в secrets) или TIMECHECKER_DB_URL")
        return 1
    from .storage.migrate import migrate_sqlite_to_postgres
    src = SqliteRepository.open(cfg.db_path)
    dst = open_repository(cfg)
    try:
        counts = migrate_sqlite_to_postgres(src, dst)
        log.info("migrate-db: перенесено в Postgres %s", counts)
    finally:
        src.close()
        dst.close()
    return 0


def _cmd_sync(args: argparse.Namespace, cfg: Config) -> int:
    dsn = cfg.supabase_dsn()
    if not dsn:
        log.error("sync: нет Supabase DSN (supabase_db_url в secrets / TIMECHECKER_DB_URL)")
        return 1
    from .storage.postgres_repository import PostgresRepository
    from .storage.sync import sync_to_postgres
    src = SqliteRepository.open(cfg.db_path)
    dst = PostgresRepository.open(dsn)
    try:
        counts = sync_to_postgres(src, dst, full=args.full, reset=args.reset,
                                  lookback_days=cfg.collect_lookback_days)
        log.info("sync → Supabase: %s", counts)
    finally:
        src.close()
        dst.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="timechecker",
        description="Учёт реального рабочего времени по output-сигналам "
                    "(Claude/codex/git + собственный реестр задач).",
    )
    p.add_argument("--version", action="version", version=f"timechecker {__version__}")
    p.add_argument(
        "--log-level",
        default=os.environ.get("TIMECHECKER_LOG_LEVEL", "INFO"),
        help="Уровень логов (DEBUG/INFO/WARNING/ERROR)",
    )
    p.add_argument(
        "--json-logs",
        action="store_true",
        default=os.environ.get("TIMECHECKER_JSON_LOGS", "") not in ("", "0"),
        help="Логи в формате JSON",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("initdb", help="Создать/мигрировать БД SQLite (применить схему)")
    hook_p = sub.add_parser("hook", help="Записать событие хука сессии в спул (для Claude Code)")
    hook_p.add_argument("event", choices=HOOK_EVENTS)
    hook_p.add_argument("--session", default=None, help="sessionId")
    hook_p.add_argument("--project", default=None, help="project_key")
    collect_p = sub.add_parser("collect", help="Собрать output-сигналы в БД")
    collect_p.add_argument("--since", default=None, help="ISO-время; события не старше")
    collect_p.add_argument("--full", action="store_true", help="полный пересбор (без окна)")
    metrics_p = sub.add_parser("metrics", help="Посчитать дневные метрики (E3) за дату")
    metrics_p.add_argument("--date", default=None, help="YYYY-MM-DD (МСК); по умолчанию сегодня")
    sched_p = sub.add_parser("schedule", help="Периодический сбор через Task Scheduler")
    sched_p.add_argument("--name", default="timechecker-collect")
    sched_p.add_argument("--command", default="timechecker collect")
    sched_p.add_argument("--every", type=int, default=30, help="период, минут")
    report_p = sub.add_parser("report", help="Дневной отчёт (markdown) из daily_*")
    report_p.add_argument("--date", default=None, help="YYYY-MM-DD (МСК); по умолчанию сегодня")
    sub.add_parser("health", help="Диагностика агента (БД, последний сбор, расписание)")
    prune_p = sub.add_parser("prune", help="Очистить сырьё старше N дней (ретеншн)")
    prune_p.add_argument("--days", type=int, default=None, help="дней (по умолчанию из конфига)")
    deploy_p = sub.add_parser("deploy", help="Развернуть агент (Task Scheduler: collect + report)")
    deploy_p.add_argument("--every", type=int, default=30, help="период collect, минут")
    deploy_p.add_argument("--report-at", default="23:50", help="время дневного отчёта HH:MM")
    deploy_p.add_argument("--sync-every", type=int, default=60, help="период sync, минут")
    daily_p = sub.add_parser("daily", help="Дневной прогон: метрики + отчёт за сегодня")
    daily_p.add_argument("--date", default=None, help="YYYY-MM-DD (МСК); по умолчанию сегодня")
    task_p = sub.add_parser("task", help="Собственный реестр задач: import/add/start/done/list")
    tsub = task_p.add_subparsers(dest="task_command", required=True)
    ti = tsub.add_parser("import", help="Импорт канона плана (JSON) в БД; назначает readable-ID")
    ti.add_argument("--plan", required=True, help="путь к канону 00_<slug>_plan.json")
    ti.add_argument("--slug", default=None, help="slug проекта (по умолчанию из канона)")
    ta = tsub.add_parser("add", help="Добавить задачу вне канона (печатает назначенный ID)")
    ta.add_argument("--slug", required=True)
    ta.add_argument("--title", required=True)
    ta.add_argument("--estimate-h", type=float, default=None)
    ta.add_argument("--prefix", default=None, help="префикс readable-ID (по умолчанию из проекта)")
    ta.add_argument("--sprint", default=None,
                    help="спринт «Прочих работ» (sX.Y); по умолчанию резолв по дате")
    for name, hlp in (("start", "Перевести задачу в работу (In Progress)"),
                      ("done", "Завершить задачу (Done)")):
        tp = tsub.add_parser(name, help=hlp)
        tp.add_argument("identifier", help="readable-ID, напр. TIME-55")
        tp.add_argument("--at", default=None, help="ISO-время перехода (по умолчанию сейчас)")
    tm = tsub.add_parser("move", help="Перепривязать внеплановую задачу к другому спринту")
    tm.add_argument("identifier", help="readable-ID, напр. TIME-67")
    tm.add_argument("--sprint", required=True, help="целевой спринт (sX.Y)")
    tl = tsub.add_parser("list", help="Список задач из БД")
    tl.add_argument("--slug", default=None)
    tl.add_argument("--open", action="store_true", help="только незавершённые")
    tb = tsub.add_parser("backfill-sprints",
                         help="Бэкфилл sprint_ext_id у внеплановых задач (одноразово)")
    tb.add_argument("--slug", default=None)
    rp = sub.add_parser("register-project", help="Привязать проект к учёту времени (git + задачи)")
    rp.add_argument("--slug", required=True)
    rp.add_argument("--repo-dir", default=None)
    rp.add_argument("--branch", default=None)
    rp.add_argument("--prefix", default=None, help="префикс readable-ID задач (напр. TIME)")
    sub.add_parser("projects", help="Список привязанных проектов")
    sub.add_parser("migrate-db", help="Перенести данные SQLite → Postgres (по db_url)")
    sync_p = sub.add_parser("sync", help="Реплицировать SQLite → Supabase (инкрементально)")
    sync_p.add_argument("--full", action="store_true", help="полная репликация (все строки)")
    sync_p.add_argument("--reset", action="store_true", help="TRUNCATE Supabase + ресед")
    sub.add_parser("pricing-refresh", help="Обновить ставки токенов из LiteLLM")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level, json_logs=args.json_logs)
    cfg = Config.load()
    for warn in cfg.validate():
        get_logger("timechecker.config").warning(warn)
    handlers = {
        "initdb": _cmd_initdb,
        "hook": _cmd_hook,
        "collect": _cmd_collect,
        "metrics": _cmd_metrics,
        "schedule": _cmd_schedule,
        "report": _cmd_report,
        "health": _cmd_health,
        "prune": _cmd_prune,
        "deploy": _cmd_deploy,
        "daily": _cmd_daily,
        "task": _cmd_task,
        "register-project": _cmd_register_project,
        "projects": _cmd_projects,
        "migrate-db": _cmd_migrate_db,
        "sync": _cmd_sync,
        "pricing-refresh": _cmd_pricing_refresh,
    }
    return handlers[args.command](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
