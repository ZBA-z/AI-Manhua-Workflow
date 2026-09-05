from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Asset:
    path: str
    name: str
    normalized: str
    sha256: str
    category: str
    aliases: list[str] = field(default_factory=list)


def normalize_name(name: str) -> str:
    stem = Path(name).stem
    return normalize_text(stem)


def normalize_text(text: str) -> str:
    """Normalize arbitrary prompt text without treating punctuation as a file suffix."""
    return re.sub(r"[\s\u3000._。；;、·（）()\[\]【】]+", "", text).lower()


def _aliases(name: str) -> list[str]:
    stem = Path(name).stem
    values = {normalize_name(stem)}
    for part in re.split(r"[.。；;、·（）()\[\]【】（）\-—]+", stem):
        normalized = normalize_name(part)
        if len(normalized) >= 2:
            values.add(normalized)
    return sorted(values, key=lambda item: (-len(item), item))


def _hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


PROP_HINTS = ("武器", "匣子", "军旗", "旗", "魔药", "晶", "心脏", "遗骸", "信物", "照片", "装备", "石像", "战舰", "穿梭机", "地图", "虫袋", "权杖", "法杖", "火尖枪", "风火轮", "混天绫", "乾坤圈", "帝玺", "天枢剑", "剑", "琴", "眼", "号角", "谎言", "短刃", "黄符", "导弹", "巨斧", "开天斧", "酒杯", "神器", "全息")
SCENE_HINTS = ("战场", "大教堂", "总部", "基地", "医院", "避难", "车库", "城市", "战壕", "营地", "神照社", "神殿", "要塞", "指挥所", "指挥中心", "混沌", "创生", "封印", "广场", "时间倒流", "训练室", "演习场", "大气层", "宇宙深处", "会议室", "机库", "控制中心", "超远景", "占领区", "红雾")


def infer_category(name: str, default: str) -> str:
    """Separate props kept in the character folder without moving files."""
    stem = Path(name).stem
    if default == "character" and any(token in stem for token in SCENE_HINTS):
        return "scene"
    if default == "character" and any(token in stem for token in PROP_HINTS):
        return "prop"
    return default


def index_assets(root: str | Path, category: str) -> list[Asset]:
    result: list[Asset] = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            result.append(Asset(str(path), path.name, normalize_name(path.name), _hash_file(path), infer_category(path.name, category), _aliases(path.name)))
    return result


def resolve_reference(number: int, assets: list[Asset]) -> Asset | None:
    """Resolve reference indexes as 1-based sorted asset order."""
    ordered = sorted(assets, key=lambda item: (item.category, item.name))
    return ordered[number - 1] if 1 <= number <= len(ordered) else None


VARIANT_HINTS = {
    "寄生": ("寄生", "脉络", "触须", "污染", "寄生体"),
    "不可控": ("不可控", "失控", "纯黑", "嘴部裂开", "扭曲"),
    "战损": ("战损", "受损", "破损", "伤痕", "残破"),
    "普通": ("普通", "常态", "正常"),
}


def _extra_aliases(asset: Asset, alias_rules: dict[str, list[str]] | None) -> list[str]:
    if not alias_rules:
        return []
    keys = {asset.name, Path(asset.name).stem, normalize_name(asset.name)}
    values: list[str] = []
    for key in keys:
        values.extend(alias_rules.get(key, []))
    return [normalize_text(value) for value in values if len(normalize_text(value)) >= 2]


def match_by_prompt(prompt: str, assets: list[Asset], alias_rules: dict[str, list[str]] | None = None) -> list[Asset]:
    normalized_prompt = normalize_text(prompt)
    by_category: dict[str, dict[str, list[tuple[int, Asset]]]] = {}
    for asset in assets:
        aliases = [normalize_text(alias) for alias in asset.aliases] + _extra_aliases(asset, alias_rules)
        hits = [alias for alias in aliases if len(alias) >= 2 and alias in normalized_prompt]
        score = max(map(len, hits), default=0)
        if score:
            variant_score = 0
            stem = normalize_name(asset.name)
            for variant, hints in VARIANT_HINTS.items():
                variant_norm = normalize_text(variant)
                if variant_norm in stem and any(normalize_text(hint) in normalized_prompt for hint in hints):
                    variant_score += len(variant_norm)
            # Prefer a plain base asset when the prompt gives no state clue;
            # prefer a state-specific asset when variant hints are present.
            is_plain = int(stem in hits)
            rank = score * 100 + variant_score * 10 + is_plain
            # One group per named entity allows a shot to carry several
            # characters/props while still collapsing variants of one entity.
            entity = max(hits, key=len)
            by_category.setdefault(asset.category, {}).setdefault(entity, []).append((rank, asset))
    selected: list[Asset] = []
    limits = {"scene": 1, "character": 4, "prop": 4}
    for category, entities in by_category.items():
        winners: list[tuple[int, Asset]] = []
        for candidates in entities.values():
            best_score = max(score for score, _ in candidates)
            best = [asset for score, asset in candidates if score == best_score]
            # Tied variants remain manual. This is safer than silently
            # uploading a visually different state of the same entity.
            if len(best) == 1:
                winners.append((best_score, best[0]))
        winners.sort(key=lambda item: (-item[0], item[1].name))
        selected.extend(asset for _, asset in winners[: limits.get(category, 4)])
    # The same composite file can match more than one alias; upload once.
    selected = list({asset.path: asset for asset in selected}.values())
    return sorted(selected, key=lambda item: (item.category, item.name))
