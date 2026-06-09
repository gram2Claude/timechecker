"""Plane-коллектор (TIME-13/14): issues + переходы статусов → task (зеркало) + plane_transition.

``PlaneHttpClient`` ходит в Plane REST (urllib, без сторонних зависимостей). ``PlaneCollector``
зеркалит issue в таблицу task и пишет переходы статусов (field=state) как plane_transition
плюс события. Клиент инъектируется — в тестах используется фейковый (без сети).
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Protocol


class PlaneClient(Protocol):
    def list_issues(self) -> list[dict]: ...
    def issue_activities(self, issue_id: str) -> list[dict]: ...


class PlaneHttpClient:
    """Минимальный REST-клиент Plane Cloud (X-API-Key)."""

    def __init__(self, base_url: str, api_key: str, workspace: str, project_id: str) -> None:
        self.base = (base_url or "https://api.plane.so").rstrip("/")
        self.key = api_key
        self.ws = workspace
        self.pid = project_id

    def _get(self, path: str) -> Any:
        url = f"{self.base}/api/v1/workspaces/{self.ws}{path}"
        req = urllib.request.Request(  # noqa: S310 (доверенный Plane API)
            url, headers={"X-API-Key": self.key, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("results", data) if isinstance(data, dict) else data

    def list_issues(self) -> list[dict]:
        return self._get(f"/projects/{self.pid}/issues/") or []

    def issue_activities(self, issue_id: str) -> list[dict]:
        return self._get(f"/projects/{self.pid}/issues/{issue_id}/activities/") or []


class PlaneCollector:
    """Зеркалит issues в task и пишет переходы статусов (идемпотентно)."""

    def __init__(self, repo: Any, client: PlaneClient, *, plane_identifier_prefix: str) -> None:
        self.repo = repo
        self.client = client
        self.prefix = plane_identifier_prefix

    def collect(self, employee_id: int, *, project_id: int | None = None,
                ingest_run_id: int | None = None) -> dict:
        issues = self.client.list_issues()
        transitions = 0
        for iss in issues:
            seq = iss.get("sequence_id")
            ident = f"{self.prefix}-{seq}" if seq is not None else None
            if ident is None or project_id is None:
                continue
            tid = self.repo.upsert_task(
                project_id, ident, plane_issue_id=iss.get("id"), title=iss.get("name"),
            )
            for act in self.client.issue_activities(iss["id"]):
                if act.get("field") != "state":
                    continue
                self.repo.insert_plane_transition(
                    tid, from_state=act.get("old_value"), to_state=act.get("new_value"),
                    ts_utc=act.get("created_at"), external_id=act.get("id"),
                )
                self.repo.insert_event(
                    employee_id, "plane", "status_change", act.get("created_at"),
                    task_id=tid, project_id=project_id, external_id=act.get("id"),
                    meta={"to": act.get("new_value")}, ingest_run_id=ingest_run_id,
                )
                transitions += 1
        return {"issues": len(issues), "transitions": transitions}
