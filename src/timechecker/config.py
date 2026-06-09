"""Конфигурация timechecker (TIME-2).

Слой конфигурации: пути к артефактам Claude, секреты wgp (Plane/GitHub),
репозиторий и ветки, маппинг сотрудник↔ветка (= имя пользователя Windows в нижнем регистре).
Значения берутся из переменных окружения ``TIMECHECKER_*`` с разумными дефолтами.
"""

from __future__ import annotations

import getpass
import json
import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    """Прочитать env-переменную; пустая строка трактуется как «не задано»."""
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def current_username() -> str:
    """Имя пользователя ОС (для пилота = сотрудник)."""
    return _env("TIMECHECKER_EMPLOYEE", None) or getpass.getuser()


@dataclass(frozen=True)
class Config:
    """Разрешённая конфигурация процесса timechecker."""

    claude_home: Path
    claude_projects_dir: Path
    wgp_secrets_path: Path
    github_repo: str | None
    target_branch: str
    dev_branch: str
    employee_username: str
    db_path: Path
    project_slug: str | None
    monitored_repo_dir: Path | None
    monitored_repo_branch: str | None
    plane_project_id: str | None
    plane_identifier_prefix: str | None

    @classmethod
    def load(cls) -> Config:
        """Собрать конфиг из env + дефолтов (домашний каталог пользователя)."""
        home = Path.home()
        claude_home = Path(_env("TIMECHECKER_CLAUDE_HOME", str(home / ".claude")))
        projects = _env("TIMECHECKER_CLAUDE_PROJECTS_DIR", str(claude_home / "projects"))
        secrets = _env("TIMECHECKER_WGP_SECRETS", str(home / ".wgp" / "secrets.json"))
        username = current_username()
        dev_branch = _env("TIMECHECKER_DEV_BRANCH", None) or username.lower()
        db_path = _env(
            "TIMECHECKER_DB_PATH", str(claude_home / "timechecker" / "timechecker.db")
        )
        repo_dir = _env("TIMECHECKER_MONITORED_REPO_DIR", None)
        return cls(
            claude_home=claude_home,
            claude_projects_dir=Path(projects),
            wgp_secrets_path=Path(secrets),
            github_repo=_env("TIMECHECKER_GITHUB_REPO", None),
            target_branch=_env("TIMECHECKER_TARGET_BRANCH", "master"),
            dev_branch=dev_branch,
            employee_username=username,
            db_path=Path(db_path),
            project_slug=_env("TIMECHECKER_PROJECT_SLUG", None),
            monitored_repo_dir=Path(repo_dir) if repo_dir else None,
            monitored_repo_branch=_env("TIMECHECKER_MONITORED_REPO_BRANCH", None),
            plane_project_id=_env("TIMECHECKER_PLANE_PROJECT_ID", None),
            plane_identifier_prefix=_env("TIMECHECKER_PLANE_PREFIX", None),
        )

    def employee_branch(self) -> tuple[str, str]:
        """Маппинг сотрудник → рабочая ветка (= username в нижнем регистре)."""
        return self.employee_username, self.dev_branch

    def read_wgp_secrets(self) -> dict:
        """Секреты wgp (Plane/GitHub). Пустой dict, если файла нет/он битый."""
        try:
            return json.loads(self.wgp_secrets_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def validate(self) -> list[str]:
        """Список предупреждений (не падаем — для диагностики при старте)."""
        warnings: list[str] = []
        if not self.claude_projects_dir.exists():
            warnings.append(f"Каталог транскриптов Claude не найден: {self.claude_projects_dir}")
        if not self.wgp_secrets_path.exists():
            warnings.append(f"Секреты wgp не найдены: {self.wgp_secrets_path}")
        return warnings
