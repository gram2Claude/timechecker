"""Конфигурация timechecker (TIME-2).

Слой конфигурации: пути к артефактам Claude, секреты wgp (GitHub/Supabase),
репозиторий и ветки, маппинг сотрудник↔ветка (= имя пользователя Windows в нижнем регистре).
Значения берутся из переменных окружения ``TIMECHECKER_*`` с разумными дефолтами.
"""

from __future__ import annotations

import getpass
import json
import os
from dataclasses import dataclass, field
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
    codex_sessions_dir: Path
    codex_since: str
    wgp_secrets_path: Path
    github_repo: str | None
    target_branch: str
    dev_branch: str
    employee_username: str
    db_path: Path
    project_slug: str | None
    monitored_repo_dir: Path | None
    monitored_repo_branch: str | None
    task_prefix: str | None
    retention_days: int
    db_url: str | None = field(repr=False)  # DSN с паролем — не светить в repr/логах/трейсбэках
    collect_lookback_days: int

    @classmethod
    def load(cls) -> Config:
        """Собрать конфиг из env + дефолтов (домашний каталог пользователя)."""
        home = Path.home()
        claude_home = Path(_env("TIMECHECKER_CLAUDE_HOME", str(home / ".claude")))
        projects = _env("TIMECHECKER_CLAUDE_PROJECTS_DIR", str(claude_home / "projects"))
        codex_sessions = _env("TIMECHECKER_CODEX_SESSIONS_DIR",
                              str(home / ".codex" / "sessions"))
        secrets = _env("TIMECHECKER_WGP_SECRETS", str(home / ".wgp" / "secrets.json"))
        username = current_username()
        dev_branch = _env("TIMECHECKER_DEV_BRANCH", None) or username.lower()
        db_path = _env(
            "TIMECHECKER_DB_PATH", str(claude_home / "timechecker" / "timechecker.db")
        )
        repo_dir = _env("TIMECHECKER_MONITORED_REPO_DIR", None)
        # Postgres — ЯВНЫЙ opt-in: либо полный DSN в TIMECHECKER_DB_URL, либо
        # TIMECHECKER_BACKEND=postgres (тогда DSN берётся из secrets supabase_db_url).
        # По умолчанию (даже при наличии supabase_db_url в secrets) — SQLite.
        db_url = _env("TIMECHECKER_DB_URL", None)
        if not db_url and (_env("TIMECHECKER_BACKEND", "") or "").lower() == "postgres":
            try:
                raw = Path(secrets).read_text(encoding="utf-8")
                db_url = json.loads(raw).get("supabase_db_url")
            except (OSError, json.JSONDecodeError):
                db_url = None
        return cls(
            claude_home=claude_home,
            claude_projects_dir=Path(projects),
            codex_sessions_dir=Path(codex_sessions),
            codex_since=_env("TIMECHECKER_CODEX_SINCE", "2026-06-01") or "2026-06-01",
            wgp_secrets_path=Path(secrets),
            github_repo=_env("TIMECHECKER_GITHUB_REPO", None),
            target_branch=_env("TIMECHECKER_TARGET_BRANCH", "master"),
            dev_branch=dev_branch,
            employee_username=username,
            db_path=Path(db_path),
            project_slug=_env("TIMECHECKER_PROJECT_SLUG", None),
            monitored_repo_dir=Path(repo_dir) if repo_dir else None,
            monitored_repo_branch=_env("TIMECHECKER_MONITORED_REPO_BRANCH", None),
            # легаси-фоллбек TIMECHECKER_PLANE_PREFIX — на случай старых env-конфигов
            task_prefix=_env("TIMECHECKER_TASK_PREFIX", None)
            or _env("TIMECHECKER_PLANE_PREFIX", None),
            retention_days=int(_env("TIMECHECKER_RETENTION_DAYS", "30") or "30"),
            db_url=db_url,
            collect_lookback_days=int(_env("TIMECHECKER_COLLECT_LOOKBACK_DAYS", "2") or "2"),
        )

    def employee_branch(self) -> tuple[str, str]:
        """Маппинг сотрудник → рабочая ветка (= username в нижнем регистре)."""
        return self.employee_username, self.dev_branch

    def read_wgp_secrets(self) -> dict:
        """Секреты wgp (GitHub/Supabase). Пустой dict, если файла нет/он битый."""
        try:
            return json.loads(self.wgp_secrets_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def supabase_dsn(self) -> str | None:
        """DSN реплики для sync — независимо от default-backend (env или secrets).

        Поле secrets ``supabase_db_url`` — ЛЕГАСИ-имя (его читают и server_checker): с E12
        (2026-06-14) указывает на self-host PostgreSQL (WEECERE), а не на облачный Supabase.
        Имя поля не переименовываем намеренно — оно кросс-репо; смысл — «DSN облачной/self-host реплики».
        """
        return _env("TIMECHECKER_DB_URL", None) or self.read_wgp_secrets().get("supabase_db_url")

    def validate(self) -> list[str]:
        """Список предупреждений (не падаем — для диагностики при старте)."""
        warnings: list[str] = []
        if not self.claude_projects_dir.exists():
            warnings.append(f"Каталог транскриптов Claude не найден: {self.claude_projects_dir}")
        if not self.wgp_secrets_path.exists():
            warnings.append(f"Секреты wgp не найдены: {self.wgp_secrets_path}")
        return warnings
