from timechecker.config import Config


def test_load_defaults():
    cfg = Config.load()
    assert cfg.target_branch  # дефолт — master
    assert cfg.dev_branch == cfg.dev_branch.lower()  # ветка в нижнем регистре
    assert str(cfg.claude_projects_dir).endswith("projects")


def test_employee_branch_mapping(monkeypatch):
    monkeypatch.setenv("TIMECHECKER_EMPLOYEE", "Oleg")
    monkeypatch.delenv("TIMECHECKER_DEV_BRANCH", raising=False)
    cfg = Config.load()
    user, branch = cfg.employee_branch()
    assert user == "Oleg"
    assert branch == "oleg"  # = username.lower()


def test_env_override(monkeypatch):
    monkeypatch.setenv("TIMECHECKER_TARGET_BRANCH", "main")
    monkeypatch.setenv("TIMECHECKER_DEV_BRANCH", "feature-x")
    cfg = Config.load()
    assert cfg.target_branch == "main"
    assert cfg.dev_branch == "feature-x"


def test_read_wgp_secrets_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMECHECKER_WGP_SECRETS", str(tmp_path / "nope.json"))
    cfg = Config.load()
    assert cfg.read_wgp_secrets() == {}
