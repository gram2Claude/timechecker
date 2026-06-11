"""Коллектор транскриптов Claude Code (TIME-7/8/10).

Парсит ``~/.claude/projects/<project_key>/**/*.jsonl`` → события (TIME-7) → сессии (TIME-8) →
пишет в репозиторий (TIME-10). **Только метаданные**: таймстемпы, sessionId, токены и счётчики
тул-вызовов, ветка/проект — тела сообщений (thinking/text) не читаются и не хранятся.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_MESSAGE_TYPES = ("user", "assistant")
_MAX_LINE = 10_000_000  # пропускать аномально длинные строки (защита памяти при стриме)


def _ts_key(ts: str) -> datetime:
    """ISO-ts → aware datetime для СРАВНЕНИЯ (хранится исходная строка).

    Лексикографика ISO-строк ненадёжна: разные офсеты (`23:50+03:00` < `22:00Z` по строке,
    но позже по времени) и доли секунд (`.100Z` < `Z`) — тот же класс бага уже чинился в
    metrics/engine.py. Naive-ts трактуем как UTC (контракт «ts всегда с офсетом» —
    валидируется на границе в parse_transcript)."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class ClaudeEvent:
    """Нормализованное метаданные-событие транскрипта."""

    ts_utc: str
    session_uid: str
    external_id: str
    event_type: str
    role: str | None
    project_key: str | None
    git_branch: str | None
    tokens_in: int
    tokens_out: int
    cache_creation: int
    cache_read: int
    model: str | None
    tool_count: int
    is_sidechain: bool


def _iter_json_lines(path: Path) -> Iterator[dict]:
    # построчный стрим (не read_text целиком) — память ограничена самой длинной строкой,
    # а не размером файла; гигантскую строку пропускаем
    try:
        fh = path.open(encoding="utf-8")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line or len(line) > _MAX_LINE:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse_transcript(path: Path, project_key: str | None = None) -> list[ClaudeEvent]:
    """Распарсить один ``.jsonl`` транскрипт в список событий (user/assistant)."""
    out: list[ClaudeEvent] = []
    for o in _iter_json_lines(path):
        if o.get("type") not in _MESSAGE_TYPES:
            continue
        ts, sid, uid = o.get("timestamp"), o.get("sessionId"), o.get("uuid")
        if not (ts and sid and uid):
            continue
        try:
            _ts_key(ts)  # валидация на границе: ниже по конвейеру ts сравниваются парсингом
        except ValueError:
            continue
        msg = o.get("message") or {}
        usage = msg.get("usage") or {}
        content = msg.get("content")
        tool_count = (
            sum(1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use")
            if isinstance(content, list) else 0
        )
        out.append(ClaudeEvent(
            ts_utc=ts, session_uid=sid, external_id=uid, event_type="message",
            role=msg.get("role") or o.get("type"),
            project_key=project_key, git_branch=o.get("gitBranch"),
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
            cache_read=int(usage.get("cache_read_input_tokens") or 0),
            model=msg.get("model"),
            tool_count=tool_count, is_sidechain=bool(o.get("isSidechain")),
        ))
    return out


def derive_sessions(events: Iterable[ClaudeEvent]) -> dict[str, dict]:
    """Свернуть события в сессии по ``sessionId`` (границы min/max ts, суммы счётчиков)."""
    sessions: dict[str, dict] = {}
    for e in events:
        s = sessions.get(e.session_uid)
        if s is None:
            s = {"started_at": e.ts_utc, "ended_at": e.ts_utc, "message_count": 0,
                 "tool_calls": 0, "tokens_in": 0, "tokens_out": 0,
                 "cache_creation": 0, "cache_read": 0, "model": None,
                 "project_key": e.project_key}
            sessions[e.session_uid] = s
        # границы — по РАСПАРСЕННОМУ времени, не по строке (офсеты/доли секунд)
        if _ts_key(e.ts_utc) < _ts_key(s["started_at"]):
            s["started_at"] = e.ts_utc
        if _ts_key(e.ts_utc) > _ts_key(s["ended_at"]):
            s["ended_at"] = e.ts_utc
        s["message_count"] += 1
        s["tool_calls"] += e.tool_count
        s["tokens_in"] += e.tokens_in
        s["tokens_out"] += e.tokens_out
        s["cache_creation"] += e.cache_creation
        s["cache_read"] += e.cache_read
        if e.model:
            s["model"] = e.model
    return sessions


def iter_project_events(projects_dir: Path, *, since: str | None = None) -> list[ClaudeEvent]:
    """Распарсить все транскрипты под ``projects_dir`` (project_key = имя подкаталога)."""
    events: list[ClaudeEvent] = []
    if not projects_dir.exists():
        return events
    for pdir in sorted(projects_dir.iterdir()):
        if not pdir.is_dir():
            continue
        for f in sorted(pdir.rglob("*.jsonl")):
            events.extend(parse_transcript(f, project_key=pdir.name))
    if since:
        since_key = _ts_key(since)
        events = [e for e in events if _ts_key(e.ts_utc) >= since_key]
    return events


class ClaudeCollector:
    """Парсит транскрипты Claude и пишет события + сессии в репозиторий (идемпотентно)."""

    def __init__(self, repo: Any, projects_dir: Path) -> None:
        self.repo = repo
        self.projects_dir = Path(projects_dir)

    def collect(self, employee_id: int, *, since: str | None = None,
                project_resolver: Callable[[str | None], int | None] | None = None,
                ingest_run_id: int | None = None) -> dict:
        events = iter_project_events(self.projects_dir, since=since)
        resolve = project_resolver or (lambda _k: None)
        for e in events:
            self.repo.insert_event(
                employee_id, "claude", e.event_type, e.ts_utc,
                project_id=resolve(e.project_key), external_id=e.external_id,
                meta={"tokens_in": e.tokens_in, "tokens_out": e.tokens_out,
                      "cache_creation": e.cache_creation, "cache_read": e.cache_read,
                      "model": e.model, "tools": e.tool_count, "role": e.role,
                      "sidechain": e.is_sidechain},
                ingest_run_id=ingest_run_id,
            )
        sessions = derive_sessions(events)
        for sid, s in sessions.items():
            self.repo.upsert_agent_session(
                employee_id, "claude", sid, project_id=resolve(s["project_key"]),
                started_at=s["started_at"], ended_at=s["ended_at"],
                message_count=s["message_count"], tool_calls=s["tool_calls"],
                tokens_in=s["tokens_in"], tokens_out=s["tokens_out"],
                cache_creation=s["cache_creation"], cache_read=s["cache_read"],
                model=s["model"],
            )
        return {"events": len(events), "sessions": len(sessions)}
