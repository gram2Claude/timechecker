import json

from timechecker.collectors.claude import ClaudeCollector
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
