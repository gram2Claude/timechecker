"""CLI timechecker: entrypoint + подкоманды (initdb / hook / collect / schedule / report)."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta

from . import __version__
from .collectors.hooks import HOOK_EVENTS, append_hook_event
from .collectors.orchestrator import collect_all
from .collectors.plane import PlaneHttpClient
from .collectors.scheduler import register_task
from .config import Config
from .logging_setup import get_logger, setup_logging
from .metrics import compute_day
from .reporting import build_daily_report, report_html
from .storage import SqliteRepository, current_version, init_db

log = get_logger("timechecker.cli")


def _cmd_initdb(args: argparse.Namespace, cfg: Config) -> int:
    conn = init_db(cfg.db_path)
    log.info("initdb: схема применена (версия %s) → %s", current_version(conn), cfg.db_path)
    conn.close()
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
    repo = SqliteRepository.open(cfg.db_path)
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
    repo = SqliteRepository.open(cfg.db_path)
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
    }
    return handlers[args.command](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
