from timechecker.pricing import cost_usd, model_family, model_tier


def test_model_family_and_tier():
    assert model_family("claude-opus-4-8") == "opus"
    assert model_family("claude-sonnet-4-6") == "sonnet"
    assert model_family("claude-haiku-4-5") == "haiku"
    assert model_family(None) == "?"
    assert model_tier("claude-opus-4-8") == "high"
    assert model_tier("claude-sonnet-4-6") == "medium"
    assert model_tier("claude-haiku-4-5") == "low"


def test_cost_usd(monkeypatch):
    # принудительно дефолтные ставки (игнорируем возможный ~/.wgp/pricing.json)
    monkeypatch.setenv("TIMECHECKER_PRICING", "/nonexistent/pricing.json")
    import timechecker.pricing as pr
    pr._loaded = False

    # opus: input $15 + output $75 за 1M → $90
    assert round(cost_usd("claude-opus-4-8", 1_000_000, 1_000_000), 2) == 90.0
    # cache: write $18.75 + read $1.50 за 1M
    assert round(cost_usd("claude-opus-4-8", 0, 0, 1_000_000, 1_000_000), 2) == 20.25
    # sonnet дешевле opus
    assert cost_usd("claude-sonnet-4-6", 1_000_000, 0) < cost_usd("claude-opus-4-8", 1_000_000, 0)
    # неизвестная модель → дефолт (opus)
    assert cost_usd("mystery", 1_000_000, 0) == cost_usd("claude-opus-4-8", 1_000_000, 0)


def test_pricing_override(monkeypatch, tmp_path):
    import json

    import timechecker.pricing as pr
    saved = dict(pr.RATES)
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps({"opus": [1, 2, 3, 4]}), encoding="utf-8")
    monkeypatch.setenv("TIMECHECKER_PRICING", str(f))
    pr._loaded = False
    try:
        assert round(cost_usd("claude-opus-4-8", 1_000_000, 1_000_000), 2) == 3.0  # 1 + 2
    finally:
        pr.RATES.clear()
        pr.RATES.update(saved)
        pr._loaded = False


def test_refresh_rates(tmp_path):
    import json

    import timechecker.pricing as pr
    from timechecker.pricing import refresh_rates
    data = {
        "claude-opus-4-1": {"input_cost_per_token": 0.000015, "output_cost_per_token": 0.000075,
                            "cache_creation_input_token_cost": 0.00001875,
                            "cache_read_input_token_cost": 0.0000015},
        "claude-sonnet-4-5": {"input_cost_per_token": 0.000003, "output_cost_per_token": 0.000015},
        "gpt-4o": {"input_cost_per_token": 0.0000025, "output_cost_per_token": 0.00001},
        "bad": "не словарь",
    }
    out = tmp_path / "pricing.json"
    saved = dict(pr.RATES)
    try:
        new = refresh_rates(data=data, write_path=str(out))
    finally:
        pr.RATES.clear()
        pr.RATES.update(saved)
        pr._loaded = False
    assert round(new["opus"][0], 2) == 15.0 and round(new["opus"][1], 2) == 75.0
    assert round(new["opus"][3], 2) == 1.5  # cache_read за 1M
    assert "sonnet" in new and "haiku" not in new  # haiku нет в датасете → не трогаем
    saved_json = json.loads(out.read_text(encoding="utf-8"))
    assert "opus" in saved_json and "gpt-4o" not in saved_json  # только Claude-семейства
