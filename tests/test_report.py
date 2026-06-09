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
        claude_tokens=50000, commits=2, hygiene_score=1.0,
        claude_cache_read=900000, claude_cache_creation=12000, claude_cost_usd=4.25, models="opus")
    r.upsert_daily_task_time(emp, "2026-06-09", t1, active_minutes=300, claude_messages=120,
                             claude_tokens=50000, commits=2, est_h=4.0,
                             claude_cache_read=900000, claude_cache_creation=12000,
                             claude_cost_usd=4.25)
    r.insert_daily_idle(emp, "2026-06-09", "2026-06-09T10:00:00Z", "2026-06-09T10:50:00Z", 50)

    rep = build_daily_report(r, emp, "2026-06-09")
    md = rep["markdown"]
    assert "Отчёт за 2026-06-09" in md
    assert "TIME-1" in md and "schema" in md
    assert "Простои ≥30 мин" in md
    assert "гигиена" in md
    assert "5ч 0м" in md  # 300 мин активно
    assert "$4.25" in md  # стоимость токенов
    assert "кэш: 900000 чит." in md  # кэш-токены отдельно
    assert "opus (high)" in md  # модель + tier-ярлык
    assert "API-эквивалент" in md  # стоимость помечена как API-эквивалент, а не реальный счёт
    assert report_html(md).startswith("<pre>")
    r.close()


def test_report_empty_day(tmp_path):
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = r.upsert_employee("Oleg")
    rep = build_daily_report(r, emp, "2026-06-09")
    assert "Нет данных" in rep["markdown"]
    r.close()
