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
    f = tmp_path / "pricing.json"
    f.write_text(json.dumps({"opus": [1, 2, 3, 4]}), encoding="utf-8")
    monkeypatch.setenv("TIMECHECKER_PRICING", str(f))
    pr._loaded = False
    try:
        assert round(cost_usd("claude-opus-4-8", 1_000_000, 1_000_000), 2) == 3.0  # 1 + 2
    finally:
        pr._loaded = False  # не влиять на другие тесты
