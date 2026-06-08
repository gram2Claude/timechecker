"""Настройка логирования timechecker (TIME-3).

Структурный лог на stdlib: текстовый формат по умолчанию, опционально JSON-строки.
"""

from __future__ import annotations

import json
import logging
import sys

_TEXT_FMT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


class _JsonFormatter(logging.Formatter):
    """Однострочный JSON на запись — удобно для машинного парсинга."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str | int = "INFO", *, json_logs: bool = False) -> None:
    """Сконфигурировать корневой логгер (идемпотентно — очищает прежние хендлеры)."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if json_logs else logging.Formatter(_TEXT_FMT))
    root.addHandler(handler)
    root.setLevel(level if isinstance(level, int) else str(level).upper())


def get_logger(name: str = "timechecker") -> logging.Logger:
    """Логгер проекта (под-логгеры: ``timechecker.<модуль>``)."""
    return logging.getLogger(name)
