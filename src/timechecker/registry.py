"""Реестр мониторимых проектов: per-project привязка git для учёта времени.

Хранится рядом с БД (``<db_dir>/projects.json``). Команда ``timechecker register-project``
добавляет/обновляет запись; оркестратор сбора проходит по всем зарегистрированным проектам
(git-коммиты; задачи приходят из собственного реестра — `timechecker task import/add`).
Claude-транскрипты собираются глобально и в реестре не нуждаются. Поле ``prefix`` — префикс
readable-ID задач; легаси-ключ ``plane_prefix`` в существующих файлах читается оркестратором.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def registry_path(db_path: Any) -> Path:
    return Path(db_path).parent / "projects.json"


def load_projects(db_path: Any) -> list[dict]:
    p = registry_path(db_path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("projects", [])
    except (OSError, json.JSONDecodeError):
        return []


def register_project(db_path: Any, *, slug: str, repo_dir: str | None = None,
                     branch: str | None = None, prefix: str | None = None) -> list[dict]:
    """Добавить/обновить проект в реестре (по slug). Возвращает полный список проектов."""
    entry = {"slug": slug, "repo_dir": repo_dir, "branch": branch, "prefix": prefix}
    projects = [x for x in load_projects(db_path) if x.get("slug") != slug]
    projects.append(entry)
    p = registry_path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"projects": projects}, ensure_ascii=False, indent=2), encoding="utf-8")
    return projects
