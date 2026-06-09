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
            capture_output=True,
        )
    except OSError:
        return 1
    return res.returncode


def build_schtasks_daily_args(task_name: str, command: str, at_time: str) -> list[str]:
    """Аргументы schtasks для ежедневной задачи в HH:MM."""
    return ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", at_time,
            "/TN", task_name, "/TR", command]


def register_daily_task(task_name: str, command: str, at_time: str = "23:50") -> int:
    """Зарегистрировать ежедневную задачу через schtasks. Возвращает returncode."""
    try:
        res = subprocess.run(build_schtasks_daily_args(task_name, command, at_time),
                             capture_output=True)
    except OSError:
        return 1
    return res.returncode


def build_schtasks_weekly_args(task_name: str, command: str, day: str, at_time: str) -> list[str]:
    """Аргументы schtasks для еженедельной задачи (день недели MON/TUE/… + HH:MM)."""
    return ["schtasks", "/Create", "/F", "/SC", "WEEKLY", "/D", day, "/ST", at_time,
            "/TN", task_name, "/TR", command]


def register_weekly_task(task_name: str, command: str, *, day: str = "MON",
                         at_time: str = "06:00") -> int:
    """Зарегистрировать еженедельную задачу через schtasks. Возвращает returncode."""
    try:
        res = subprocess.run(build_schtasks_weekly_args(task_name, command, day, at_time),
                             capture_output=True)
    except OSError:
        return 1
    return res.returncode


def task_exists(task_name: str) -> bool:
    """Проверить наличие задачи в Task Scheduler."""
    try:
        res = subprocess.run(["schtasks", "/Query", "/TN", task_name],
                             capture_output=True)
    except OSError:
        return False
    return res.returncode == 0
