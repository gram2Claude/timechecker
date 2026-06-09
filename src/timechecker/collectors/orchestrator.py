"""Оркестратор сбора (TIME-15): один прогон всех настроенных коллекторов в рамках ingest_run.

Идемпотентно: повторный ``collect_all`` не плодит дубли. Источники по конфигу:
Claude и хуки — всегда; git — при ``monitored_repo_dir``; Plane — при наличии creds и project_id.
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..storage import SqliteRepository
from .claude import ClaudeCollector
from .git import GitCollector
from .hooks import HookCollector
from .plane import PlaneCollector, PlaneHttpClient


def collect_all(cfg: Config, *, since: str | None = None) -> dict:
    """Прогнать все настроенные коллекторы; вернуть сводные счётчики."""
    repo = SqliteRepository.open(cfg.db_path)
    try:
        emp = repo.upsert_employee(cfg.employee_username, dev_branch=cfg.dev_branch)
        project_id = None
        if cfg.plane_project_id or cfg.github_repo or cfg.project_slug:
            slug = cfg.project_slug or (cfg.github_repo or "monitored").split("/")[-1]
            project_id = repo.upsert_project(
                slug, repo=cfg.github_repo, plane_project_id=cfg.plane_project_id,
                plane_identifier=cfg.plane_identifier_prefix,
            )
        run = repo.start_ingest_run(emp, sources="claude,hook,git,plane")
        counts: dict[str, Any] = {}
        counts.update(ClaudeCollector(repo, cfg.claude_projects_dir).collect(
            emp, since=since, ingest_run_id=run))
        counts.update(HookCollector(repo, cfg.db_path.parent / "hooks.jsonl").collect(
            emp, since=since, ingest_run_id=run))
        if cfg.monitored_repo_dir:
            counts.update(GitCollector(repo, cfg.monitored_repo_dir).collect(
                emp, project_id=project_id, branch=cfg.monitored_repo_branch,
                since=since, ingest_run_id=run))
        secrets = cfg.read_wgp_secrets()
        if cfg.plane_project_id and secrets.get("plane_api_key"):
            client = PlaneHttpClient(
                secrets.get("plane_base_url", "https://api.plane.so"),
                secrets["plane_api_key"], secrets.get("plane_workspace_slug", ""),
                cfg.plane_project_id,
            )
            counts.update(PlaneCollector(
                repo, client, plane_identifier_prefix=cfg.plane_identifier_prefix or "",
            ).collect(emp, project_id=project_id, ingest_run_id=run))
        repo.finish_ingest_run(run, "ok", counts=counts)
        return counts
    finally:
        repo.close()
