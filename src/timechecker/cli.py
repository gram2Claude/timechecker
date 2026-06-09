"""CLI timechecker (TIME-3): entrypoint + подкоманды collect/report (пока заглушки).

Реальная логика появится в эпохах E2 (коллекторы → SQLite) и E4 (отчётность).
"""

from __future__ import annotations

import argparse
import os

from . import __version__
from .collectors.claude import ClaudeCollector
from .collectors.hooks import HOOK_EVENTS, HookCollector, append_hook_event
from .config import Config
from .logging_setup import get_logger, setup_logging
from .storage import SqliteRepository, current_version, init_db

log = get_logger("timechecker.cli")


def _cmd_collect(args: argparse.Namespace, cfg: Config) -> int:
    repo = SqliteRepository.open(cfg.db_path)
    try:
        emp = repo.upsert_employee(cfg.employee_username, dev_branch=cfg.dev_branch)
        run = repo.start_ingest_run(emp, sources="claude,hook")
        claude = ClaudeCollector(repo, cfg.claude_projects_dir).collect(emp, ingest_run_id=run)
        spool = cfg.db_path.parent / "hooks.jsonl"
        hooks = HookCollector(repo, spool).collect(emp, ingest_run_id=run)
        repo.finish_ingest_run(run, "ok", counts={**claude, **hooks})
        log.info(
            "collect: claude события=%s сессий=%s, hook события=%s → %s",
            claude["events"], claude["sessions"], hooks["hook_events"], cfg.db_path,
        )
    finally:
        repo.close()
    return 0


def _cmd_report(args: argparse.Namespace, cfg: Config) -> int:
    log.info("report: дневной отчёт по метрикам — будет реализован в E4")
    return 0


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
    sub.add_parser("collect", help="Собрать output-сигналы (Claude + хуки) в БД")
    hook_p = sub.add_parser("hook", help="Записать событие хука сессии в спул (для Claude Code)")
    hook_p.add_argument("event", choices=HOOK_EVENTS)
    hook_p.add_argument("--session", default=None, help="sessionId")
    hook_p.add_argument("--project", default=None, help="project_key")
    sub.add_parser("report", help="Сформировать дневной отчёт (E4)")
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
        "report": _cmd_report,
    }
    return handlers[args.command](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
