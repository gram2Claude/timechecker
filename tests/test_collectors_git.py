import subprocess
from pathlib import Path

from timechecker.collectors.git import GitCollector, parse_plane_ids, read_commits
from timechecker.storage import SqliteRepository


def _git(d: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True, text=True)


def _make_repo(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "Tester")
    (d / "a.txt").write_text("1", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "feat: thing (TIME-4)")
    (d / "a.txt").write_text("2", encoding="utf-8")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "chore: no id here")


def test_parse_plane_ids():
    assert parse_plane_ids("feat: x (TIME-4) and TIME-12") == ["TIME-4", "TIME-12"]
    assert parse_plane_ids("no id") == []
    assert parse_plane_ids("TIME-4 TIME-4") == ["TIME-4"]  # дедуп


def test_read_commits(tmp_path):
    repo_dir = tmp_path / "work"
    _make_repo(repo_dir)
    commits = read_commits(repo_dir)
    assert len(commits) == 2
    assert any(c.plane_ids == ["TIME-4"] for c in commits)


def test_read_commits_non_repo(tmp_path):
    assert read_commits(tmp_path / "nope") == []


def test_read_commits_branch_fallback(tmp_path):
    repo_dir = tmp_path / "work"
    _make_repo(repo_dir)
    # несуществующая ветка → fallback на HEAD
    assert len(read_commits(repo_dir, branch="does-not-exist")) == 2


def test_git_collector_writes_idempotent(tmp_path):
    repo_dir = tmp_path / "work"
    _make_repo(repo_dir)
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg")
    proj = repo.upsert_project("timechecker", plane_identifier="TIME")
    repo.upsert_task(proj, "TIME-4", title="schema")
    counts = GitCollector(repo, repo_dir).collect(emp, project_id=proj)
    assert counts["commits"] == 2
    assert counts["commit_task_links"] == 1  # связалась только существующая задача TIME-4
    # повтор не плодит дубли
    assert GitCollector(repo, repo_dir).collect(emp, project_id=proj)["commits"] == 2
    repo.close()
