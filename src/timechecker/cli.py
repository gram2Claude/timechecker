"""CLI timechecker: entrypoint + подкоманды (initdb / hook / collect / schedule / report)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime, timedelta

from . import __version__
from .collectors.hooks import HOOK_EVENTS, append_hook_event
from .collectors.orchestrator import collect_all
from .collectors.plane import PlaneHttpClient
from .collectors.scheduler import register_daily_task, register_task
from .config import Config
from .logging_setup import get_logger, setup_logging
from .metrics import compute_day
from .ops import health_check
from .registry import load_projects, register_project, registry_path
from .reporting import build_daily_report, report_html
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
    counts = collect_all(cfg)
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
        (out_dir / f"{date}.csv").write_text(rep["csv"], encoding="utf-8")
        log.info("report %s → %s", date, md_path)
        if args.plane_issue:
            secrets = cfg.read_wgp_secrets()
            client = PlaneHttpClient(
                secrets.get("plane_base_url", "https://api.plane.so"),
                secrets.get("plane_api_key", ""), secrets.get("plane_workspace_slug", ""),
                cfg.plane_project_id or "",
            )
            client.post_issue_comment(args.plane_issue, report_html(rep["markdown"]))
            log.info("report → Plane issue %s", args.plane_issue)
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
    """Дневной прогон: метрики + отчёт за сегодня (одна команда для планировщика)."""
    ns = argparse.Namespace(date=getattr(args, "date", None), plane_issue=None)
    rc1 = _cmd_metrics(ns, cfg)
    rc2 = _cmd_report(ns, cfg)
    return rc1 or rc2


def _cmd_deploy(args: argparse.Namespace, cfg: Config) -> int:
    exe = shutil.which("timechecker") or "timechecker"
    rc1 = register_task("timechecker-collect", f'"{exe}" collect', args.every)
    rc2 = register_daily_task("timechecker-report", f'"{exe}" daily', args.report_at)
    backend = "postgres" if cfg.db_url else "sqlite"
    log.info("deploy: collect/%sмин rc=%s; daily @%s rc=%s; backend=%s; exe=%s",
             args.every, rc1, args.report_at, rc2, backend, exe)
    log.info("deploy: подключи хуки (см. RUNBOOK); проверь 'timechecker health'")
    return 0 if rc1 == 0 and rc2 == 0 else 1


def _cmd_register_project(args: argparse.Namespace, cfg: Config) -> int:
    projects = register_project(
        cfg.db_path, slug=args.slug, repo_dir=args.repo_dir, branch=args.branch,
        plane_project_id=args.plane_project_id, plane_prefix=args.plane_prefix)
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="timechecker",
        description="Учёт реального рабочего времени по output-сигналам (Claude/git/Plane).",
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
    sub.add_parser("collect", help="Собрать output-сигналы (Claude/hooks/git/Plane) в БД")
    metrics_p = sub.add_parser("metrics", help="Посчитать дневные метрики (E3) за дату")
    metrics_p.add_argument("--date", default=None, help="YYYY-MM-DD (МСК); по умолчанию сегодня")
    sched_p = sub.add_parser("schedule", help="Периодический сбор через Task Scheduler")
    sched_p.add_argument("--name", default="timechecker-collect")
    sched_p.add_argument("--command", default="timechecker collect")
    sched_p.add_argument("--every", type=int, default=30, help="период, минут")
    report_p = sub.add_parser("report", help="Дневной отчёт (markdown+CSV) из daily_*")
    report_p.add_argument("--date", default=None, help="YYYY-MM-DD (МСК); по умолчанию сегодня")
    report_p.add_argument("--plane-issue", default=None, help="issue для отчёта в Plane")
    sub.add_parser("health", help="Диагностика агента (БД, последний сбор, расписание)")
    prune_p = sub.add_parser("prune", help="Очистить сырьё старше N дней (ретеншн)")
    prune_p.add_argument("--days", type=int, default=None, help="дней (по умолчанию из конфига)")
    deploy_p = sub.add_parser("deploy", help="Развернуть агент (Task Scheduler: collect + report)")
    deploy_p.add_argument("--every", type=int, default=30, help="период collect, минут")
    deploy_p.add_argument("--report-at", default="23:50", help="время дневного отчёта HH:MM")
    daily_p = sub.add_parser("daily", help="Дневной прогон: метрики + отчёт за сегодня")
    daily_p.add_argument("--date", default=None, help="YYYY-MM-DD (МСК); по умолчанию сегодня")
    rp = sub.add_parser("register-project", help="Привязать проект к учёту времени (git/Plane)")
    rp.add_argument("--slug", required=True)
    rp.add_argument("--repo-dir", default=None)
    rp.add_argument("--branch", default=None)
    rp.add_argument("--plane-project-id", default=None)
    rp.add_argument("--plane-prefix", default=None)
    sub.add_parser("projects", help="Список привязанных проектов")
    sub.add_parser("migrate-db", help="Перенести данные SQLite → Postgres (по db_url)")
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
        "register-project": _cmd_register_project,
        "projects": _cmd_projects,
        "migrate-db": _cmd_migrate_db,
    }
    return handlers[args.command](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
