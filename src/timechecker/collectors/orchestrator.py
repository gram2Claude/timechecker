"""Оркестратор сбора (TIME-15): один прогон всех настроенных коллекторов в рамках ingest_run.

Идемпотентно: повторный ``collect_all`` не плодит дубли. Claude и хуки — всегда (глобально).
git — по каждому проекту из конфига (env) и из реестра ``projects.json``. Задачи появляются в БД
через собственный реестр (`timechecker task import/add`, E9) — git-коллектор находит их по
TASK-ID при линковке commit_task. Сбой коллектора изолируется (ingest_run.error), прогон не
падает. Счётчики суммируются.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import Config
from ..registry import load_projects
from ..storage import open_repository
from .claude import ClaudeCollector
from .codex import CodexCollector, make_cwd_resolver
from .git import GitCollector
from .hooks import HookCollector

_HOME = os.path.expanduser("~")


def _sanitize_error(msg: str, *, limit: int = 500) -> str:
    """Подготовить текст ошибки коллектора к записи (она реплицируется в облако): заменить
    домашний путь на ``~`` и усечь длину — не льём абсолютные пути/«простыни» в Supabase."""
    if _HOME and _HOME in msg:
        msg = msg.replace(_HOME, "~")
    return msg[:limit]


def _merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, (int, float)) and isinstance(dst.get(k), (int, float)):
            dst[k] += v
        else:
            dst[k] = v


def _sources(cfg: Config) -> list[dict]:
    """Список проектов для git: из конфига (env) + из реестра (дедуп по slug)."""
    out: list[dict] = []
    if cfg.github_repo or cfg.project_slug or cfg.monitored_repo_dir:
        slug = cfg.project_slug or (cfg.github_repo or "monitored").split("/")[-1]
        out.append({
            "slug": slug, "repo": cfg.github_repo,
            "repo_dir": str(cfg.monitored_repo_dir) if cfg.monitored_repo_dir else None,
            "branch": cfg.monitored_repo_branch, "prefix": cfg.task_prefix,
        })
    for proj in load_projects(cfg.db_path):
        if not any(s["slug"] == proj.get("slug") for s in out):
            out.append(proj)
    return out


def collect_all(cfg: Config, *, since: str | None = None, full: bool = False) -> dict:
    """Прогнать все настроенные коллекторы по всем проектам; вернуть сводные счётчики.

    По умолчанию инкрементально: окно ``since`` = now − ``collect_lookback_days`` (idempotent
    upsert поверх накопленной БД — критично для Postgres по сети). ``full=True`` — полный пересбор.
    """
    if since is None and not full:
        days = getattr(cfg, "collect_lookback_days", 2)
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = open_repository(cfg)
    try:
        emp = repo.upsert_employee(cfg.employee_username, dev_branch=cfg.dev_branch)
        run = repo.start_ingest_run(emp, sources="claude,codex,hook,git")
        counts: dict[str, Any] = {}
        errors: dict[str, str] = {}

        def _run(name: str, fn: Callable[[], dict]) -> None:
            try:
                _merge(counts, fn())
            except Exception as e:  # изоляция: сбой одного коллектора не рушит прогон
                errors[name] = _sanitize_error(f"{type(e).__name__}: {e}")

        sources = _sources(cfg)
        _run("claude", lambda: ClaudeCollector(repo, cfg.claude_projects_dir).collect(
            emp, since=since, ingest_run_id=run))
        _run("codex", lambda: CodexCollector(
            repo, cfg.codex_sessions_dir, codex_since=cfg.codex_since).collect(
            emp, since=since, project_resolver=make_cwd_resolver(repo, sources),
            ingest_run_id=run))
        _run("hook", lambda: HookCollector(repo, cfg.db_path.parent / "hooks.jsonl").collect(
            emp, since=since, ingest_run_id=run))

        for proj in sources:
            pid = repo.upsert_project(
                proj["slug"], repo=proj.get("repo"),
                identifier_prefix=proj.get("prefix") or proj.get("plane_prefix"),
            )
            if proj.get("repo_dir"):
                _run(f"git:{proj['slug']}", lambda proj=proj, pid=pid:
                     GitCollector(repo, proj["repo_dir"]).collect(
                         emp, project_id=pid, branch=proj.get("branch"),
                         since=since, ingest_run_id=run))

        status = "ok" if not errors else "partial"
        repo.finish_ingest_run(
            run, status, error=json.dumps(errors, ensure_ascii=False) if errors else None,
            counts=counts)
        if errors:
            counts["errors"] = errors
        return counts
    finally:
        repo.close()
