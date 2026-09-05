from __future__ import annotations

from .domain import Episode, Shot, ShotGroup


def _adjustable(shot: Shot) -> bool:
    return not shot.dialogue and shot.duration >= 2


def _plan_group(episode: int, index: int, shots: list[Shot], target: int) -> ShotGroup:
    original = sum(shot.duration for shot in shots)
    durations = {shot.number: shot.duration for shot in shots}
    if original == target:
        return ShotGroup(episode, index, shots, original, target, "ready", "", durations)
    if len(shots) == 1:
        return ShotGroup(episode, index, shots, original, original, "manual", "单镜头不补写剧情，跳过该无配对分镜", durations)
    if original < target:
        candidates = [shot for shot in shots if _adjustable(shot)] or shots
        candidate = candidates[-1]
        durations[candidate.number] += target - original
        caution = "" if _adjustable(candidate) else "；候选镜头含台词，需确认口型/节奏"
        return ShotGroup(episode, index, shots, original, target, "ready", f"延长镜号{candidate.number}：{target - original}s{caution}", durations)

    remaining = original - target
    candidates = sorted((shot for shot in shots if _adjustable(shot)), key=lambda shot: shot.duration, reverse=True)
    dialogue_candidates = sorted((shot for shot in shots if shot not in candidates), key=lambda shot: len(shot.dialogue))
    changed: list[str] = []
    for candidate in candidates + dialogue_candidates:
        removable = max(0, durations[candidate.number] - 1)
        cut = min(remaining, removable)
        if cut:
            durations[candidate.number] -= cut
            remaining -= cut
            changed.append(f"镜号{candidate.number}-{cut}s")
        if remaining == 0:
            break
    if remaining:
        return ShotGroup(episode, index, shots, original, original - (original - target - remaining), "manual", "无法在不制造零时长镜头的前提下压缩，跳过该分镜组", durations)
    has_dialogue_cut = any(shot.dialogue and durations[shot.number] != shot.duration for shot in shots)
    caution = "；涉及台词镜头，需复核口型" if has_dialogue_cut else ""
    return ShotGroup(episode, index, shots, original, target, "ready", f"压缩：{'、'.join(changed)}{caution}", durations)


def plan_episode(episode: Episode, max_shots: int = 3, target_seconds: int = 10) -> list[ShotGroup]:
    groups: list[ShotGroup] = []
    current: list[Shot] = []
    index = 1
    for shot in episode.shots:
        if current and len(current) >= max_shots:
            groups.append(_plan_group(episode.number, index, current, target_seconds))
            index += 1
            current = []
        current.append(shot)
    if current:
        groups.append(_plan_group(episode.number, index, current, target_seconds))
    return groups


def plan_all(episodes: list[Episode], target_seconds: int = 10) -> list[ShotGroup]:
    result: list[ShotGroup] = []
    for episode in episodes:
        result.extend(plan_episode(episode, target_seconds=target_seconds))
    return result


def split_by_reference_limit(
    groups: list[ShotGroup],
    reference_sets: dict[tuple[int, str], set[str]],
    max_references: int,
    target_seconds: int = 10,
) -> list[ShotGroup]:
    """Split only groups whose combined upload set exceeds the model limit."""
    by_episode: dict[int, list[ShotGroup]] = {}
    for group in groups:
        chunks: list[list[Shot]] = []
        current: list[Shot] = []
        current_refs: set[str] = set()
        for shot in group.shots:
            shot_refs = reference_sets.get((shot.episode, str(shot.number)), set())
            if current and len(current_refs | shot_refs) > max_references:
                chunks.append(current)
                current = []
                current_refs = set()
            current.append(shot)
            current_refs |= shot_refs
        if current:
            chunks.append(current)

        if len(chunks) == 1:
            by_episode.setdefault(group.episode, []).append(group)
            continue
        for shots in chunks:
            planned = _plan_group(group.episode, 0, shots, target_seconds)
            if len(shots) == 1 and sum(planned.duration_map.values()) <= target_seconds and not shots[0].dialogue:
                only = shots[0]
                planned.duration_map[only.number] = target_seconds
                planned.planned_seconds = target_seconds
                planned.status = "ready"
                planned.adjustment = f"参考图超限拆组；单镜头延长至{target_seconds}s"
            elif len(shots) == 1 and sum(planned.duration_map.values()) <= target_seconds and shots[0].dialogue:
                planned.status = "manual"
                planned.adjustment = f"参考图超限拆组；单镜头含台词且仅{sum(planned.duration_map.values())}s，需人工确认是否延长"
            else:
                planned.adjustment = f"参考图超限自动拆组；{planned.adjustment}".rstrip("；")
            by_episode.setdefault(group.episode, []).append(planned)

    result: list[ShotGroup] = []
    for episode, episode_groups in by_episode.items():
        for index, group in enumerate(episode_groups, start=1):
            group.index = index
            result.append(group)
    return sorted(result, key=lambda item: (item.episode, item.index))
