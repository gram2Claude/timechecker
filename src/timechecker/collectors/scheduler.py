"""Планировщик сбора (TIME-16): регистрация задачи Windows Task Scheduler.

Запускает ``timechecker collect`` каждые N минут под текущей учёткой. ``build_schtasks_args`` —
чистая функция (тестируемо); ``register_task`` выполняет ``schtasks``.
"""

from __future__ import annotations

import subprocess


def build_schtasks_args(task_name: str, command: str, every_minutes: int) -> list[str]:
    """Сформировать аргументы schtasks для периодической (поминутной) задачи."""
    return [
        "schtasks", "/Create", "/F",
        "/SC", "MINUTE", "/MO", str(every_minutes),
        "/TN", task_name, "/TR", command,
    ]


def register_task(task_name: str, command: str, every_minutes: int = 30) -> int:
    """Зарегистрировать задачу через schtasks. Возвращает returncode (0 = успех)."""
    try:
        res = subprocess.run(
            build_schtasks_args(task_name, command, every_minutes),
            capture_output=True, text=True,
        )
    except OSError:
        return 1
    return res.returncode
