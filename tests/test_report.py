from timechecker.reporting import build_daily_report
from timechecker.storage import SqliteRepository


def test_build_daily_report(tmp_path):
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = r.upsert_employee("Oleg")
    proj = r.upsert_project("timechecker", identifier_prefix="TIME")
    t1 = r.upsert_task(proj, "TIME-1", title="schema", estimate_h=4.0)
    t2 = r.upsert_task(proj, "TIME-2", title="codex-only")
    r.upsert_daily_summary(
        emp, "2026-06-09", span_start="2026-06-09T06:00:00Z", span_end="2026-06-09T15:00:00Z",
        active_minutes=300, gap_minutes=60, idle_ge30_count=1, idle_ge30_minutes=50,
        tasks_count=2, switches=3, longest_focus_min=90, commits=2, hygiene_score=1.0,
        models="opus, gpt-5.5")
    r.upsert_daily_task_time(emp, "2026-06-09", t1, active_minutes=300, commits=2, est_h=4.0)
    r.upsert_daily_task_time(emp, "2026-06-09", t2, active_minutes=0)  # usage-only задача
    r.insert_daily_agent_usage(emp, "2026-06-09", t1, "claude", messages=120, tokens=50000,
                               cache_read=900000, cache_creation=12000, cost_usd=4.25)
    r.insert_daily_agent_usage(emp, "2026-06-09", t2, "codex", messages=7, tokens=20000,
                               cache_read=6000, cost_usd=0.55)
    r.insert_daily_agent_usage(emp, "2026-06-09", None, "codex", messages=3, tokens=10000,
                               cost_usd=0.20)
    r.insert_daily_idle(emp, "2026-06-09", "2026-06-09T10:00:00Z", "2026-06-09T10:50:00Z", 50)

    rep = build_daily_report(r, emp, "2026-06-09")
    md = rep["markdown"]
    assert "Отчёт за 2026-06-09" in md
    assert "TIME-1" in md and "schema" in md
    assert "TIME-2" in md  # usage-only задача в таблице
    assert "Простои ≥30 мин" in md
    assert "гигиена" in md
    assert "5ч 0м" in md  # 300 мин активно
    assert "$4.25" in md  # стоимость Claude
    assert "кэш: 900000 чит." in md  # кэш-токены отдельно
    assert "opus (high)" in md and "gpt-5.5 (high)" in md  # модели обоих агентов + tier
    assert "**codex:** 10 ходов, 30000 токенов" in md  # задачная + неатрибутированная строки
    assert "Всего ИИ:" in md and "$5.00" in md  # 4.25 + 0.55 + 0.20
    assert "API-эквивалент" in md  # стоимость помечена как API-эквивалент, а не реальный счёт
    r.close()


def test_report_claude_only_no_total(tmp_path):
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = r.upsert_employee("Oleg")
    r.upsert_daily_summary(emp, "2026-06-09", active_minutes=10, tasks_count=0)
    r.insert_daily_agent_usage(emp, "2026-06-09", None, "claude",
                               messages=5, tokens=100, cost_usd=0.01)
    md = build_daily_report(r, emp, "2026-06-09")["markdown"]
    assert "**Claude:**" in md
    assert "Всего ИИ" not in md  # один источник — итог не дублируем
    r.close()


def test_report_empty_day(tmp_path):
    r = SqliteRepository.open(tmp_path / "db.sqlite")
    emp = r.upsert_employee("Oleg")
    rep = build_daily_report(r, emp, "2026-06-09")
    assert "Нет данных" in rep["markdown"]
    r.close()
