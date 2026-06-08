"""CLI timechecker (TIME-3): entrypoint + подкоманды collect/report (пока заглушки).

Реальная логика появится в эпохах E2 (коллекторы → SQLite) и E4 (отчётность).
"""

from __future__ import annotations

import argparse
import os

from . import __version__
from .config import Config
from .logging_setup import get_logger, setup_logging

log = get_logger("timechecker.cli")


def _cmd_collect(args: argparse.Namespace, cfg: Config) -> int:
    log.info("collect: сбор output-сигналов (Claude/git/Plane) — будет реализован в E2")
    log.info(
        "employee=%s dev_branch=%s projects=%s",
        cfg.employee_username,
        cfg.dev_branch,
        cfg.claude_projects_dir,
    )
    return 0


def _cmd_report(args: argparse.Namespace, cfg: Config) -> int:
    log.info("report: дневной отчёт по метрикам — будет реализован в E4")
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
    sub.add_parser("collect", help="Собрать output-сигналы за период (E2)")
    sub.add_parser("report", help="Сформировать дневной отчёт (E4)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level, json_logs=args.json_logs)
    cfg = Config.load()
    for warn in cfg.validate():
        get_logger("timechecker.config").warning(warn)
    handlers = {"collect": _cmd_collect, "report": _cmd_report}
    return handlers[args.command](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
