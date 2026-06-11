import subprocess

from timechecker.collectors.orchestrator import collect_all
from timechecker.config import Config
from timechecker.registry import load_projects, register_project


def test_register_and_load(tmp_path):
    db = tmp_path / "db.sqlite"
    register_project(db, slug="proj1", repo_dir="/x", prefix="PR")
    projects = load_projects(db)
    assert len(projects) == 1 and projects[0]["slug"] == "proj1"
    register_project(db, slug="proj1", repo_dir="/y")  # обновление по slug — без дубля
    projects = load_projects(db)
    assert len(projects) == 1 and projects[0]["repo_dir"] == "/y"
    register_project(db, slug="proj2")
    assert len(load_projects(db)) == 2


def test_collect_all_uses_registry(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()

    def g(*a: str) -> None:
        subprocess.run(["git", "-C", str(work), *a], check=True, capture_output=True, text=True)

    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "Tester")
    (work / "f").write_text("1", encoding="utf-8")
    g("add", "-A")
    g("commit", "-q", "-m", "x (TIME-1)")

    db = tmp_path / "db.sqlite"
    register_project(db, slug="reg-proj", repo_dir=str(work))
    monkeypatch.setenv("TIMECHECKER_DB_PATH", str(db))
    monkeypatch.setenv("TIMECHECKER_CLAUDE_PROJECTS_DIR", str(tmp_path / "no_projects"))
    monkeypatch.setenv("TIMECHECKER_WGP_SECRETS", str(tmp_path / "none.json"))
    cfg = Config.load()
    counts = collect_all(cfg)
    assert counts.get("commits") == 1  # git собран по проекту из реестра
    assert "errors" not in counts
