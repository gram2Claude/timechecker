import timechecker


def test_version_present():
    assert isinstance(timechecker.__version__, str)
    assert timechecker.__version__
