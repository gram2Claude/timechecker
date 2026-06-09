import json

from timechecker.collectors.claude import ClaudeCollector
from timechecker.collectors.codex import CodexCollector
from timechecker.storage import SqliteRepository

_BODY = "secret password 12345 confidential body text"


def test_no_message_bodies_stored(tmp_path):
    """Тело сообщения Claude НЕ должно попадать в БД (метаданные-only, комплаенс §8)."""
    projects = tmp_path / "projects"
    pdir = projects / "p1"
    pdir.mkdir(parents=True)
    line = json.dumps({
        "type": "assistant", "sessionId": "s1", "uuid": "a1",
        "timestamp": "2026-06-09T08:00:00Z",
        "message": {"role": "assistant", "usage": {"input_tokens": 5, "output_tokens": 7},
                    "content": [{"type": "text", "text": _BODY}]},
    })
    (pdir / "t.jsonl").write_text(line, encoding="utf-8")
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg")
    ClaudeCollector(repo, projects).collect(emp)
    dump = "".join(
        str(dict(r)) for r in repo.conn.execute("SELECT * FROM activity_event").fetchall())
    assert _BODY not in dump  # тело не утекло ни в одно поле
    repo.close()


def test_no_codex_bodies_stored(tmp_path):
    """Тела codex (инструкции/response_item) НЕ должны попадать в БД (метаданные-only)."""
    sessions = tmp_path / "sessions"
    d = sessions / "2026" / "06" / "09"
    d.mkdir(parents=True)
    lines = [
        {"timestamp": "2026-06-09T08:00:00Z", "type": "session_meta",
         "payload": {"id": "cx-sec", "timestamp": "2026-06-09T08:00:00Z", "cwd": "C:\\x",
                     "base_instructions": {"text": _BODY}}},
        {"timestamp": "2026-06-09T08:01:00Z", "type": "response_item",
         "payload": {"type": "message", "content": [{"type": "output_text", "text": _BODY}]}},
        {"timestamp": "2026-06-09T08:02:00Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"input_tokens": 10, "cached_input_tokens": 0,
                                   "output_tokens": 5, "reasoning_output_tokens": 1,
                                   "total_tokens": 15}}}},
    ]
    (d / "rollout-s.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg")
    CodexCollector(repo, sessions).collect(emp)
    dump = "".join(
        str(dict(r)) for r in repo.conn.execute("SELECT * FROM activity_event").fetchall())
    dump += "".join(
        str(dict(r)) for r in repo.conn.execute("SELECT * FROM agent_session").fetchall())
    assert _BODY not in dump  # тело не утекло ни в одно поле
    repo.close()
