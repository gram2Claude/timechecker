import json
from pathlib import Path

from timechecker.collectors.claude import (
    ClaudeCollector,
    derive_sessions,
    iter_project_events,
    parse_transcript,
)
from timechecker.collectors.hooks import HookCollector, append_hook_event, read_hook_events
from timechecker.storage import SqliteRepository


def _msg(ts: str, uid: str, sid: str = "s1") -> dict:
    return {"type": "user", "sessionId": sid, "uuid": uid, "timestamp": ts,
            "message": {"role": "user", "content": "x"}}


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


def test_parse_normalizes_ts_offsets(tmp_path):
    """Баг-репорт nexus_admin + финальное ревью (TIME-66): ts нормализуется при ЗАПИСИ
    в канонический UTC ...Z — иначе строковое SQL-окно events_between относит
    23:50+03:00 (=20:50Z) к чужому МСК-дню. Границы сессии — по времени, не по строке."""
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(o) for o in [
        _msg("2026-06-10T23:50:00+03:00", "u1"),
        _msg("2026-06-10T22:00:00Z", "u2"),
    ]), encoding="utf-8")
    events = parse_transcript(f)
    assert [e.ts_utc for e in events] == ["2026-06-10T20:50:00Z", "2026-06-10T22:00:00Z"]
    s = derive_sessions(events)["s1"]
    assert s["started_at"] == "2026-06-10T20:50:00Z"  # реальное начало, нормализовано
    assert s["ended_at"] == "2026-06-10T22:00:00Z"


def test_parse_normalizes_ts_fractions(tmp_path):
    """Доли секунд срезаются до канонической секундной точности (формат хранения):
    `.100Z` < `Z` лексикографически — в едином формате ловушка исчезает."""
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(o) for o in [
        _msg("2026-06-10T10:00:00Z", "u1"),
        _msg("2026-06-10T10:00:00.100Z", "u2"),
    ]), encoding="utf-8")
    events = parse_transcript(f)
    assert len(events) == 2  # оба события живы (идемпотентность — по uuid, не по ts)
    assert all(e.ts_utc == "2026-06-10T10:00:00Z" for e in events)
    s = derive_sessions(events)["s1"]
    assert s["started_at"] == s["ended_at"] == "2026-06-10T10:00:00Z"


def test_since_filter_compares_parsed_ts(tmp_path):
    """Окно since — тоже по распарсенному времени: событие 23:50+03:00 (=20:50Z)
    РАНЬШЕ since=21:00Z и должно отфильтроваться (строкой оно бы прошло)."""
    pdir = tmp_path / "projects" / "proj"
    pdir.mkdir(parents=True)
    (pdir / "t.jsonl").write_text("\n".join(json.dumps(o) for o in [
        _msg("2026-06-10T23:50:00+03:00", "u1"),
        _msg("2026-06-10T22:00:00Z", "u2"),
    ]), encoding="utf-8")
    events = iter_project_events(tmp_path / "projects", since="2026-06-10T21:00:00Z")
    assert [e.external_id for e in events] == ["u2"]


def test_parse_skips_malformed_ts(tmp_path):
    """Контракт границы: непарсябельный И нестроковый ts отбрасываются на входе —
    одна отравленная строка транскрипта не должна валить весь claude-сбор."""
    lines = [_msg("not-a-timestamp", "u1"), _msg("2026-06-10T10:00:00Z", "u2")]
    lines.append({"type": "user", "sessionId": "s1", "uuid": "u3",
                  "timestamp": 1765400000000,  # int вместо строки (финальное ревью)
                  "message": {"role": "user", "content": "x"}})
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    assert [e.external_id for e in parse_transcript(f)] == ["u2"]


def test_offset_ts_lands_in_correct_msk_day(tmp_path):
    """Сквозной (финальное ревью, P1 обоих ревьюеров): событие 02:30+03:00 — это
    23:30Z прошлых суток UTC, но МСК-день 2026-06-09; после нормализации строковое
    SQL-окно compute_day относит его к ПРАВИЛЬНОМУ дню."""
    from timechecker.metrics import compute_day
    projects = tmp_path / "projects"
    pdir = projects / "proj"
    pdir.mkdir(parents=True)
    (pdir / "t.jsonl").write_text(json.dumps(
        _msg("2026-06-09T02:30:00+03:00", "u1")), encoding="utf-8")
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg")
    ClaudeCollector(repo, projects).collect(emp)
    evs = repo.events_between(emp, "2026-06-08T21:00:00Z", "2026-06-09T20:59:59Z")
    assert len(evs) == 1 and evs[0]["ts_utc"] == "2026-06-08T23:30:00Z"
    res = compute_day(repo, emp, "2026-06-09")  # день МСК
    assert res["tasks"] == 0 and res["idle_episodes"] == 0  # событие учтено без падений
    assert repo.get_daily_summary(emp, "2026-06-09")["span_start"] == "2026-06-08T23:30:00Z"
    repo.close()


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


def test_parse_captures_cache_and_model(tmp_path):
    f = tmp_path / "t.jsonl"
    line = {"type": "assistant", "sessionId": "s2", "uuid": "a9",
            "timestamp": "2026-06-09T08:05:00Z",
            "message": {"role": "assistant", "model": "claude-opus-4-8",
                        "usage": {"input_tokens": 100, "output_tokens": 200,
                                  "cache_creation_input_tokens": 5000,
                                  "cache_read_input_tokens": 90000},
                        "content": [{"type": "text"}]}}
    f.write_text(json.dumps(line), encoding="utf-8")
    e = parse_transcript(f)[0]
    assert (e.cache_creation, e.cache_read) == (5000, 90000)
    assert e.model == "claude-opus-4-8"
    s = derive_sessions([e])["s2"]
    assert s["cache_read"] == 90000 and s["cache_creation"] == 5000
    assert s["model"] == "claude-opus-4-8"


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


def test_hooks_since_boundary_parses_ts(tmp_path):
    """Финальное ревью: хук с долями секунд (+00:00) на границе since не должен
    отсекаться строковым сравнением ('.' < 'Z'); ts нормализуется при записи в БД."""
    spool = tmp_path / "hooks.jsonl"
    append_hook_event(spool, "session-start", session_uid="s1",
                      ts_utc="2026-06-09T08:00:00.500000+00:00")  # ПОСЛЕ since по времени
    append_hook_event(spool, "stop", session_uid="s1",
                      ts_utc="2026-06-09T10:50:00+03:00")  # 07:50Z — ДО since по времени
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg")
    assert HookCollector(repo, spool).collect(emp, since="2026-06-09T08:00:00Z") == {
        "hook_events": 1}
    evs = repo.events_between(emp, "2026-06-09T00:00:00Z", "2026-06-09T23:59:59Z")
    assert [e["ts_utc"] for e in evs] == ["2026-06-09T08:00:00Z"]  # нормализован
    repo.close()
