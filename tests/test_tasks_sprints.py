"""Тесты спеки 11 (misc_works): справочник спринтов, резолв «Прочих работ», move, backfill."""

import json

import pytest

from timechecker.cli import main
from timechecker.storage import SqliteRepository
from timechecker.storage.sync import _ALL, _CONFLICT, _REF
from timechecker.tasks import (
    STARTED_STATE,
    add_task,
    backfill_sprints,
    import_canon,
    move_task,
    resolve_sprint,
    transition,
)

# канон в духе реальных: done-спринт s1.1 заморожен с датами «в будущем»,
# пересекающимися с открытым s1.2 (ревью плана P1.1)
CANON = {
    "project": {"slug": "demo", "plane_identifier": "DEMO", "misc_rate": 0.3},
    "epochs": [{
        "id": "e1",
        "sprints": [
            {"id": "s1.1", "name": "S1.1", "start_date": "2026-06-10", "end_date": "2026-06-20",
             "tasks": [
                 {"id": "t1.1.1", "name": "обычная done", "estimate_h": 4,
                  "status": "done", "plane_identifier": "DEMO-1"},
                 {"id": "s1.1.misc", "name": "Прочие работы", "task_type": "misc",
                  "auto": True, "status": "done", "estimate_h": 1.25,
                  "plane_identifier": "DEMO-2"},
             ]},
            {"id": "s1.2", "name": "S1.2", "start_date": "2026-06-10", "end_date": "2026-06-15",
             "tasks": [
                 {"id": "t1.2.1", "name": "открытая", "estimate_h": 2,
                  "status": "todo", "plane_identifier": "DEMO-3"},
             ]},
        ],
    }],
}


@pytest.fixture
def repo(tmp_path):
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    yield r
    r.close()


def _imported(repo, tmp_path, canon=CANON):
    p = tmp_path / "00_demo_plan.json"
    p.write_text(json.dumps(canon, ensure_ascii=False), encoding="utf-8")
    return import_canon(repo, p)


# ---------- import: справочник спринтов ----------

def test_import_fills_sprint_directory(repo, tmp_path):
    res = _imported(repo, tmp_path)
    assert res["sprints"] == 2 and res["warnings"] == []
    pid = repo.get_project("demo")["id"]
    rows = repo.sprints_for_project(pid)
    assert [(s["ext_id"], s["ord"], s["status"]) for s in rows] == [
        ("s1.1", 1, "done"), ("s1.2", 2, "open")]
    # плановым задачам проставлен спринт
    by_ident = {t["identifier"]: t for t in repo.all_tasks()}
    assert by_ident["DEMO-1"]["sprint_ext_id"] == "s1.1"
    assert by_ident["DEMO-3"]["sprint_ext_id"] == "s1.2"
    # misc-задача канона ПЛАНОВАЯ: canon_task_id заполнен → не «прочие»
    assert by_ident["DEMO-2"]["canon_task_id"] == "s1.1.misc"


def test_import_reimport_moves_task_between_sprints(repo, tmp_path):
    _imported(repo, tmp_path)
    moved = json.loads(json.dumps(CANON))
    t = moved["epochs"][0]["sprints"][1]["tasks"][0]  # DEMO-3 из s1.2
    moved["epochs"][0]["sprints"][0]["tasks"].append(t)
    moved["epochs"][0]["sprints"][1]["tasks"] = []
    _imported(repo, tmp_path, moved)
    by_ident = {x["identifier"]: x for x in repo.all_tasks()}
    assert by_ident["DEMO-3"]["sprint_ext_id"] == "s1.1"  # non-NULL перетирает (codex P2)


def test_import_warns_on_misc_without_id(repo, tmp_path):
    canon = json.loads(json.dumps(CANON))
    del canon["epochs"][0]["sprints"][0]["tasks"][1]["id"]
    res = _imported(repo, tmp_path, canon)
    assert any("misc" in w for w in res["warnings"])


# ---------- resolve_sprint: правило §4 на пересекающихся датах ----------

SPRINTS = [
    {"ext_id": "s1.1", "ord": 1, "status": "done",
     "start_date": "2026-06-10", "end_date": "2026-06-20"},
    {"ext_id": "s1.2", "ord": 2, "status": "open",
     "start_date": "2026-06-10", "end_date": "2026-06-15"},
    {"ext_id": "s2.1", "ord": 3, "status": "open",
     "start_date": "2026-06-18", "end_date": "2026-06-25"},
]


def test_resolve_covering_skips_done_sprints():
    # 2026-06-12 покрыт и done s1.1, и open s1.2 → s1.2 (done исключён)
    assert resolve_sprint(SPRINTS, "2026-06-12") == "s1.2"


def test_resolve_gap_between_sprints_takes_previous_open():
    # 16-17.06 — дыра между s1.2 и s2.1 → ближайший предыдущий открытый
    assert resolve_sprint(SPRINTS, "2026-06-16") == "s1.2"


def test_resolve_before_plan_takes_first_open():
    assert resolve_sprint(SPRINTS, "2026-06-01") == "s1.2"


def test_resolve_all_done_falls_to_last_by_ord():
    done = [dict(s, status="done") for s in SPRINTS]
    assert resolve_sprint(done, "2026-07-01") == "s2.1"


def test_resolve_no_sprints_returns_none():
    assert resolve_sprint([], "2026-06-12") is None


def test_resolve_open_sprint_without_dates_beats_done():
    """Ревью кода: открытый спринт без дат недостижим date-правилами —
    фоллбек должен предпочесть его done-спринту."""
    sprints = [
        {"ext_id": "s1", "ord": 1, "status": "open", "start_date": None, "end_date": None},
        {"ext_id": "s2", "ord": 2, "status": "done",
         "start_date": "2026-06-01", "end_date": "2026-06-05"},
    ]
    assert resolve_sprint(sprints, "2026-06-12") == "s1"


def test_reimport_removes_stale_sprints(repo, tmp_path):
    """Ревью кода: спринт, исчезнувший из канона (replan), удаляется из справочника —
    stale-строка не должна выигрывать резолв."""
    _imported(repo, tmp_path)
    pid = repo.get_project("demo")["id"]
    assert [s["ext_id"] for s in repo.sprints_for_project(pid)] == ["s1.1", "s1.2"]
    trimmed = json.loads(json.dumps(CANON))
    del trimmed["epochs"][0]["sprints"][1]
    _imported(repo, tmp_path, trimmed)
    assert [s["ext_id"] for s in repo.sprints_for_project(pid)] == ["s1.1"]


# ---------- add: резолв и явный --sprint ----------

def test_add_task_resolves_sprint(repo, tmp_path, monkeypatch):
    _imported(repo, tmp_path)
    monkeypatch.setattr("timechecker.tasks._today_msk", lambda: "2026-06-12")
    ident = add_task(repo, "demo", "Внеплановая")
    t = {x["identifier"]: x for x in repo.all_tasks()}[ident]
    assert t["sprint_ext_id"] == "s1.2" and t["canon_task_id"] is None


def test_add_task_explicit_sprint_validated(repo, tmp_path):
    _imported(repo, tmp_path)
    ident = add_task(repo, "demo", "Вне очереди", sprint="s1.1")
    assert {x["identifier"]: x for x in repo.all_tasks()}[ident]["sprint_ext_id"] == "s1.1"
    with pytest.raises(ValueError, match="не найден"):
        add_task(repo, "demo", "Опечатка", sprint="s9.9")


def test_add_task_no_directory_gives_null_sprint(repo):
    ident = add_task(repo, "fresh", "Без канона")
    assert {x["identifier"]: x for x in repo.all_tasks()}[ident]["sprint_ext_id"] is None


# ---------- move ----------

def test_move_unplanned_ok_planned_rejected(repo, tmp_path):
    _imported(repo, tmp_path)
    ident = add_task(repo, "demo", "Внеплановая", sprint="s1.2")
    res = move_task(repo, ident, "s1.1")
    assert res["sprint_ext_id"] == "s1.1"
    assert {x["identifier"]: x for x in repo.all_tasks()}[ident]["sprint_ext_id"] == "s1.1"
    with pytest.raises(ValueError, match="плановая"):
        move_task(repo, "DEMO-1", "s1.2")
    with pytest.raises(ValueError, match="не найден"):
        move_task(repo, ident, "s9.9")
    with pytest.raises(ValueError, match="не найдена"):
        move_task(repo, "NOPE-1", "s1.1")


# ---------- backfill ----------

def test_backfill_uses_first_started_and_is_idempotent(repo, tmp_path, monkeypatch):
    _imported(repo, tmp_path)
    monkeypatch.setattr("timechecker.tasks._today_msk", lambda: "2026-06-12")
    ident = add_task(repo, "demo", "Старая внеплановая")
    tid = repo.task_id_by_identifier(ident)
    repo.set_task_sprint(tid, None)  # имитация задачи, созданной до v5
    emp = repo.upsert_employee("oleg")
    transition(repo, emp, ident, STARTED_STATE, at="2026-06-16T10:00:00Z")  # дыра → s1.2
    res = backfill_sprints(repo, slug="demo")
    assert res == {"updated": 1, "skipped": 0}
    assert {x["identifier"]: x for x in repo.all_tasks()}[ident]["sprint_ext_id"] == "s1.2"
    assert backfill_sprints(repo, slug="demo") == {"updated": 0, "skipped": 0}  # идемпотентен


def test_backfill_without_transitions_uses_updated_at(repo, tmp_path, monkeypatch):
    _imported(repo, tmp_path)
    monkeypatch.setattr("timechecker.tasks._today_msk", lambda: "2026-06-12")
    ident = add_task(repo, "demo", "Никогда не стартовала")
    repo.set_task_sprint(repo.task_id_by_identifier(ident), None)
    res = backfill_sprints(repo)
    assert res["updated"] == 1


# ---------- sync: состав списков репликации (codex P1) ----------

def test_sync_table_lists_include_sprint():
    assert "sprint" in _REF and "sprint" in _ALL and "sprint" in _CONFLICT
    assert _REF.index("project") < _REF.index("sprint") < _REF.index("task")


# ---------- CLI: сквозной ----------

def test_cli_sprint_commands_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMECHECKER_DB_PATH", str(tmp_path / "db.sqlite"))
    p = tmp_path / "00_demo_plan.json"
    p.write_text(json.dumps(CANON, ensure_ascii=False), encoding="utf-8")
    assert main(["task", "import", "--plan", str(p)]) == 0
    assert main(["task", "add", "--slug", "demo", "--title", "X", "--sprint", "s1.2"]) == 0
    assert main(["task", "move", "DEMO-4", "--sprint", "s1.1"]) == 0
    assert main(["task", "move", "DEMO-1", "--sprint", "s1.2"]) == 1  # плановая → rc=1
    assert main(["task", "backfill-sprints", "--slug", "demo"]) == 0
    assert main(["task", "list", "--slug", "demo"]) == 0
