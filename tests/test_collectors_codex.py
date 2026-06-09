import json
from pathlib import Path

from timechecker.collectors.codex import (
    CodexCollector,
    iter_sessions,
    make_cwd_resolver,
    parse_rollout,
)
from timechecker.storage import SqliteRepository

_BODY = "secret codex answer body, confidential"


def _rollout_lines(sid="cx-1", cwd="C:\\dev\\proj", model="gpt-5.5", with_usage=True):
    lines = [
        {"timestamp": "2026-06-09T08:00:00.100Z", "type": "session_meta",
         "payload": {"id": sid, "timestamp": "2026-06-09T08:00:00.000Z", "cwd": cwd,
                     "base_instructions": {"text": _BODY}}},
        {"timestamp": "2026-06-09T08:00:01Z", "type": "turn_context",
         "payload": {"turn_id": "t1", "cwd": cwd, "model": model}},
        {"timestamp": "2026-06-09T08:00:05Z", "type": "response_item",
         "payload": {"type": "message", "content": [{"type": "output_text", "text": _BODY}]}},
    ]
    if with_usage:
        # накопительный total: reasoning ⊆ output (как в реальных логах: input+output=total)
        lines += [
            {"timestamp": "2026-06-09T08:05:00Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {
                 "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                       "output_tokens": 200, "reasoning_output_tokens": 50,
                                       "total_tokens": 1200}}}},
            {"timestamp": "2026-06-09T08:09:00Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": None}},  # null info — guard
            {"timestamp": "2026-06-09T08:10:00Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {
                 "total_token_usage": {"input_tokens": 5000, "cached_input_tokens": 2000,
                                       "output_tokens": 700, "reasoning_output_tokens": 100,
                                       "total_tokens": 5700}}}},
        ]
    return lines


def _write(path: Path, lines) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(o) for o in lines)
    path.write_text(text + "\nобрезанная строка{не json", encoding="utf-8")


def test_parse_rollout_totals_and_model(tmp_path):
    f = tmp_path / "2026" / "06" / "09" / "rollout-x.jsonl"
    _write(f, _rollout_lines())
    s = parse_rollout(f)
    assert s.session_uid == "cx-1" and s.model == "gpt-5.5"
    assert s.turns == 2  # null-info ход не считается
    # последний total = итог; tokens_out БЕЗ прибавления reasoning (он уже внутри output)
    assert (s.input_tokens, s.cached_input, s.output_tokens, s.reasoning) == (5000, 2000, 700, 100)
    assert s.started_at == "2026-06-09T08:00:00.000Z"
    assert s.ended_at == "2026-06-09T08:10:00Z"


def test_parse_rollout_skips_empty_and_idless(tmp_path):
    f1 = tmp_path / "2026" / "06" / "09" / "rollout-empty.jsonl"
    _write(f1, _rollout_lines(with_usage=False))  # без token_count
    assert parse_rollout(f1) is None
    f2 = tmp_path / "2026" / "06" / "09" / "rollout-noid.jsonl"
    lines = _rollout_lines()
    lines[0]["payload"]["id"] = None  # без id — нет ключа идемпотентности
    _write(f2, lines)
    assert parse_rollout(f2) is None


def test_iter_sessions_since_filter(tmp_path):
    _write(tmp_path / "2026" / "05" / "31" / "rollout-old.jsonl",
           _rollout_lines(sid="old"))
    _write(tmp_path / "2026" / "06" / "09" / "rollout-new.jsonl",
           _rollout_lines(sid="new"))
    got = iter_sessions(tmp_path, since="2026-06-01")
    assert [s.session_uid for s in got] == ["new"]  # каталог 05/31 отфильтрован по пути


def test_collector_idempotent_and_metadata_only(tmp_path):
    sessions = tmp_path / "sessions"
    _write(sessions / "2026" / "06" / "09" / "rollout-a.jsonl", _rollout_lines())
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg")
    c = CodexCollector(repo, sessions)
    assert c.collect(emp) == {"codex_sessions": 1}
    assert c.collect(emp) == {"codex_sessions": 1}  # повтор не плодит дубли
    evs = repo.events_between(emp, "2026-06-09T00:00:00Z", "2026-06-09T23:59:59Z")
    assert len(evs) == 1
    e = evs[0]
    assert (e["source"], e["event_type"]) == ("codex", "session")
    meta = json.loads(e["meta_json"])
    assert meta == {"input": 5000, "cached_input": 2000, "output": 700, "reasoning": 100,
                    "model": "gpt-5.5", "turns": 2}
    sess = repo._fetchone("SELECT * FROM agent_session WHERE source='codex'")
    assert sess["session_uid"] == "cx-1"
    assert (sess["tokens_in"], sess["tokens_out"], sess["cache_read"]) == (5000, 700, 2000)
    assert sess["cache_creation"] == 0 and sess["message_count"] == 2
    # комплаенс: тела (инструкции/response_item) не попадают в БД
    dump = "".join(str(r) for r in repo._query("SELECT * FROM activity_event"))
    dump += "".join(str(r) for r in repo._query("SELECT * FROM agent_session"))
    assert _BODY not in dump
    repo.close()


def test_collector_codex_since_floor(tmp_path):
    sessions = tmp_path / "sessions"
    _write(sessions / "2026" / "05" / "20" / "rollout-may.jsonl", _rollout_lines(sid="may"))
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg")
    # since не задан → нижняя граница codex_since (2026-06-01) всё равно режет май
    assert CodexCollector(repo, sessions).collect(emp) == {"codex_sessions": 0}
    repo.close()


def test_cwd_resolver_boundaries(tmp_path):
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    proj_dir = tmp_path / "repo"
    (proj_dir / "sub").mkdir(parents=True)
    other = tmp_path / "repo-old"
    other.mkdir()
    resolve = make_cwd_resolver(repo, [{"slug": "p1", "repo_dir": str(proj_dir)}])
    pid = resolve(str(proj_dir))
    assert pid is not None
    assert resolve(str(proj_dir / "sub")) == pid     # вложенный путь → тот же проект
    assert resolve(str(other)) is None               # `repo-old` НЕ матчится префиксом `repo`
    assert resolve(None) is None
    assert resolve(str(tmp_path / "elsewhere")) is None
    repo.close()
