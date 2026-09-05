from __future__ import annotations

import json
from pathlib import Path

from .domain import Episode, ShotGroup


def write_report(path: str | Path, episodes: list[Episode], groups: list[ShotGroup], issues: list[str], parameters: dict | None = None, setting_library: dict | None = None, asset_coverage: dict | None = None, text_agent: dict | None = None, queue_status_counts: dict[str, int] | None = None, warnings: list[str] | None = None, manual_actions: list[dict] | None = None) -> None:
    coverage = asset_coverage or {}
    payload = {
        "episodes": len(episodes),
        "shots": sum(len(e.shots) for e in episodes),
        "source_seconds": sum(s.duration for e in episodes for s in e.shots),
        "groups": len(groups),
        "ready_groups": sum(g.status == "ready" for g in groups),
        "manual_groups": sum(g.status == "manual" for g in groups),
        "queue_status_counts": queue_status_counts or {},
        "parameters": parameters or {"model": "Seedance 2.0 Fast", "duration_seconds": 10, "aspect_ratio": "16:9"},
        "text_agent": text_agent or {"provider": "codex", "model": "gpt-5.6-terra", "reasoning_effort": "high", "mode": "exception_only"},
        "setting_library": setting_library or {},
        "asset_coverage": coverage,
        "issues": issues,
        "fatal_errors": issues,
        "warnings": warnings or [],
        "manual_actions": manual_actions or [],
        "unresolved_assets": coverage.get("unresolved", []),
        "group_plan": [
            {"task_id": g.task_id, "episode": g.episode, "shots": [s.number for s in g.shots], "original_seconds": g.original_seconds, "planned_seconds": g.planned_seconds, "duration_map": g.duration_map, "status": g.status, "adjustment": g.adjustment}
            for g in groups
        ],
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
