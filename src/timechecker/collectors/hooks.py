"""Хуки сессий Claude (TIME-9): спул событий SessionStart/SessionEnd/Stop → activity_event.

Хуки Claude Code вызывают ``timechecker hook <event>``, которая дописывает строку в спул-jsonl.
``HookCollector`` читает спул и нормализует в события. Регистрация хуков в settings.json —
это деплой (E5), чтобы не конфликтовать с уже стоящими хуками памяти.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HOOK_EVENTS = ("session-start", "session-end", "stop")


def append_hook_event(spool: Path, event: str, *, session_uid: str | None = None,
                      project_key: str | None = None, ts_utc: str | None = None) -> None:
    """Дописать событие хука в спул (создаёт каталог при необходимости)."""
    spool = Path(spool)
    spool.parent.mkdir(parents=True, exist_ok=True)
    rec = {"event": event, "ts_utc": ts_utc or datetime.now(UTC).isoformat(),
           "session_uid": session_uid, "project_key": project_key}
    with spool.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_hook_events(spool: Path) -> list[dict]:
    """Прочитать все записи спула хуков."""
    spool = Path(spool)
    if not spool.exists():
        return []
    out: list[dict] = []
    for line in spool.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class HookCollector:
    """Читает спул хуков и пишет события сессий в репозиторий (идемпотентно)."""

    def __init__(self, repo: Any, spool: Path) -> None:
        self.repo = repo
        self.spool = Path(spool)

    def collect(self, employee_id: int, *, since: str | None = None,
                ingest_run_id: int | None = None) -> dict:
        n = 0
        for r in read_hook_events(self.spool):
            ts, ev = r.get("ts_utc"), r.get("event")
            if not ts or ev not in HOOK_EVENTS or (since and ts < since):
                continue
            ext = f"{r.get('session_uid') or 'na'}:{ev}:{ts}"
            self.repo.insert_event(
                employee_id, "hook", ev, ts, external_id=ext,
                meta={"session_uid": r.get("session_uid"), "project_key": r.get("project_key")},
                ingest_run_id=ingest_run_id,
            )
            n += 1
        return {"hook_events": n}
