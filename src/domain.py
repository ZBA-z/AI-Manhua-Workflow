from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class Shot:
    episode: int
    episode_title: str
    number: str
    duration: int
    shot_line: str
    prompt: str
    metadata: str = ""
    references: list[int] = field(default_factory=list)
    dialogue: str = ""


@dataclass(slots=True)
class Episode:
    number: int
    title: str
    declared_shots: Optional[int]
    shots: list[Shot] = field(default_factory=list)


@dataclass(slots=True)
class ShotGroup:
    episode: int
    index: int
    shots: list[Shot]
    original_seconds: int
    planned_seconds: int
    status: str
    adjustment: str = ""
    duration_map: dict[str, int] = field(default_factory=dict)

    @property
    def task_id(self) -> str:
        return f"ep{self.episode:03d}-g{self.index:03d}"


@dataclass(slots=True)
class WorkflowTask:
    task_id: str
    episode: int
    group_index: int
    prompt: str
    references: list[str]
    model: str = "Seedance 2.0 Fast"
    duration_seconds: int = 10
    aspect_ratio: str = "16:9"
    status: str = "ready"
    duration_map: dict[str, int] = field(default_factory=dict)
