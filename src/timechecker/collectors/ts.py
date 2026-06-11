"""Таймстемпы коллекторов (TIME-66): канонический формат хранения + parse-сравнение.

Контракт хранения: ``ts_utc`` в БД — UTC секундной точности ``%Y-%m-%dT%H:%M:%SZ``
(как git-коллектор с E7.1). Лексикографика ISO-строк ненадёжна между форматами
(офсеты: ``23:50+03:00`` < ``22:00Z`` по строке, позже по времени; доли: ``.100Z`` < ``Z``),
а `events_between` фильтрует окно дня строковым SQL — поэтому нормализуем на ЗАПИСИ,
а сравнения в памяти ведём по распарсенному datetime.
"""

from __future__ import annotations

from datetime import UTC, datetime

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def ts_key(ts: str) -> datetime:
    """ISO-ts → aware datetime для сравнения. Naive трактуем как UTC.

    Бросает ValueError/TypeError на мусоре — вызывающий валидирует на своей границе.
    """
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_utc_z(ts: str) -> str:
    """Нормализовать ISO-ts к каноническому UTC ``...Z`` секундной точности (формат хранения)."""
    return ts_key(ts).astimezone(UTC).strftime(TS_FMT)
