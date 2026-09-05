from __future__ import annotations

import time
from pathlib import Path

from .media import MediaRejected, QualityReviewRequired, assess_video, output_name, require_16x9


def snapshot_mp4(directories: list[str | Path]) -> set[str]:
    """Return an absolute-path snapshot across configured discovery folders."""
    snapshot: set[str] = set()
    for directory in directories:
        root = Path(directory)
        if not root.exists():
            continue
        snapshot.update(str(item.resolve()) for item in root.glob("*.mp4"))
    return snapshot


def wait_for_new_mp4_in_dirs(directories: list[str | Path], before: set[str], timeout: int = 900, submitted_after: float | None = None) -> Path:
    """Find one stable new MP4 across all discovery folders."""
    deadline = time.time() + timeout
    before_resolved = {str(Path(path).resolve()) for path in before}
    while time.time() < deadline:
        candidates: list[Path] = []
        for directory in directories:
            root = Path(directory)
            if not root.exists():
                continue
            candidates.extend(
                item for item in root.glob("*.mp4")
                if str(item.resolve()) not in before_resolved
                and (submitted_after is None or item.stat().st_mtime >= submitted_after - 1)
                and item.stat().st_size > 0
                and not item.name.endswith((".part.mp4", ".crdownload.mp4"))
            )
        if len(candidates) == 1:
            candidate = candidates[0]
            first_size = candidate.stat().st_size
            first_mtime = candidate.stat().st_mtime
            time.sleep(2)
            if not (candidate.exists() and candidate.stat().st_size == first_size and candidate.stat().st_mtime == first_mtime):
                continue
            time.sleep(2)
            if candidate.exists() and candidate.stat().st_size == first_size and candidate.stat().st_mtime == first_mtime:
                return candidate
        elif len(candidates) > 1:
            raise RuntimeError(f"发现多个新MP4，拒绝猜测任务绑定：{[str(item) for item in candidates]}")
        time.sleep(2)
    raise TimeoutError(f"下载目录在{timeout}s内没有唯一新MP4：{directories}")


def wait_for_new_mp4(download_dir: str | Path, before: set[str], timeout: int = 900, submitted_after: float | None = None) -> Path:
    root = Path(download_dir)
    root.mkdir(parents=True, exist_ok=True)
    before_resolved = {str(Path(path).resolve()) for path in before}
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = [
            item for item in root.glob("*.mp4")
            if str(item.resolve()) not in before_resolved
            and (submitted_after is None or item.stat().st_mtime >= submitted_after - 1)
            and item.stat().st_size > 0
            and not item.name.endswith((".part.mp4", ".crdownload.mp4"))
        ]
        if candidates:
            candidate = max(candidates, key=lambda item: item.stat().st_mtime)
            first_size = candidate.stat().st_size
            first_mtime = candidate.stat().st_mtime
            time.sleep(2)
            if not (candidate.exists() and candidate.stat().st_size == first_size and candidate.stat().st_mtime == first_mtime):
                continue
            time.sleep(2)
            if candidate.exists() and candidate.stat().st_size == first_size and candidate.stat().st_mtime == first_mtime:
                return candidate
        time.sleep(2)
    raise TimeoutError(f"下载目录在{timeout}s内没有新MP4：{root}")


def archive_download(
    source: str | Path,
    output_dir: str | Path,
    episode: int,
    index: int,
    expected_duration: float | None = None,
    quality_review_dir: str | Path | None = None,
    quality_evidence_dir: str | Path | None = None,
    black_ratio_limit: float = 0.45,
    freeze_ratio_limit: float = 0.85,
) -> Path:
    source_path = Path(source)
    if quality_review_dir is not None:
        report = assess_video(
            source_path,
            expected_duration=expected_duration,
            evidence_dir=quality_evidence_dir or Path(quality_review_dir) / f"{source_path.stem}-evidence",
            black_ratio_limit=black_ratio_limit,
            freeze_ratio_limit=freeze_ratio_limit,
        )
        if report.status == "reject":
            raise MediaRejected(f"媒体质量拒绝：{report.reason}")
        if report.status == "review":
            review_root = Path(quality_review_dir)
            review_root.mkdir(parents=True, exist_ok=True)
            review_target = review_root / source_path.name
            if review_target.exists():
                review_target = review_root / f"{source_path.stem}-{int(time.time())}{source_path.suffix}"
            source_path.replace(review_target)
            raise QualityReviewRequired(f"{report.reason}; file={review_target}")
    else:
        info = require_16x9(source_path)
        if expected_duration is not None and abs(info.duration - expected_duration) > 1.5:
            raise ValueError(f"视频时长超出允许误差：{source_path} ({info.duration:.2f}s，目标{expected_duration}s±1.5s)")
    destination = Path(output_dir) / output_name(episode, index)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"成片已存在，拒绝覆盖：{destination}")
    if source_path.resolve() == destination.resolve():
        return destination
    try:
        source_path.replace(destination)
    except OSError:
        import shutil
        shutil.move(str(source_path), str(destination))
    return destination
