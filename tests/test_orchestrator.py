import json

from timechecker.collectors.orchestrator import collect_all
from timechecker.collectors.scheduler import build_schtasks_args
from timechecker.config import Config

_TRANSCRIPT = "\n".join(json.dumps(o) for o in [
    {"type": "mode", "sessionId": "s1"},
    {"type": "user", "sessionId": "s1", "uuid": "u1", "timestamp": "2026-06-09T08:00:00Z",
     "message": {"role": "user", "content": "hi"}},
    {"type": "assistant", "sessionId": "s1", "uuid": "a1", "timestamp": "2026-06-09T08:01:00Z",
     "message": {"role": "assistant", "usage": {"input_tokens": 5, "output_tokens": 7},
                 "content": [{"type": "tool_use"}]}},
])


def test_collect_all_claude_hooks(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    pdir = projects / "p1"
    pdir.mkdir(parents=True)
    (pdir / "t.jsonl").write_text(_TRANSCRIPT, encoding="utf-8")
    monkeypatch.setenv("TIMECHECKER_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TIMECHECKER_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("TIMECHECKER_WGP_SECRETS", str(tmp_path / "none.json"))
    cfg = Config.load()
    counts = collect_all(cfg)
    assert counts["events"] == 2 and counts["sessions"] == 1 and counts["hook_events"] == 0
    assert collect_all(cfg)["events"] == 2  # идемпотентно


def test_build_schtasks_args():
    args = build_schtasks_args("tc", "timechecker collect", 30)
    assert args[0] == "schtasks"
    assert "/Create" in args and "/SC" in args and "MINUTE" in args
    assert "30" in args and "tc" in args and "timechecker collect" in args
