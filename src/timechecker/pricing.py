"""Оценка стоимости токенов ИИ-агентов (USD): Anthropic (Claude) и OpenAI (codex/gpt).

СТАВКИ — РЕДАКТИРУЕМАЯ таблица: USD за 1M токенов (input, output, cache-write, cache-read).
Семейство модели определяется по имени (opus/sonnet/haiku, gpt-X.Y); неизвестное → дефолт
провайдера (дорогое, чтобы не недооценить). Семантика провайдеров РАЗНАЯ: у Anthropic input
БЕЗ кэша (cache_* отдельно), у OpenAI input ВКЛЮЧАЕТ cached (и нет cache-write) — см. cost_usd.
Это ОЦЕНКА (raw-токены × ставка), не счёт от вендора.
"""

from __future__ import annotations

import json
import os
import re

# USD за 1_000_000 токенов: (input, output, cache_write, cache_read). Дефолт; переопределяется
# файлом ~/.wgp/pricing.json (или TIMECHECKER_PRICING) — см. _ensure_overrides().
# gpt-дефолты запинены по LiteLLM на 2026-06-10 (cache_write у OpenAI не существует → 0).
RATES: dict[str, tuple[float, float, float, float]] = {
    "opus": (15.0, 75.0, 18.75, 1.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku": (0.80, 4.0, 1.00, 0.08),
    "gpt-5.5": (5.0, 30.0, 0.0, 0.50),
    "gpt-5": (1.25, 10.0, 0.0, 0.125),
}
_FAMILIES = ("opus", "sonnet", "haiku")
_GPT_FAMILIES = ("gpt-5.5", "gpt-5")
_GPT_RE = re.compile(r"gpt-\d+(?:\.\d+)?")  # ловит и `openai/gpt-5.5`, и `gpt-5.5-codex`
# ярлык tier выводится из семейства (отдельно не хранится)
_TIER = {"haiku": "low", "sonnet": "medium", "opus": "high"}


def model_family(model: str | None) -> str:
    """Семейство модели по имени (`claude-opus-4-…` → `opus`, `gpt-5.5-codex` → `gpt-5.5`)."""
    m = (model or "").lower()
    for fam in _FAMILIES:
        if fam in m:
            return fam
    gpt = _GPT_RE.search(m)
    if gpt:
        return gpt.group(0)
    return model or "?"


def provider(model: str | None) -> str:
    """Провайдер по семейству модели: gpt-* → openai, иначе anthropic."""
    return "openai" if model_family(model).startswith("gpt") else "anthropic"


def model_tier(model: str | None) -> str:
    """Ярлык tier: haiku→low, sonnet→medium, opus→high; gpt: mini→medium, nano→low, иначе high."""
    fam = model_family(model)
    if fam.startswith("gpt"):
        m = (model or "").lower()
        return "medium" if "mini" in m else ("low" if "nano" in m else "high")
    return _TIER.get(fam, "?")


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
             cache_creation: int = 0, cache_read: int = 0, *,
             provider_name: str | None = None) -> float:
    """Оценка стоимости (USD) по разбивке токенов и модели (ставки: дефолт + override).

    Провайдер-семантика: anthropic — ``input`` БЕЗ кэша (cache_creation/cache_read сверху);
    openai — ``input`` ВКЛЮЧАЕТ cached → платный вход = max(0, input − cache_read), cache_read
    по льготной ставке, cache_write не существует. ``provider_name`` можно передать явно
    (коллектор знает источник надёжнее, чем регэксп по имени модели).
    """
    _ensure_overrides()
    prov = provider_name or provider(model)
    fam = model_family(model)
    if prov == "openai":
        ri, ro, _rw, rr = RATES.get(fam) or RATES["gpt-5.5"]
        return (max(0, input_tokens - cache_read) * ri + output_tokens * ro
                + cache_read * rr) / 1_000_000
    ri, ro, rw, rr = RATES.get(fam) or RATES["opus"]
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

    Claude (opus/sonnet/haiku): самая дорогая (топовая) запись семейства с ценами.
    OpenAI (gpt-5.5/gpt-5): точное совпадение ключа приоритетно (иначе `startswith(fam + "-")`
    схватил бы дорогой pro-вариант); кэш-критерий — только cache_read (cache_creation у OpenAI
    в датасете нет). Сетевой/парс-сбой бросает исключение — вызывающий сохраняет текущие ставки.
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
    for fam in _GPT_FAMILIES:
        best = None
        best_score = ()
        for key, v in data.items():
            k = str(key).lower()
            if not isinstance(v, dict) or (k != fam and not k.startswith(fam + "-")):
                continue
            ic, oc = v.get("input_cost_per_token"), v.get("output_cost_per_token")
            if ic is None or oc is None:
                continue
            cr = v.get("cache_read_input_token_cost")
            # точное совпадение бьёт варианты (pro/mini/…), кэш-несущие — без кэша; хвост —
            # max по output («не недооценить»: без точного ключа лучше pro, чем nano)
            score = (k == fam, cr is not None, float(oc))
            if best is None or score > best_score:
                best_score = score
                best = (float(ic) * million, float(oc) * million, 0.0,
                        float(cr or 0) * million)
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
