"""Движок метрик (E3, TIME-18..24): сырьё за рабочий день (МСК) → daily_summary/task_time/idle.

Метрики:
  1. задачи за день      → daily_summary.tasks_count + строки daily_task_time
  2. время на задачу      → daily_task_time.active_minutes (+ est_h для adherence)
  3. простои ≥30 мин      → daily_idle + daily_summary.idle_ge30_*
  4. span рабочего дня     → daily_summary.span_start/end
  5. active vs gap         → daily_summary.active_minutes/gap_minutes
  6. effort-прокси Claude  → daily_task_time.claude_messages/tokens + summary
  7. фрагментация          → daily_summary.switches/longest_focus_min
  8. adherence             → daily_task_time.est_h (vs active_minutes; отношение считает отчёт)
  9. гигиена процесса       → daily_summary.hygiene_score (доля коммитов с PLANE-ID)

Атрибуция активного времени к задаче — по «окнам в работе» из plane_transition (от перехода в
started-статус до completed). Все таймстемпы — UTC; «рабочий день» = дата по МСК (UTC+3).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

MSK = timedelta(hours=3)
IDLE_THRESHOLD_MIN = 30
_STARTED = {"In Progress", "Started", "In progress"}
_COMPLETED = {"Done", "Completed", "Cancelled", "Canceled"}
_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)


def _parse(ts: str) -> datetime:
    """ISO-таймстемп (Z или с офсетом) → aware datetime в UTC."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(UTC)


def msk_date_of(ts: str) -> str:
    """Дата по МСК для UTC-таймстемпа (рабочий день)."""
    return (_parse(ts) + MSK).date().isoformat()


def msk_day_window(work_date: str) -> tuple[str, str]:
    """UTC-границы [начало, конец] суток МСК для даты (формат ...Z)."""
    d = datetime.fromisoformat(work_date)
    start = datetime(d.year, d.month, d.day, tzinfo=UTC) - MSK
    end = start + timedelta(days=1) - timedelta(seconds=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def build_task_windows(transitions: list[dict]) -> list[tuple[int, datetime, datetime]]:
    """Окна «в работе» по задачам: от перехода в started до следующего completed (или открыто)."""
    by_task: dict[int, list[dict]] = defaultdict(list)
    for tr in transitions:
        by_task[tr["task_id"]].append(tr)
    windows: list[tuple[int, datetime, datetime]] = []
    for tid, trs in by_task.items():
        trs.sort(key=lambda x: x["ts_utc"])
        open_start: datetime | None = None
        for tr in trs:
            to = tr.get("to_state")
            if to in _STARTED and open_start is None:
                open_start = _parse(tr["ts_utc"])
            elif to in _COMPLETED and open_start is not None:
                windows.append((tid, open_start, _parse(tr["ts_utc"])))
                open_start = None
        if open_start is not None:
            windows.append((tid, open_start, _FAR_FUTURE))
    return windows


def attribute(ts: datetime, windows: list[tuple[int, datetime, datetime]]) -> int | None:
    """Задача, в чьём окне «в работе» лежит момент ts. При нескольких — с самым поздним стартом."""
    best: tuple[datetime, int] | None = None
    for tid, start, end in windows:
        if start <= ts <= end and (best is None or start > best[0]):
            best = (start, tid)
    return best[1] if best else None


def _minutes(td: timedelta) -> int:
    return int(td.total_seconds() // 60)


def compute_day(repo: Any, employee_id: int, work_date: str, *,
                idle_threshold_min: int = IDLE_THRESHOLD_MIN) -> dict:
    """Посчитать метрики за work_date (МСК) и записать в daily_*. Идемпотентно."""
    w0, w1 = msk_day_window(work_date)
    events = sorted(repo.events_between(employee_id, w0, w1), key=lambda e: e["ts_utc"])
    repo.delete_daily_idle(employee_id, work_date)
    repo.delete_daily_task_time(employee_id, work_date)
    if not events:
        repo.upsert_daily_summary(employee_id, work_date, tasks_count=0)
        return {"tasks": 0, "idle_episodes": 0, "active_minutes": 0}

    windows = build_task_windows(repo.all_plane_transitions())
    tasks = {t["id"]: t for t in repo.all_tasks()}
    threshold = timedelta(minutes=idle_threshold_min)

    active = timedelta()
    gap = timedelta()
    idle_episodes: list[tuple[str, str, int]] = []
    per_task: dict[int, dict] = defaultdict(
        lambda: {"active": timedelta(), "messages": 0, "tokens": 0, "commits": 0})
    claude_messages = 0
    claude_tokens = 0
    switches = 0
    longest_focus = timedelta()
    cur_focus = timedelta()
    last_task: Any = None

    for i, e in enumerate(events):
        if e["source"] == "claude":
            claude_messages += 1
            meta = json.loads(e["meta_json"]) if e.get("meta_json") else {}
            tok = int(meta.get("tokens_in") or 0) + int(meta.get("tokens_out") or 0)
            claude_tokens += tok
            tid_e = attribute(_parse(e["ts_utc"]), windows)
            if tid_e is not None:
                per_task[tid_e]["messages"] += 1
                per_task[tid_e]["tokens"] += tok
        if i + 1 >= len(events):
            continue
        t0 = _parse(e["ts_utc"])
        delta = _parse(events[i + 1]["ts_utc"]) - t0
        if delta >= threshold:
            gap += delta
            idle_episodes.append((e["ts_utc"], events[i + 1]["ts_utc"], _minutes(delta)))
            last_task = None
            cur_focus = timedelta()
            continue
        active += delta
        tid = attribute(t0 + delta / 2, windows)
        if tid is not None:
            per_task[tid]["active"] += delta
            if tid != last_task:
                switches += 1
                cur_focus = delta
                last_task = tid
            else:
                cur_focus += delta
            longest_focus = max(longest_focus, cur_focus)

    commits = repo.commits_between(employee_id, w0, w1)
    commits_with_id = 0
    for c in commits:
        if c["task_ids"]:
            commits_with_id += 1
            for tid in c["task_ids"]:
                per_task[tid]["commits"] += 1
    hygiene = round(commits_with_id / len(commits), 3) if commits else 1.0

    for tid, d in per_task.items():
        repo.upsert_daily_task_time(
            employee_id, work_date, tid,
            active_minutes=_minutes(d["active"]), claude_messages=d["messages"],
            claude_tokens=d["tokens"], commits=d["commits"],
            est_h=tasks.get(tid, {}).get("estimate_h"),
        )
    for start, end, mins in idle_episodes:
        repo.insert_daily_idle(employee_id, work_date, start, end, mins)

    repo.upsert_daily_summary(
        employee_id, work_date,
        span_start=events[0]["ts_utc"], span_end=events[-1]["ts_utc"],
        active_minutes=_minutes(active), gap_minutes=_minutes(gap),
        idle_ge30_count=len(idle_episodes),
        idle_ge30_minutes=sum(m for _, _, m in idle_episodes),
        tasks_count=len(per_task), switches=switches,
        longest_focus_min=_minutes(longest_focus),
        claude_messages=claude_messages, claude_tokens=claude_tokens,
        commits=len(commits), hygiene_score=hygiene,
    )
    return {"tasks": len(per_task), "idle_episodes": len(idle_episodes),
            "active_minutes": _minutes(active)}
