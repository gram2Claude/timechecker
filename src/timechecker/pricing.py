"""Оценка стоимости токенов Claude (USD).

СТАВКИ — РЕДАКТИРУЕМАЯ таблица: USD за 1M токенов (input, output, cache-write, cache-read).
Проверяй/обновляй под актуальный тариф Anthropic. Семейство модели определяется по подстроке имени
(opus/sonnet/haiku); неизвестное → дефолт (самая дорогая, чтобы не недооценить стоимость).
Это ОЦЕНКА (raw-токены × ставка), не счёт от вендора.
"""

from __future__ import annotations

import json
import os

# USD за 1_000_000 токенов: (input, output, cache_write, cache_read). Дефолт; переопределяется
# файлом ~/.wgp/pricing.json (или TIMECHECKER_PRICING) — см. _ensure_overrides().
RATES: dict[str, tuple[float, float, float, float]] = {
    "opus": (15.0, 75.0, 18.75, 1.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku": (0.80, 4.0, 1.00, 0.08),
}
_FAMILIES = ("opus", "sonnet", "haiku")
# ярлык tier выводится из семейства (отдельно не хранится)
_TIER = {"haiku": "low", "sonnet": "medium", "opus": "high"}


def model_family(model: str | None) -> str:
    """Семейство модели по имени транскрипта (`claude-opus-4-…` → `opus`)."""
    m = (model or "").lower()
    for fam in _FAMILIES:
        if fam in m:
            return fam
    return model or "?"


def model_tier(model: str | None) -> str:
    """Ярлык tier по семейству: haiku→low, sonnet→medium, opus→high."""
    return _TIER.get(model_family(model), "?")


_loaded = False


def _ensure_overrides() -> None:
    """Подхватить override ставок из ~/.wgp/pricing.json (или env TIMECHECKER_PRICING).

    Формат: ``{"opus": [input, output, cache_write, cache_read], ...}``. Позволяет обновить тариф
    БЕЗ правки кода/редеплоя — отредактировал файл, следующий расчёт берёт новые ставки.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = os.environ.get("TIMECHECKER_PRICING") or os.path.expanduser("~/.wgp/pricing.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    for k, v in data.items():
        if isinstance(v, (list, tuple)) and len(v) == 4:
            RATES[k.lower()] = tuple(float(x) for x in v)


def cost_usd(model: str | None, input_tokens: int, output_tokens: int,
             cache_creation: int = 0, cache_read: int = 0) -> float:
    """Оценка стоимости (USD) по разбивке токенов и модели (ставки: дефолт + override)."""
    _ensure_overrides()
    ri, ro, rw, rr = RATES.get(model_family(model)) or RATES["opus"]
    return (input_tokens * ri + output_tokens * ro
            + cache_creation * rw + cache_read * rr) / 1_000_000
