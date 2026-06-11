"""Тесты E9 plane_exit: собственный реестр задач (tasks.py + CLI `task ...`)."""

import json

import pytest

from timechecker.cli import main
from timechecker.metrics import compute_day
from timechecker.storage import SqliteRepository
from timechecker.tasks import (
    DONE_STATE,
    STARTED_STATE,
    add_task,
    import_canon,
    list_tasks,
    next_identifier,
    transition,
)

CANON = {
    "project": {"slug": "demo", "plane_identifier": "DEMO"},
    "epochs": [{
        "id": "e1",
        "sprints": [{
            "id": "s1.1",
            "tasks": [
                {"id": "t1.1.1", "name": "Задача с ID", "estimate_h": 4,
                 "status": "done", "plane_identifier": "DEMO-1"},
                {"id": "t1.1.2", "name": "Задача без ID", "estimate_h": 2,
                 "status": "todo"},
            ],
        }],
    }],
}


@pytest.fixture
def repo(tmp_path):
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    yield r
    r.close()


def _write_canon(tmp_path, canon=CANON):
    p = tmp_path / "00_demo_plan.json"
    p.write_text(json.dumps(canon, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_import_canon_idempotent_and_writeback(repo, tmp_path):
    p = _write_canon(tmp_path)
    res = import_canon(repo, p)
    assert res["tasks"] == 2 and res["created"] == 2 and res["assigned_ids"] == 1
    # назначенный ID дописан обратно в канон (sequence после максимума: DEMO-2);
    # в каноне поле зовётся plane_identifier (формат wgp), в БД — task.identifier
    canon = json.loads(p.read_text(encoding="utf-8"))
    t2 = canon["epochs"][0]["sprints"][0]["tasks"][1]
    assert t2["plane_identifier"] == "DEMO-2"
    # статусы канона смапились на статусы метрик
    by_ident = {t["identifier"]: t for t in repo.all_tasks()}
    assert by_ident["DEMO-1"]["status"] == DONE_STATE
    assert by_ident["DEMO-1"]["canon_task_id"] == "t1.1.1"
    assert by_ident["DEMO-2"]["status"] == "Todo"
    # повторный импорт ничего не создаёт и не переназначает ID
    res2 = import_canon(repo, p)
    assert res2["created"] == 0 and res2["assigned_ids"] == 0 and res2["tasks"] == 2


def test_import_canon_does_not_steal_explicit_ids(repo, tmp_path):
    """P1 двойного ревью: задача без ID раньше задачи с явным ID не должна занять её ID
    (иначе upsert молча сливает две задачи в одну и канон получает дубль)."""
    canon = {
        "project": {"slug": "demo", "plane_identifier": "DEMO"},
        "epochs": [{"id": "e1", "sprints": [{"id": "s1", "tasks": [
            {"id": "t1", "name": "без ID", "status": "todo"},
            {"id": "t2", "name": "с явным ID", "status": "todo", "plane_identifier": "DEMO-1"},
        ]}]}],
    }
    p = tmp_path / "c.json"
    p.write_text(json.dumps(canon, ensure_ascii=False), encoding="utf-8")
    res = import_canon(repo, p)
    assert res["tasks"] == 2 and res["created"] == 2  # обе живы, ничего не слилось
    out = json.loads(p.read_text(encoding="utf-8"))
    t1, t2 = out["epochs"][0]["sprints"][0]["tasks"]
    assert t2["plane_identifier"] == "DEMO-1"
    assert t1["plane_identifier"] == "DEMO-2"  # свободный, НЕ занятый DEMO-1
    assert repo.task_id_by_identifier("DEMO-1") != repo.task_id_by_identifier("DEMO-2")


def test_import_canon_keeps_registered_prefix(repo, tmp_path):
    """Канон без project.plane_identifier не должен перезатирать префикс проекта
    (иначе ID-пространство раздваивается и коммиты отвязываются от задач)."""
    repo.upsert_project("demo", identifier_prefix="TIME")
    canon = {"project": {"slug": "demo"},
             "epochs": [{"id": "e1", "sprints": [{"id": "s1", "tasks": [
                 {"id": "t1", "name": "x", "status": "todo"}]}]}]}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(canon, ensure_ascii=False), encoding="utf-8")
    import_canon(repo, p)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert out["epochs"][0]["sprints"][0]["tasks"][0]["plane_identifier"] == "TIME-1"
    assert repo.get_project("demo")["identifier_prefix"] == "TIME"


def test_import_canon_rejects_duplicate_explicit_ids(repo, tmp_path):
    """Финальное ревью (codex): дубль явного ID в каноне — ошибка ДО любых upsert,
    а не молчаливое слияние двух задач в одну."""
    canon = {"project": {"slug": "demo", "plane_identifier": "DEMO"},
             "epochs": [{"id": "e1", "sprints": [{"id": "s1", "tasks": [
                 {"id": "t1", "name": "a", "plane_identifier": "DEMO-1"},
                 {"id": "t2", "name": "b", "plane_identifier": "DEMO-1"},
             ]}]}]}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(canon, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="DEMO-1"):
        import_canon(repo, p)
    assert repo.all_tasks() == []  # ничего не записано


def test_import_canon_reuses_id_by_canon_task_id(repo, tmp_path):
    """Финальное ревью (codex P1): сбой между upsert и writeback канона — при
    ре-импорте задача без ID получает СВОЙ прежний ID по canon_task_id, а не новый."""
    pid = repo.upsert_project("demo", identifier_prefix="DEMO")
    repo.upsert_task(pid, "DEMO-7", canon_task_id="t1.1.2", title="из прошлого импорта")
    canon = {"project": {"slug": "demo", "plane_identifier": "DEMO"},
             "epochs": [{"id": "e1", "sprints": [{"id": "s1", "tasks": [
                 {"id": "t1.1.2", "name": "из прошлого импорта"},  # ID потерян writeback-сбоем
             ]}]}]}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(canon, ensure_ascii=False), encoding="utf-8")
    res = import_canon(repo, p)
    assert res["created"] == 0 and res["assigned_ids"] == 1
    out = json.loads(p.read_text(encoding="utf-8"))
    assert out["epochs"][0]["sprints"][0]["tasks"][0]["plane_identifier"] == "DEMO-7"
    assert len(repo.all_tasks()) == 1  # дубль-строка не создана


def test_reimport_does_not_reset_live_status(repo, tmp_path):
    """Ре-импорт канона (штатный replan) не откатывает статусы, которые ведёт реестр."""
    p = _write_canon(tmp_path)
    import_canon(repo, p)
    emp = repo.upsert_employee("oleg")
    transition(repo, emp, "DEMO-2", STARTED_STATE)
    import_canon(repo, p)  # в каноне DEMO-2 = todo
    by_ident = {t["identifier"]: t for t in repo.all_tasks()}
    assert by_ident["DEMO-2"]["status"] == STARTED_STATE


def test_transition_retry_heals_partial_state(repo, tmp_path):
    """Повтор с тем же --at дозаписывает недостающее: упали между transition и event —
    ретрай восстанавливает событие, не плодя дублей переходов."""
    import_canon(repo, _write_canon(tmp_path))
    emp = repo.upsert_employee("oleg")
    transition(repo, emp, "DEMO-2", STARTED_STATE, at="2026-06-11T09:00:00Z")
    repo._exec("DELETE FROM activity_event WHERE source='task'", ())  # имитация частичной записи
    transition(repo, emp, "DEMO-2", STARTED_STATE, at="2026-06-11T09:00:00Z")
    assert len(repo.all_task_transitions()) == 1
    events = repo.events_between(emp, "2026-06-11T00:00:00Z", "2026-06-11T23:59:59Z")
    assert len([e for e in events if e["source"] == "task"]) == 1


def test_add_task_generates_sequence(repo, tmp_path):
    import_canon(repo, _write_canon(tmp_path))
    ident = add_task(repo, "demo", "Новая задача", estimate_h=1.5)
    assert ident == "DEMO-3"
    assert repo.task_id_by_identifier("DEMO-3") is not None
    # префикс из slug, если проект новый
    ident2 = add_task(repo, "fresh_proj", "Первая")
    assert ident2 == "FRESHP-1"


def test_next_identifier_ignores_foreign_prefixes(repo):
    pid = repo.upsert_project("x", identifier_prefix="AAA")
    repo.upsert_task(pid, "AAA-7", title="t")
    repo.upsert_task(pid, "AAAB-99", title="другой префикс")
    assert next_identifier(repo, "AAA") == "AAA-8"


def test_transition_writes_window_and_is_idempotent(repo, tmp_path):
    import_canon(repo, _write_canon(tmp_path))
    emp = repo.upsert_employee("oleg")
    r1 = transition(repo, emp, "DEMO-2", STARTED_STATE, at="2026-06-11T09:00:00Z")
    transition(repo, emp, "DEMO-2", DONE_STATE, at="2026-06-11T11:30:00+03:00")  # офсет → UTC
    trs = repo.all_task_transitions()
    assert [(t["to_state"], t["ts_utc"]) for t in trs] == [
        (STARTED_STATE, "2026-06-11T09:00:00Z"), (DONE_STATE, "2026-06-11T08:30:00Z")]
    assert r1["task_id"] == repo.task_id_by_identifier("DEMO-2")
    # статус задачи обновился
    by_ident = {t["identifier"]: t for t in repo.all_tasks()}
    assert by_ident["DEMO-2"]["status"] == DONE_STATE
    # повтор с тем же --at не дублирует ни переход, ни событие
    transition(repo, emp, "DEMO-2", STARTED_STATE, at="2026-06-11T09:00:00Z")
    assert len(repo.all_task_transitions()) == 2
    events = repo.events_between(emp, "2026-06-11T00:00:00Z", "2026-06-11T23:59:59Z")
    assert len([e for e in events if e["source"] == "task"]) == 2


def test_transition_unknown_task_raises(repo):
    emp = repo.upsert_employee("oleg")
    with pytest.raises(ValueError):
        transition(repo, emp, "NOPE-1", STARTED_STATE)


def test_cli_transition_feeds_metrics_attribution(repo, tmp_path):
    """Сквозной: переход из CLI создаёт окно, активность внутри атрибутируется задаче."""
    import_canon(repo, _write_canon(tmp_path))
    emp = repo.upsert_employee("oleg")
    tid = repo.task_id_by_identifier("DEMO-2")
    transition(repo, emp, "DEMO-2", STARTED_STATE, at="2026-06-11T09:00:00Z")
    # активность claude внутри окна (10:00 и 10:10 UTC)
    for i, ts in enumerate(("2026-06-11T10:00:00Z", "2026-06-11T10:10:00Z")):
        repo.insert_event(emp, "claude", "message", ts, external_id=f"m{i}",
                          meta={"tokens_in": 10, "tokens_out": 5})
    transition(repo, emp, "DEMO-2", DONE_STATE, at="2026-06-11T11:00:00Z")
    compute_day(repo, emp, "2026-06-11")
    per_task = {r["task_id"]: r for r in repo.daily_task_times(emp, "2026-06-11")}
    assert tid in per_task and per_task[tid]["active_minutes"] > 0
    assert per_task[tid]["identifier"] == "DEMO-2"


def test_list_tasks_filters_and_sorts(repo, tmp_path):
    import_canon(repo, _write_canon(tmp_path))
    add_task(repo, "other", "Чужая задача")
    rows = list_tasks(repo, slug="demo")
    assert [t["identifier"] for t in rows] == ["DEMO-1", "DEMO-2"]
    rows_open = list_tasks(repo, slug="demo", open_only=True)
    assert [t["identifier"] for t in rows_open] == ["DEMO-2"]  # DEMO-1 = Done


def test_cli_task_commands_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMECHECKER_DB_PATH", str(tmp_path / "db.sqlite"))
    p = _write_canon(tmp_path)
    assert main(["task", "import", "--plan", str(p)]) == 0
    assert main(["task", "add", "--slug", "demo", "--title", "Из CLI"]) == 0
    assert main(["task", "start", "DEMO-2", "--at", "2026-06-11T09:00:00Z"]) == 0
    assert main(["task", "done", "DEMO-2"]) == 0
    assert main(["task", "list", "--slug", "demo", "--open"]) == 0
    # неизвестная задача → rc=1, не traceback
    assert main(["task", "start", "NOPE-1"]) == 1
    # несуществующий путь канона → rc=1, не traceback (OSError ловится)
    assert main(["task", "import", "--plan", str(tmp_path / "nope.json")]) == 1
