"""Эксплуатация timechecker (E5): диагностика (health), деплой, ретеншн."""

from __future__ import annotations

from .health import health_check

__all__ = ["health_check"]
