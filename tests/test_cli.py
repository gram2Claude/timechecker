import pytest

from timechecker.cli import main


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_collect_returns_zero(tmp_path, monkeypatch):
    # изолируем: пустой каталог проектов + temp БД — без чтения реальных транскриптов
    monkeypatch.setenv("TIMECHECKER_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("TIMECHECKER_CLAUDE_PROJECTS_DIR", str(tmp_path / "no_projects"))
    assert main(["collect"]) == 0


def test_report_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMECHECKER_DB_PATH", str(tmp_path / "db.sqlite"))
    assert main(["report"]) == 0


def test_no_command_errors():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0
