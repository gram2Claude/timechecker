"""Дневной отчёт (E4, TIME-25): daily_* → markdown + CSV + HTML (для Plane-комментария)."""

from __future__ import annotations

import csv
import io
from html import escape
from typing import Any

from ..pricing import model_tier


def _hm(minutes: int | None) -> str:
    m = int(minutes or 0)
    return f"{m // 60}ч {m % 60}м"


def _hhmm(ts: str | None) -> str:
    return ts[11:16] if ts and len(ts) >= 16 else "?"


def _usd(x: Any) -> str:
    return f"${float(x or 0):.2f}"


def _models(summary: dict) -> str:
    """Список моделей с tier-ярлыком: ' · модели: opus (high), sonnet (medium)'."""
    fams = [f.strip() for f in (summary.get("models") or "").split(",") if f.strip()]
    return " · модели: " + ", ".join(f"{f} ({model_tier(f)})" for f in fams) if fams else ""


def build_daily_report(repo: Any, employee_id: int, work_date: str) -> dict:
    """Собрать дневной отчёт из daily_*: markdown, csv и структурированные данные."""
    summary = repo.get_daily_summary(employee_id, work_date)
    tasks = repo.daily_task_times(employee_id, work_date)
    idles = repo.daily_idles(employee_id, work_date)
    return {
        "work_date": work_date,
        "summary": summary,
        "tasks": tasks,
        "idle": idles,
        "markdown": render_markdown(work_date, summary, tasks, idles),
        "csv": render_csv(work_date, tasks),
    }


def render_markdown(work_date: str, summary: dict | None,
                    tasks: list[dict], idles: list[dict]) -> str:
    if not summary:
        return f"# Отчёт за {work_date} (МСК)\n\n_Нет данных за день._\n"
    s = summary
    span = f"{_hhmm(s.get('span_start'))}–{_hhmm(s.get('span_end'))}"
    lines = [
        f"# Отчёт за {work_date} (МСК)",
        "",
        f"- **Рабочий день:** {span} (по событиям)",
        f"- **Активно:** {_hm(s.get('active_minutes'))} · "
        f"**простои:** {_hm(s.get('gap_minutes'))} "
        f"(эпизодов ≥30мин: {s.get('idle_ge30_count', 0)})",
        f"- **Задач за день:** {s.get('tasks_count', 0)} · "
        f"**переключений:** {s.get('switches', 0)} · "
        f"**макс. фокус:** {_hm(s.get('longest_focus_min'))}",
        f"- **Claude:** {s.get('claude_messages', 0)} сообщ., {s.get('claude_tokens', 0)} токенов "
        f"(кэш: {s.get('claude_cache_read', 0)} чит. / {s.get('claude_cache_creation', 0)} зап.) · "
        f"стоимость **≈ {_usd(s.get('claude_cost_usd'))}**{_models(s)}",
        f"- **Коммитов:** {s.get('commits', 0)} · **гигиена:** {s.get('hygiene_score', 0)} "
        f"(доля с PLANE-ID)",
        "",
        "## Время по задачам",
        "| Задача | Активно | План | Сообщ. | Токены | ≈ $ | Коммиты |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in tasks:
        est = t.get("est_h")
        lines.append(
            f"| {t.get('plane_identifier') or '—'} {t.get('title') or ''} | "
            f"{_hm(t.get('active_minutes'))} | {f'{est}ч' if est is not None else '—'} | "
            f"{t.get('claude_messages', 0)} | {t.get('claude_tokens', 0)} | "
            f"{_usd(t.get('claude_cost_usd'))} | {t.get('commits', 0)} |"
        )
    if not tasks:
        lines.append("| — | — | — | — | — | — | — |")
    if idles:
        lines += ["", "## Простои ≥30 мин"]
        lines += [
            f"- {_hhmm(e.get('gap_start'))}–{_hhmm(e.get('gap_end'))} ({e.get('minutes', 0)} мин)"
            for e in idles
        ]
    return "\n".join(lines) + "\n"


def render_csv(work_date: str, tasks: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["work_date", "task", "active_minutes", "est_h", "claude_messages",
                "claude_tokens", "claude_cache_read", "claude_cache_creation",
                "claude_cost_usd", "commits"])
    for t in tasks:
        est = t.get("est_h")
        w.writerow([work_date, t.get("plane_identifier") or "", t.get("active_minutes") or 0,
                    est if est is not None else "", t.get("claude_messages") or 0,
                    t.get("claude_tokens") or 0, t.get("claude_cache_read") or 0,
                    t.get("claude_cache_creation") or 0, t.get("claude_cost_usd") or 0,
                    t.get("commits") or 0])
    return buf.getvalue()


def report_html(markdown: str) -> str:
    """Простая обёртка markdown → HTML для комментария Plane (без внешних зависимостей)."""
    return f"<pre>{escape(markdown)}</pre>"
