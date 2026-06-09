import json
from pathlib import Path

from timechecker.collectors.claude import ClaudeCollector, derive_sessions, parse_transcript
from timechecker.collectors.hooks import HookCollector, append_hook_event, read_hook_events
from timechecker.storage import SqliteRepository


def _write_transcript(path: Path) -> None:
    lines = [
        {"type": "mode", "sessionId": "s1"},  # не message — пропускается
        {"type": "user", "sessionId": "s1", "uuid": "u1", "timestamp": "2026-06-09T08:00:00Z",
         "message": {"role": "user", "content": "hi"}, "gitBranch": "oleg"},
        {"type": "assistant", "sessionId": "s1", "uuid": "a1",
         "timestamp": "2026-06-09T08:01:00Z",
         "message": {"role": "assistant", "usage": {"input_tokens": 10, "output_tokens": 20},
                     "content": [{"type": "thinking"},
                                 {"type": "tool_use"}, {"type": "tool_use"}]}},
    ]
    path.write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")


def test_parse_and_sessions(tmp_path):
    f = tmp_path / "t.jsonl"
    _write_transcript(f)
    events = parse_transcript(f, project_key="proj")
    assert len(events) == 2  # mode пропущен, метаданные-only
    a = next(e for e in events if e.external_id == "a1")
    assert (a.tokens_in, a.tokens_out, a.tool_count) == (10, 20, 2)
    sess = derive_sessions(events)
    assert set(sess) == {"s1"}
    s = sess["s1"]
    assert s["message_count"] == 2 and s["tool_calls"] == 2 and s["tokens_out"] == 20
    assert s["started_at"] == "2026-06-09T08:00:00Z"
    assert s["ended_at"] == "2026-06-09T08:01:00Z"


def test_claude_collector_writes_idempotent(tmp_path):
    projects = tmp_path / "projects"
    pdir = projects / "proj-key"
    pdir.mkdir(parents=True)
    _write_transcript(pdir / "t.jsonl")
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg", dev_branch="oleg")
    assert ClaudeCollector(repo, projects).collect(emp) == {"events": 2, "sessions": 1}
    assert ClaudeCollector(repo, projects).collect(emp) == {"events": 2, "sessions": 1}  # повтор
    evs = repo.events_between(emp, "2026-06-09T00:00:00Z", "2026-06-09T23:59:59Z")
    assert len(evs) == 2  # без дублей
    repo.close()


def test_hooks_spool_and_collector(tmp_path):
    spool = tmp_path / "hooks.jsonl"
    append_hook_event(spool, "session-start", session_uid="s1", ts_utc="2026-06-09T08:00:00Z")
    append_hook_event(spool, "stop", session_uid="s1", ts_utc="2026-06-09T09:00:00Z")
    assert len(read_hook_events(spool)) == 2
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg")
    assert HookCollector(repo, spool).collect(emp) == {"hook_events": 2}
    assert HookCollector(repo, spool).collect(emp) == {"hook_events": 2}  # идемпотентно
    assert len(repo.events_between(emp, "2026-06-09T00:00:00Z", "2026-06-09T23:59:59Z")) == 2
    repo.close()
