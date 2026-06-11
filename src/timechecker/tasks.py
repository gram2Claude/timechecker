"""Собственный реестр задач (E9, plane_exit): import канона / add / start / done / list.

Заменяет Plane как источник ЗАПИСИ: задачи и переходы статусов пишутся напрямую в
репозиторий (local-first SQLite → sync в Supabase). Имена статусов согласованы с
metrics.engine (_STARTED/_COMPLETED) — переходы из CLI попадают в окна атрибуции наравне
с историческими из Plane. Идемпотентность перехода — по external_id "cli:{ident}:{to}:{ts}"
(повтор с тем же --at не дублирует ни transition, ни событие).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STARTED_STATE = "In Progress"
DONE_STATE = "Done"
_OPEN_EXCLUDES = {"Done", "Completed", "Cancelled", "Canceled"}
# статусы канона (workflow_global_plan) → статусы task-таблицы
_CANON_STATUS = {"done": DONE_STATE, "in_progress": STARTED_STATE,
                 "todo": "Todo", "backlog": "Backlog"}
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now_utc() -> str:
    return datetime.now(UTC).strftime(_TS_FMT)


def _normalize_ts(at: str) -> str:
    """Произвольный ISO (--at) → канонический UTC "...Z"; бросает ValueError на мусоре."""
    dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime(_TS_FMT)


def default_prefix(slug: str) -> str:
    """Префикс readable-ID из slug: ASCII-буквы/цифры, верхний регистр, ≥2 символов."""
    pfx = re.sub(r"[^A-Za-z0-9]", "", slug).upper()[:6]
    return pfx if len(pfx) >= 2 else "TASK"


def next_identifier(repo: Any, prefix: str) -> str:
    """Следующий свободный readable-ID: prefix + (max существующего sequence + 1)."""
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    mx = 0
    for t in repo.all_tasks():
        m = pat.match(t.get("plane_identifier") or "")
        if m:
            mx = max(mx, int(m.group(1)))
    return f"{prefix}-{mx + 1}"


def iter_canon_tasks(canon: dict):
    for ep in canon.get("epochs", []):
        for sp in ep.get("sprints", []):
            yield from sp.get("tasks", [])


def import_canon(repo: Any, canon_path: Path, *, slug: str | None = None) -> dict:
    """Идемпотентный импорт канона плана в БД.

    Задачам без readable-ID назначает "{prefix}-{n}" и дописывает его обратно в канон-JSON
    (поле plane_identifier — имя сохранено для совместимости со скриптами wgp до E9.1),
    чтобы конвенция коммитов [PREFIX-NN] продолжала работать.
    """
    canon = json.loads(canon_path.read_text(encoding="utf-8"))
    proj = canon.get("project", {})
    slug = slug or proj.get("slug")
    if not slug:
        raise ValueError("в каноне нет project.slug (и --slug не задан)")
    prefix = proj.get("plane_identifier") or default_prefix(slug)
    project_id = repo.upsert_project(slug, plane_identifier=prefix)
    seen = created = assigned = 0
    for t in iter_canon_tasks(canon):
        ident = t.get("plane_identifier")
        if not ident:
            ident = next_identifier(repo, prefix)
            t["plane_identifier"] = ident
            assigned += 1
        existed = repo.task_id_by_identifier(ident) is not None
        repo.upsert_task(
            project_id, ident, canon_task_id=t.get("id"), title=t.get("name"),
            estimate_h=t.get("estimate_h"),
            status=_CANON_STATUS.get(t.get("status"), t.get("status")))
        seen += 1
        created += 0 if existed else 1
    if assigned:
        canon_path.write_text(json.dumps(canon, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return {"project_id": project_id, "tasks": seen, "created": created,
            "updated": seen - created, "assigned_ids": assigned}


def add_task(repo: Any, slug: str, title: str, *, estimate_h: float | None = None,
             canon_id: str | None = None, prefix: str | None = None) -> str:
    """Одиночная задача вне канона; возвращает назначенный readable-ID."""
    project = repo.get_project(slug)
    pfx = prefix or (project or {}).get("plane_identifier") or default_prefix(slug)
    project_id = repo.upsert_project(slug, plane_identifier=pfx)
    ident = next_identifier(repo, pfx)
    repo.upsert_task(project_id, ident, canon_task_id=canon_id, title=title,
                     estimate_h=estimate_h, status="Todo")
    return ident


def transition(repo: Any, employee_id: int, identifier: str, to_state: str, *,
               at: str | None = None) -> dict:
    """Переход статуса: plane_transition + событие status_change + task.status."""
    tid = repo.task_id_by_identifier(identifier)
    if tid is None:
        raise ValueError(f"задача {identifier!r} не найдена — сначала task import/add")
    task = next(t for t in repo.all_tasks() if t["id"] == tid)
    ts = _normalize_ts(at) if at else _now_utc()
    ext = f"cli:{identifier}:{to_state}:{ts}"
    repo.insert_plane_transition(tid, from_state=task.get("status"), to_state=to_state,
                                 ts_utc=ts, external_id=ext)
    repo.insert_event(employee_id, "task", "status_change", ts, task_id=tid,
                      project_id=task.get("project_id"), external_id=ext,
                      meta={"to": to_state})
    repo.upsert_task(task["project_id"], identifier, status=to_state)
    return {"task_id": tid, "identifier": identifier, "to": to_state, "ts_utc": ts}


def _ident_key(ident: str) -> tuple:
    m = re.match(r"^(.*)-(\d+)$", ident or "")
    return (m.group(1), int(m.group(2))) if m else (ident or "", 0)


def list_tasks(repo: Any, *, slug: str | None = None, open_only: bool = False) -> list[dict]:
    tasks = repo.all_tasks()
    if slug:
        project = repo.get_project(slug)
        pid = project["id"] if project else -1
        tasks = [t for t in tasks if t["project_id"] == pid]
    if open_only:
        tasks = [t for t in tasks if (t.get("status") or "") not in _OPEN_EXCLUDES]
    return sorted(tasks, key=lambda t: _ident_key(t.get("plane_identifier") or ""))
