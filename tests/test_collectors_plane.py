from timechecker.collectors.plane import PlaneCollector
from timechecker.storage import SqliteRepository


class FakeClient:
    def __init__(self, issues, activities):
        self._issues = issues
        self._acts = activities

    def list_issues(self):
        return self._issues

    def issue_activities(self, issue_id):
        return self._acts.get(issue_id, [])


def _fixture():
    issues = [{"id": "i1", "sequence_id": 4, "name": "schema"}]
    acts = {"i1": [
        {"id": "a1", "field": "state", "old_value": "Backlog",
         "new_value": "In Progress", "created_at": "2026-06-09T07:00:00Z"},
        {"id": "a2", "field": "name", "old_value": "x", "new_value": "y",
         "created_at": "2026-06-09T07:30:00Z"},  # не state — пропуск
        {"id": "a3", "field": "state", "old_value": "In Progress",
         "new_value": "Done", "created_at": "2026-06-09T12:00:00Z"},
    ]}
    return issues, acts


def test_plane_collector_mirrors_and_transitions(tmp_path):
    issues, acts = _fixture()
    repo = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = repo.upsert_employee("Oleg")
    proj = repo.upsert_project("timechecker", plane_identifier="TIME")
    coll = PlaneCollector(repo, FakeClient(issues, acts), plane_identifier_prefix="TIME")
    counts = coll.collect(emp, project_id=proj)
    assert counts == {"issues": 1, "transitions": 2}  # field!=state пропущен
    assert repo.task_id_by_identifier("TIME-4") is not None  # issue → task зеркало
    # идемпотентно
    counts2 = coll.collect(emp, project_id=proj)
    assert counts2 == {"issues": 1, "transitions": 2}
    evs = repo.events_between(emp, "2026-06-09T00:00:00Z", "2026-06-09T23:59:59Z")
    assert len(evs) == 2  # без дублей переходов
    repo.close()
