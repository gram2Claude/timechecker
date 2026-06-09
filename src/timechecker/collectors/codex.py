"""Коллектор сессий OpenAI Codex CLI (E8, codex_usage).

Парсит ``~/.codex/sessions/<ГГГГ>/<ММ>/<ДД>/rollout-*.jsonl`` → ОДНО событие на сессию
(гранулярность «итог сессии», согласовано спекой 08) + строка ``agent_session``
(``source="codex"``). **Только метаданные**: id/время/cwd/модель/usage — тела
(``response_item``, инструкции) не читаются и не хранятся.

Семантика usage (проверено по живым логам): ``total_token_usage`` накопительный (последний =
итог сессии); ``input_tokens`` ВКЛЮЧАЕТ ``cached_input_tokens`` (OpenAI), а
``reasoning_output_tokens`` уже ВХОДИТ в ``output_tokens`` — суммировать нельзя (задвоение).
Сессии без ``token_count`` или без id пропускаются (нет usage / нет ключа идемпотентности).
Событие ставится на ``started_at`` (стабильно между пересборами; ON CONFLICT обновляет meta,
не ts) — сессия через полночь целиком ложится на день старта (известное ограничение).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_CODEX_SINCE = "2026-06-01"
# дешёвый фильтр по дате каталога пропускает и недавние ПРОШЛЫЕ дни: сессия, начатая до окна,
# но ещё активная в нём, должна дозреть (повторный collect обновляет meta события)
_PATH_MARGIN_DAYS = 3


def _ts(value: str | None) -> datetime | None:
    """ISO-таймстемп (Z/офсет/только дата) → aware UTC; битый/пустой → None.

    Сравнение строк лексикографически ловит ловушку `08:00:00.100Z < 08:00:00Z`
    (точка сортируется раньше Z) — поэтому сравниваем только распарсенные значения.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@dataclass
class CodexSession:
    """Метаданные одной сессии codex (итог по последнему token_count)."""

    session_uid: str
    started_at: str
    ended_at: str
    cwd: str | None
    model: str | None
    turns: int
    input_tokens: int
    cached_input: int
    output_tokens: int
    reasoning: int


def _iter_json_lines(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # живой файл: обрезанная/битая строка — пропускаем
    return out


def parse_rollout(path: Path) -> CodexSession | None:
    """Распарсить один rollout-файл в итог сессии. None — сессия без usage или без id."""
    sid: str | None = None
    started: str | None = None
    last_ts: str | None = None
    cwd: str | None = None
    model: str | None = None
    turns = 0
    usage: dict | None = None
    for o in _iter_json_lines(path):
        typ = o.get("type")
        payload = o.get("payload") or {}
        if typ == "session_meta":
            sid = payload.get("id")
            started = payload.get("timestamp") or o.get("timestamp")
            cwd = payload.get("cwd")
        elif typ == "turn_context":
            model = payload.get("model") or model
            cwd = payload.get("cwd") or cwd
        elif typ == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            total = info.get("total_token_usage")
            if isinstance(total, dict):
                usage = total  # накопительный: последний = итог сессии
                turns += 1
                last_ts = o.get("timestamp") or last_ts
    if not (sid and started and usage):
        return None
    return CodexSession(
        session_uid=sid, started_at=started, ended_at=last_ts or started,
        cwd=cwd, model=model, turns=turns,
        input_tokens=int(usage.get("input_tokens") or 0),
        cached_input=int(usage.get("cached_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        reasoning=int(usage.get("reasoning_output_tokens") or 0),
    )


def iter_sessions(sessions_dir: Path, *, since: str | None = None) -> list[CodexSession]:
    """Все сессии под ``sessions_dir`` (структура ГГГГ/ММ/ДД), активные начиная с ``since``.

    Сессия включается, если она НАЧАЛАСЬ или ЗАКОНЧИЛАСЬ в окне (вторая часть — «дозревание»
    длинных сессий: collect повторно подхватывает выросший rollout и апсертит итог).
    Таймстемпы парсятся (не строковое сравнение). Фильтр по дате каталога — с запасом
    ``_PATH_MARGIN_DAYS`` (более старые длинные сессии перестают дозревать — RUNBOOK).
    """
    out: list[CodexSession] = []
    if not sessions_dir.exists():
        return out
    since_dt = _ts(since)
    path_floor = ((since_dt - timedelta(days=_PATH_MARGIN_DAYS)).date().isoformat()
                  if since_dt else "")
    for f in sorted(sessions_dir.rglob("rollout-*.jsonl")):
        try:  # дешёвый фильтр по ГГГГ/ММ/ДД в пути — не читая файл
            y, m, d = f.parent.parts[-3], f.parent.parts[-2], f.parent.parts[-1]
            if path_floor and f"{y}-{m}-{d}" < path_floor:
                continue
        except IndexError:
            pass  # нестандартный путь — парсим без фильтра
        s = parse_rollout(f)
        if s is None:
            continue
        if since_dt is not None:
            started, ended = _ts(s.started_at), _ts(s.ended_at)
            in_window = ((started is None or started >= since_dt)
                         or (ended is not None and ended >= since_dt))
            if not in_window:
                continue
        out.append(s)
    return out


def make_cwd_resolver(repo: Any, projects: list[dict]) -> Callable[[str | None], int | None]:
    """Резолвер ``cwd → project_id`` по реестру (``repo_dir``): равенство или вложенность
    (boundary-aware, ``C:\\repo`` ≠ ``C:\\repo-old``), при нескольких — длиннейший префикс;
    ``upsert_project`` лениво, с кэшем. Нерезолвленный cwd → None."""
    dirs: list[tuple[Path, str]] = []
    for p in projects:
        rd = p.get("repo_dir")
        if rd:
            try:
                dirs.append((Path(rd).resolve(), p["slug"]))
            except OSError:
                continue
    cache: dict[str, int | None] = {}

    def resolve(cwd: str | None) -> int | None:
        if not cwd:
            return None
        if cwd in cache:
            return cache[cwd]
        try:
            c = Path(cwd).resolve()
        except OSError:
            cache[cwd] = None
            return None
        best: tuple[int, str] | None = None  # (длина префикса, slug)
        for d, slug in dirs:
            try:
                ok = str(c).casefold() == str(d).casefold() or c.is_relative_to(d)
            except (OSError, ValueError):
                ok = False
            if ok and (best is None or len(str(d)) > best[0]):
                best = (len(str(d)), slug)
        cache[cwd] = repo.upsert_project(best[1]) if best else None
        return cache[cwd]

    return resolve


class CodexCollector:
    """Парсит rollout-логи codex и пишет событие/сессию в репозиторий (идемпотентно)."""

    def __init__(self, repo: Any, sessions_dir: Path,
                 codex_since: str = DEFAULT_CODEX_SINCE) -> None:
        self.repo = repo
        self.sessions_dir = Path(sessions_dir)
        self.codex_since = codex_since

    def collect(self, employee_id: int, *, since: str | None = None,
                project_resolver: Callable[[str | None], int | None] | None = None,
                ingest_run_id: int | None = None) -> dict:
        effective_since = max(since or "", self.codex_since) or None
        sessions = iter_sessions(self.sessions_dir, since=effective_since)
        resolve = project_resolver or (lambda _c: None)
        for s in sessions:
            project_id = resolve(s.cwd)
            self.repo.insert_event(
                employee_id, "codex", "session", s.started_at,
                project_id=project_id, external_id=s.session_uid,
                meta={"input": s.input_tokens, "cached_input": s.cached_input,
                      "output": s.output_tokens, "reasoning": s.reasoning,
                      "model": s.model, "turns": s.turns},
                ingest_run_id=ingest_run_id,
            )
            self.repo.upsert_agent_session(
                employee_id, "codex", s.session_uid, project_id=project_id,
                started_at=s.started_at, ended_at=s.ended_at,
                message_count=s.turns,
                tokens_in=s.input_tokens,
                tokens_out=s.output_tokens,  # reasoning уже внутри output
                cache_read=s.cached_input, cache_creation=0,
                model=s.model,
            )
        return {"codex_sessions": len(sessions)}
