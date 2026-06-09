from timechecker.reporting import build_daily_report, report_html
from timechecker.storage import SqliteRepository


def test_build_daily_report(tmp_path):
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = r.upsert_employee("Oleg")
    proj = r.upsert_project("timechecker", plane_identifier="TIME")
    t1 = r.upsert_task(proj, "TIME-1", title="schema", estimate_h=4.0)
    r.upsert_daily_summary(
        emp, "2026-06-09", span_start="2026-06-09T06:00:00Z", span_end="2026-06-09T15:00:00Z",
        active_minutes=300, gap_minutes=60, idle_ge30_count=1, idle_ge30_minutes=50,
        tasks_count=1, switches=3, longest_focus_min=90, claude_messages=120,
        claude_tokens=50000, commits=2, hygiene_score=1.0)
    r.upsert_daily_task_time(emp, "2026-06-09", t1, active_minutes=300, claude_messages=120,
                             claude_tokens=50000, commits=2, est_h=4.0)
    r.insert_daily_idle(emp, "2026-06-09", "2026-06-09T10:00:00Z", "2026-06-09T10:50:00Z", 50)

    rep = build_daily_report(r, emp, "2026-06-09")
    md = rep["markdown"]
    assert "Отчёт за 2026-06-09" in md
    assert "TIME-1" in md and "schema" in md
    assert "Простои ≥30 мин" in md
    assert "Гигиена процесса" in md
    assert "5ч 0м" in md  # 300 мин активно
    assert "work_date,task,active_minutes" in rep["csv"]
    assert "TIME-1" in rep["csv"]
    assert report_html(md).startswith("<pre>")
    r.close()


def test_report_empty_day(tmp_path):
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = r.upsert_employee("Oleg")
    rep = build_daily_report(r, emp, "2026-06-09")
    assert "Нет данных" in rep["markdown"]
    r.close()
