import json
import subprocess

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
    counts = collect_all(cfg, full=True)
    assert counts["events"] == 2 and counts["sessions"] == 1 and counts["hook_events"] == 0
    assert collect_all(cfg, full=True)["events"] == 2  # идемпотентно


def test_build_schtasks_args():
    args = build_schtasks_args("tc", "timechecker collect", 30)
    assert args[0] == "schtasks"
    assert "/Create" in args and "/SC" in args and "MINUTE" in args
    assert "30" in args and "tc" in args and "timechecker collect" in args


def test_collect_all_with_git_and_branch_fallback(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    (projects / "p1").mkdir(parents=True)
    (projects / "p1" / "t.jsonl").write_text(_TRANSCRIPT, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    def g(*a: str) -> None:
        subprocess.run(["git", "-C", str(work), *a], check=True, capture_output=True, text=True)

    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "Tester")
    (work / "f").write_text("1", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "x (TIME-9)")

    monkeypatch.setenv("TIMECHECKER_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TIMECHECKER_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("TIMECHECKER_MONITORED_REPO_DIR", str(work))
    monkeypatch.setenv("TIMECHECKER_MONITORED_REPO_BRANCH", "nonexistent")  # тест fallback
    monkeypatch.setenv("TIMECHECKER_WGP_SECRETS", str(tmp_path / "none.json"))
    cfg = Config.load()
    counts = collect_all(cfg, full=True)
    assert counts["events"] == 2
    assert counts["commits"] == 1  # fallback на HEAD несмотря на неверную ветку
    assert "errors" not in counts


def test_collect_incremental_window(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    pdir = projects / "p1"
    pdir.mkdir(parents=True)
    old = json.dumps({"type": "user", "sessionId": "s9", "uuid": "old1",
                      "timestamp": "2020-01-01T00:00:00Z",
                      "message": {"role": "user", "content": "x"}})
    (pdir / "t.jsonl").write_text(old, encoding="utf-8")
    monkeypatch.setenv("TIMECHECKER_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TIMECHECKER_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("TIMECHECKER_WGP_SECRETS", str(tmp_path / "none.json"))
    cfg = Config.load()
    assert collect_all(cfg).get("events", 0) == 0       # окно lookback отфильтровало старое
    assert collect_all(cfg, full=True)["events"] == 1   # full — собирает
