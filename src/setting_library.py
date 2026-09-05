from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AssetSpec:
    heading: str
    prompt: str
    parameters: str
    explanation: str

    @property
    def aliases(self) -> list[str]:
        text = self.heading
        text = re.sub(r"^\d+(?:\.\d+)?\s*", "", text)
        text = re.sub(r"^[一二三四五六七八九十百]+、", "", text)
        text = re.sub(r"^[一二三四五六七八九十百]+、", "", text)
        return [item for item in re.split(r"\s*[-—·（）()：:、]+\s*", text) if len(item.strip()) >= 2]


@dataclass(slots=True)
class SettingLibrary:
    background_path: str | None
    asset_path: str | None
    background_sha256: str | None
    asset_sha256: str | None
    asset_specs: list[AssetSpec]

    @property
    def summary(self) -> dict:
        return {
            "background_path": self.background_path,
            "asset_path": self.asset_path,
            "background_sha256": self.background_sha256,
            "asset_sha256": self.asset_sha256,
            "asset_spec_count": len(self.asset_specs),
            "asset_spec_names": [item.heading for item in self.asset_specs],
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_asset_specs(text: str) -> list[AssetSpec]:
    lines = text.splitlines()
    specs: list[AssetSpec] = []
    current: str | None = None
    values = {"prompt": [], "parameters": [], "explanation": []}
    section: str | None = None

    def flush() -> None:
        nonlocal current, values
        if current and values["prompt"]:
            specs.append(AssetSpec(current, "\n".join(values["prompt"]).strip(), " ".join(values["parameters"]).strip(), "\n".join(values["explanation"]).strip()))
        values = {"prompt": [], "parameters": [], "explanation": []}

    for raw in lines:
        line = raw.strip()
        if re.match(r"^(?:\d+\.\d+\s+|[一二三四五六七八九十百]+、)", line) and not line.startswith("【"):
            flush()
            current = line
            section = None
            continue
        if line.startswith("【提示词】"):
            section = "prompt"
            continue
        if line.startswith("【参数】"):
            section = "parameters"
            continue
        if line.startswith("【说明】"):
            section = "explanation"
            continue
        if current and section:
            values[section].append(raw.rstrip())
    flush()
    return specs


def load_setting_library(background_path: str | None, asset_path: str | None) -> SettingLibrary:
    background = Path(background_path) if background_path and Path(background_path).exists() else None
    asset = Path(asset_path) if asset_path and Path(asset_path).exists() else None
    return SettingLibrary(
        str(background) if background else None,
        str(asset) if asset else None,
        _sha256(background) if background else None,
        _sha256(asset) if asset else None,
        _extract_asset_specs(asset.read_text(encoding="utf-8-sig")) if asset else [],
    )


def relevant_asset_aliases(library: SettingLibrary) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for spec in library.asset_specs:
        for alias in spec.aliases:
            result.setdefault(alias, []).append(spec.heading)
    return result


def attach_specs_to_assets(assets, library: SettingLibrary) -> None:
    """Attach only high-confidence setting aliases to existing files."""
    def norm(value: str) -> str:
        return re.sub(r"[\s\u3000._。；;、·（）()\[\]【】\-—]+", "", value).lower()

    for asset in assets:
        asset_name = norm(Path(asset.name).stem)
        for spec in library.asset_specs:
            for alias in spec.aliases:
                alias_norm = norm(alias)
                if len(alias_norm) >= 2 and (alias_norm in asset_name or asset_name in alias_norm):
                    asset.aliases.append(alias_norm)
