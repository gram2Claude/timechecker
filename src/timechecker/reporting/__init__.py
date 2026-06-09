"""Отчётность timechecker (E4): daily_* → дневной отчёт (markdown / CSV / HTML)."""

from __future__ import annotations

from .report import build_daily_report, report_html

__all__ = ["build_daily_report", "report_html"]
