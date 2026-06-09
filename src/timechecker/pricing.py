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
    if not isinstance(data, dict):
        return
    for k, v in data.items():
        try:  # битую запись пропускаем, не роняя расчёт
            if isinstance(v, (list, tuple)) and len(v) == 4:
                RATES[str(k).lower()] = tuple(float(x) for x in v)
        except (ValueError, TypeError):
            continue


def cost_usd(model: str | None, input_tokens: int, output_tokens: int,
             cache_creation: int = 0, cache_read: int = 0) -> float:
    """Оценка стоимости (USD) по разбивке токенов и модели (ставки: дефолт + override)."""
    _ensure_overrides()
    ri, ro, rw, rr = RATES.get(model_family(model)) or RATES["opus"]
    return (input_tokens * ri + output_tokens * ro
            + cache_creation * rw + cache_read * rr) / 1_000_000


LITELLM_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
               "model_prices_and_context_window.json")


def _fetch_json(url: str, timeout: int = 20) -> dict:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "timechecker"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def refresh_rates(*, data: dict | None = None, url: str | None = None,
                  write_path: str | None = None) -> dict:
    """Обновить ставки из датасета LiteLLM (per-token → per-1M) и записать override-файл.

    Для opus/sonnet/haiku берёт самую дорогую (топовую) Claude-запись с ценами. Сетевой/парс-сбой
    бросает исключение — вызывающий сохраняет текущие ставки. Возвращает новые ставки по семействам.
    """
    if data is None:
        data = _fetch_json(url or LITELLM_URL)
    if not isinstance(data, dict):
        raise ValueError("датасет цен — не словарь")
    million = 1_000_000
    new: dict[str, tuple[float, float, float, float]] = {}
    for fam in _FAMILIES:
        best: tuple[float, float, float, float] | None = None
        best_score: tuple = ()
        for key, v in data.items():
            k = str(key).lower()
            if not isinstance(v, dict) or "claude" not in k or fam not in k:
                continue
            ic, oc = v.get("input_cost_per_token"), v.get("output_cost_per_token")
            if ic is None or oc is None:
                continue
            cw = v.get("cache_creation_input_token_cost")
            cr = v.get("cache_read_input_token_cost")
            has_cache = cw is not None and cr is not None
            # предпочесть записи С кэш-ставками и прямой тариф (без provider-префикса), затем
            # макс по output — иначе можно схватить markup-вариант без кэша
            score = (has_cache, "/" not in k, float(oc))
            if best is None or score > best_score:
                best_score = score
                best = (float(ic) * million, float(oc) * million,
                        float(cw or 0) * million, float(cr or 0) * million)
        if best is not None:
            new[fam] = best
    if not new:
        raise ValueError("в датасете не найдено ставок Claude")
    RATES.update(new)
    path = write_path or os.path.expanduser("~/.wgp/pricing.json")
    existing: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh)
    except (OSError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    for fam, tup in new.items():
        existing[fam] = [round(x, 6) for x in tup]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, ensure_ascii=False, indent=2)
    return new
