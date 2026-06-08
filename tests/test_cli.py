import pytest

from timechecker.cli import main


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_collect_stub_returns_zero():
    assert main(["collect"]) == 0


def test_report_stub_returns_zero():
    assert main(["report"]) == 0


def test_no_command_errors():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0
