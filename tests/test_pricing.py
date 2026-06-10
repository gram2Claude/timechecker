from timechecker.pricing import cost_usd, model_family, model_tier, provider


def test_model_family_and_tier():
    assert model_family("claude-opus-4-8") == "opus"
    assert model_family("claude-sonnet-4-6") == "sonnet"
    assert model_family("claude-haiku-4-5") == "haiku"
    assert model_family(None) == "?"
    assert model_tier("claude-opus-4-8") == "high"
    assert model_tier("claude-sonnet-4-6") == "medium"
    assert model_tier("claude-haiku-4-5") == "low"


def test_model_family_and_tier_gpt():
    assert model_family("gpt-5.5") == "gpt-5.5"
    assert model_family("gpt-5.5-codex") == "gpt-5.5"
    assert model_family("openai/gpt-5.5") == "gpt-5.5"  # provider-префикс
    assert model_family("gpt-5-mini") == "gpt-5"
    assert provider("gpt-5.5") == "openai"
    assert provider("claude-opus-4-8") == "anthropic"
    assert model_tier("gpt-5.5") == "high"
    assert model_tier("gpt-5-mini") == "medium"
    assert model_tier("gpt-5-nano") == "low"


def test_cost_usd_openai():
    import timechecker.pricing as pr
    saved = dict(pr.RATES)
    pr.RATES.update({"gpt-5.5": (5.0, 30.0, 0.0, 0.50)})
    pr._loaded = True
    try:
        # OpenAI: input ВКЛЮЧАЕТ cached → (1M−400k)·5 + 400k·0.5 + 100k·30 = $6.20
        assert abs(cost_usd("gpt-5.5", 1_000_000, 100_000, 0, 400_000) - 6.20) < 1e-9
        # явный провайдер (движок передаёт по source) — тот же результат
        assert abs(cost_usd("gpt-5.5", 1_000_000, 100_000, 0, 400_000,
                            provider_name="openai") - 6.20) < 1e-9
        # битые данные: cached > input → платный вход клампится к 0, не уходит в минус
        assert cost_usd("gpt-5.5", 100, 0, 0, 500) == 500 * 0.5 / 1_000_000
        # неизвестная gpt-модель → фолбэк на gpt-5.5
        assert (cost_usd("gpt-9-experimental", 1000, 0, provider_name="openai")
                == cost_usd("gpt-5.5", 1000, 0))
    finally:
        pr.RATES.clear()
        pr.RATES.update(saved)
        pr._loaded = False


def test_cost_usd():
    import timechecker.pricing as pr
    saved = dict(pr.RATES)
    # фиксируем известные ставки (изоляция от реального ~/.wgp/pricing.json)
    pr.RATES.update({"opus": (15.0, 75.0, 18.75, 1.50), "sonnet": (3.0, 15.0, 3.75, 0.30),
                     "haiku": (0.80, 4.0, 1.00, 0.08)})
    pr._loaded = True  # не подгружать override
    try:
        # opus: input $15 + output $75 за 1M → $90
        assert round(cost_usd("claude-opus-4-8", 1_000_000, 1_000_000), 2) == 90.0
        # cache: write $18.75 + read $1.50 за 1M
        assert round(cost_usd("claude-opus-4-8", 0, 0, 1_000_000, 1_000_000), 2) == 20.25
        # sonnet дешевле opus
        assert (cost_usd("claude-sonnet-4-6", 1_000_000, 0)
                < cost_usd("claude-opus-4-8", 1_000_000, 0))
        # неизвестная модель → дефолт (opus)
        assert cost_usd("mystery", 1_000_000, 0) == cost_usd("claude-opus-4-8", 1_000_000, 0)
    finally:
        pr.RATES.clear()
        pr.RATES.update(saved)
        pr._loaded = False


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
        # прямой Anthropic с кэшем (должен победить)
        "claude-opus-4-1": {"input_cost_per_token": 0.000015, "output_cost_per_token": 0.000075,
                            "cache_creation_input_token_cost": 0.00001875,
                            "cache_read_input_token_cost": 0.0000015},
        # markup-вариант без кэша + выше output — НЕ должен победить
        "bedrock/anthropic.claude-opus-4": {"input_cost_per_token": 0.0000165,
                                            "output_cost_per_token": 0.0000825},
        "claude-sonnet-4-5": {"input_cost_per_token": 0.000003, "output_cost_per_token": 0.000015},
        "gpt-4o": {"input_cost_per_token": 0.0000025, "output_cost_per_token": 0.00001},
        # OpenAI: у точной записи НЕТ cache_creation (его не бывает) — has_cache по cache_read;
        # дорогой pro-вариант НЕ должен победить точное совпадение ключа
        "gpt-5.5": {"input_cost_per_token": 0.000005, "output_cost_per_token": 0.00003,
                    "cache_read_input_token_cost": 0.0000005},
        "gpt-5.5-pro": {"input_cost_per_token": 0.00003, "output_cost_per_token": 0.00018,
                        "cache_read_input_token_cost": 0.000003},
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
    # выбрана кэш-несущая прямая запись (75), а не markup без кэша (82.5)
    assert round(new["opus"][0], 2) == 15.0 and round(new["opus"][1], 2) == 75.0
    assert round(new["opus"][3], 2) == 1.5  # cache_read за 1M
    assert "sonnet" in new and "haiku" not in new  # haiku нет в датасете → не трогаем
    # gpt: точный ключ победил pro-вариант; cache_write=0, cache_read взят
    assert new["gpt-5.5"] == (5.0, 30.0, 0.0, 0.5)
    assert "gpt-5" not in new  # записей семейства gpt-5 (вне 5.5) в датасете нет
    saved_json = json.loads(out.read_text(encoding="utf-8"))
    assert "opus" in saved_json and "gpt-5.5" in saved_json
    assert "gpt-4o" not in saved_json  # не отслеживаемое семейство


def test_refresh_rates_rejects_insane(tmp_path):
    """Отравленный датасет: абсурдные/отрицательные ставки отбраковываются (sanity)."""
    import timechecker.pricing as pr
    from timechecker.pricing import refresh_rates
    data = {
        "claude-opus-4-1": {"input_cost_per_token": 1.0,  # 1e6/1M — выше потолка
                            "output_cost_per_token": 0.000075},
        "claude-sonnet-4-5": {"input_cost_per_token": 0.000003,  # валидная
                              "output_cost_per_token": 0.000015},
        "claude-haiku-4": {"input_cost_per_token": -0.0000008,  # отрицательная
                           "output_cost_per_token": 0.000004},
    }
    saved = dict(pr.RATES)
    try:
        new = refresh_rates(data=data, write_path=str(tmp_path / "p.json"))
    finally:
        pr.RATES.clear()
        pr.RATES.update(saved)
        pr._loaded = False
    assert "opus" not in new   # абсурдно высокая — отброшена
    assert "haiku" not in new  # отрицательная — отброшена
    assert "sonnet" in new     # валидная — принята
