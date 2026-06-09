from timechecker.collectors.scheduler import build_schtasks_args, build_schtasks_daily_args
from timechecker.config import Config
from timechecker.ops import health_check
from timechecker.storage import SqliteRepository


def test_schtasks_builders():
    a = build_schtasks_args("tc", "timechecker collect", 30)
    assert "MINUTE" in a and "30" in a and "/Create" in a
    d = build_schtasks_daily_args("tc-r", "timechecker report", "23:50")
    assert "DAILY" in d and "23:50" in d and "/ST" in d


def test_health_check(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMECHECKER_DB_PATH", str(tmp_path / "db.sqlite"))
    cfg = Config.load()
    repo = SqliteRepository.open(cfg.db_path)
    try:
        emp = repo.upsert_employee("Oleg")
        repo.insert_event(emp, "claude", "message", "2026-06-09T08:00:00Z", external_id="e1")
        info = health_check(repo, cfg)
        assert info["db_exists"] is True
        assert info["schema_version"] == 1
        assert info["stats"]["events"] == 1
        assert info["retention_days"] == 30
        assert "collect_task_scheduled" in info
    finally:
        repo.close()


def test_prune_via_repo(tmp_path):
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg")
    repo.insert_event(emp, "claude", "message", "2026-01-01T00:00:00Z", external_id="old")
    repo.insert_event(emp, "claude", "message", "2026-06-09T00:00:00Z", external_id="new")
    assert repo.prune_raw("2026-05-10T00:00:00Z") >= 1
    repo.close()
