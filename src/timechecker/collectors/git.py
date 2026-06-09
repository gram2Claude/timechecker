"""Git-коллектор (TIME-11/12): коммиты dev-ветки → git_commit + commit_task + события.

Читает ``git log`` из рабочего репозитория, извлекает ``PLANE-ID`` из сообщений, пишет
метаданные (sha/branch/author/ts/subject) и связи коммит↔задача. Диффы/тело не читаются.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FS = "\x1f"  # разделитель полей
_RS = "\x1e"  # разделитель записей
_PLANE_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


@dataclass
class GitCommit:
    sha: str
    author: str
    ts_utc: str
    subject: str
    plane_ids: list[str]


def parse_plane_ids(text: str) -> list[str]:
    """Извлечь уникальные PLANE-ID (напр. TIME-4) из текста, сохраняя порядок."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _PLANE_RE.findall(text or ""):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _git_log(git_dir: Path, branch: str | None, since: str | None) -> str | None:
    """Выполнить git log; None при ошибке (не репозиторий / нет ветки)."""
    args = ["git", "-C", str(git_dir), "log", f"--pretty=format:%H{_FS}%an{_FS}%aI{_FS}%s{_RS}"]
    if since:
        args.append(f"--since={since}")
    if branch:
        args.append(branch)
    try:
        res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    except OSError:
        return None
    return res.stdout if res.returncode == 0 else None


def read_commits(git_dir: Path, *, branch: str | None = None,
                 since: str | None = None) -> list[GitCommit]:
    """Прочитать коммиты (метаданные). Если ветки нет локально — fallback на HEAD."""
    text = _git_log(git_dir, branch, since)
    if text is None and branch:
        text = _git_log(git_dir, None, since)  # ветки может не быть локально (напр. master в клоне)
    if text is None:
        return []
    out: list[GitCommit] = []
    for rec in text.split(_RS):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split(_FS)
        if len(parts) < 4:
            continue
        out.append(GitCommit(sha=parts[0], author=parts[1], ts_utc=parts[2],
                             subject=parts[3], plane_ids=parse_plane_ids(parts[3])))
    return out


class GitCollector:
    """Пишет коммиты dev-ветки + связи с задачами в репозиторий (идемпотентно)."""

    def __init__(self, repo: Any, git_dir: Path) -> None:
        self.repo = repo
        self.git_dir = Path(git_dir)

    def collect(self, employee_id: int, *, project_id: int | None = None,
                branch: str | None = None, since: str | None = None,
                ingest_run_id: int | None = None) -> dict:
        commits = read_commits(self.git_dir, branch=branch, since=since)
        links = 0
        for c in commits:
            cid = self.repo.upsert_git_commit(
                employee_id, c.sha, project_id=project_id, branch=branch,
                ts_utc=c.ts_utc, author=c.author, subject=c.subject,
            )
            for pid in c.plane_ids:
                tid = self.repo.task_id_by_identifier(pid)
                if tid is not None:
                    self.repo.link_commit_task(cid, tid)
                    links += 1
            self.repo.insert_event(
                employee_id, "git", "commit", c.ts_utc, project_id=project_id,
                external_id=c.sha, meta={"branch": branch, "plane_ids": c.plane_ids},
                ingest_run_id=ingest_run_id,
            )
        return {"commits": len(commits), "commit_task_links": links}
