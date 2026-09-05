from __future__ import annotations

import re
from pathlib import Path

from .domain import Episode, Shot

EPISODE_RE = re.compile(r"^第\s*(\d+)\s*集\s*[：:]\s*(.*?)(?:（\s*(\d+)\s*镜\s*）)?\s*$")
SHOT_RE = re.compile(r"^镜号\s*[：:]\s*(\d+(?:-\d+)?)\s*[；;]\s*时长\s*[：:]\s*(\d+)s\s*[；;](.*)$")
REF_RE = re.compile(r"参考图\s*(\d+)|第\s*(\d+)\s*张参考图")


def _extract_dialogue(metadata: str) -> str:
    marker = "旁白音效："
    if marker not in metadata:
        return ""
    value = metadata.split(marker, 1)[1].strip()
    if value in {"", "（无台词）", "(无台词)"}:
        return ""
    return value.rstrip("。；; ")


def _extract_references(text: str) -> list[int]:
    values: set[int] = set()
    for match in REF_RE.finditer(text):
        values.update(int(value) for value in match.groups() if value)
    return sorted(values)


def parse_script(path: str | Path) -> list[Episode]:
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    episodes: list[Episode] = []
    current: Episode | None = None
    pending_shot: Shot | None = None
    for raw in lines:
        line = raw.strip()
        episode_match = EPISODE_RE.match(line)
        if episode_match:
            current = Episode(
                number=int(episode_match.group(1)),
                title=episode_match.group(2).strip(),
                declared_shots=int(episode_match.group(3)) if episode_match.group(3) else None,
            )
            episodes.append(current)
            pending_shot = None
            continue
        shot_match = SHOT_RE.match(line)
        if shot_match and current is not None:
            metadata = shot_match.group(3).strip()
            pending_shot = Shot(
                episode=current.number,
                episode_title=current.title,
                number=shot_match.group(1),
                duration=int(shot_match.group(2)),
                shot_line=line,
                prompt="",
                metadata=metadata,
                references=_extract_references(metadata),
                dialogue=_extract_dialogue(metadata),
            )
            current.shots.append(pending_shot)
            continue
        if pending_shot is not None and line.startswith("电影级文生视频："):
            pending_shot.prompt = line.split("：", 1)[1].strip()
            pending_shot.references = _extract_references(line)
    # Episode 21 has a documented source typo: shot 5 is absent and the
    # remaining shots continue at 6. Normalize only this known case so task
    # IDs and duration maps stay contiguous without rewriting the source file.
    for episode in episodes:
        if episode.number == 21 and all(shot.number.isdigit() for shot in episode.shots):
            numbers = [int(shot.number) for shot in episode.shots]
            if numbers == list(range(1, len(numbers) + 1)):
                continue
            if numbers[:4] == [1, 2, 3, 4] and numbers[4:] == list(range(6, 6 + len(numbers) - 4)):
                for index, shot in enumerate(episode.shots, start=1):
                    shot.number = str(index)
                episode.declared_shots = len(episode.shots)
    return episodes


def validate_episodes(episodes: list[Episode]) -> list[str]:
    issues: list[str] = []
    for episode in episodes:
        if episode.declared_shots is not None and episode.declared_shots != len(episode.shots):
            issues.append(
                f"第{episode.number}集声明{episode.declared_shots}镜，实际解析{len(episode.shots)}镜"
            )
        simple_numbers = all(shot.number.isdigit() for shot in episode.shots)
        composite_numbers = all("-" in shot.number for shot in episode.shots)
        expected = 1
        composite_prefix = None
        composite_expected = None
        for shot in episode.shots:
            if simple_numbers and int(shot.number) != expected:
                issues.append(f"第{episode.number}集镜号断裂：期望{expected}，发现{shot.number}")
                expected = int(shot.number)
            if composite_numbers:
                prefix, suffix = shot.number.split("-", 1)
                if composite_prefix is None:
                    composite_prefix, composite_expected = prefix, int(suffix)
                if prefix != composite_prefix or int(suffix) != composite_expected:
                    issues.append(f"第{episode.number}集复合镜号断裂：期望{composite_prefix}-{composite_expected}，发现{shot.number}")
                    composite_prefix, composite_expected = prefix, int(suffix)
            if shot.duration <= 0:
                issues.append(f"第{episode.number}集镜号{shot.number}时长非法：{shot.duration}")
            if not shot.prompt:
                issues.append(f"第{episode.number}集镜号{shot.number}缺少视频提示词")
            expected += 1
            if composite_numbers and composite_expected is not None:
                composite_expected += 1
    return issues
