from __future__ import annotations

import json
import shutil
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class MediaInfo:
    path: str
    duration: float
    width: int
    height: int
    codec: str

    @property
    def aspect_ratio(self) -> str:
        return "16:9" if self.width * 9 == self.height * 16 else f"{self.width}:{self.height}"


@dataclass(frozen=True, slots=True)
class VideoQualityReport:
    status: str
    reason: str
    media: MediaInfo | None
    black_ratio: float = 0.0
    freeze_ratio: float = 0.0
    evidence: tuple[str, ...] = ()


class MediaRejected(ValueError):
    """The downloaded media failed a deterministic acceptance gate."""


class QualityReviewRequired(RuntimeError):
    """The media needs human review and has been quarantined."""


def find_ffprobe() -> str | None:
    found = shutil.which("ffprobe")
    if found:
        return found
    return None


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def inspect_video(path: str | Path) -> MediaInfo:
    executable = find_ffprobe()
    if executable:
        command = [executable, "-v", "error", "-show_entries", "format=duration:stream=width,height,codec_name", "-of", "json", str(path)]
        output = subprocess.check_output(command, text=True, encoding="utf-8")
        payload = json.loads(output)
        stream = next(item for item in payload["streams"] if "width" in item)
        return MediaInfo(str(path), float(payload["format"]["duration"]), int(stream["width"]), int(stream["height"]), stream.get("codec_name", ""))
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffprobe/ffmpeg 不可用，无法验收视频比例")
    result = subprocess.run([ffmpeg, "-i", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = result.stderr
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    size_match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    codec_match = re.search(r"Video:\s*([^,\s]+)", text)
    if not duration_match or not size_match:
        raise RuntimeError(f"无法解析视频信息：{path}")
    duration = int(duration_match.group(1)) * 3600 + int(duration_match.group(2)) * 60 + float(duration_match.group(3))
    return MediaInfo(str(path), duration, int(size_match.group(1)), int(size_match.group(2)), codec_match.group(1) if codec_match else "")


def require_16x9(path: str | Path) -> MediaInfo:
    info = inspect_video(path)
    if info.aspect_ratio != "16:9":
        raise ValueError(f"视频不是16:9：{path} ({info.width}x{info.height})")
    return info


def validate_video(path: str | Path, expected_duration: float | None = 10, duration_tolerance: float = 1.5) -> MediaInfo:
    """Validate decodability, video dimensions and configured duration."""
    info = require_16x9(path)
    if expected_duration is not None and abs(info.duration - expected_duration) > duration_tolerance:
        raise ValueError(
            f"视频时长超出允许误差：{path} ({info.duration:.2f}s，目标{expected_duration}s±{duration_tolerance}s)"
        )
    return info


def _filter_ratio(executable: str, path: str | Path, filter_name: str, duration: float) -> float:
    result = subprocess.run(
        [executable, "-hide_banner", "-i", str(path), "-vf", filter_name, "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    output = result.stderr
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg质量滤镜失败({result.returncode})：{path}")
    intervals = []
    if filter_name.startswith("blackdetect"):
        for match in re.finditer(r"black_start:([0-9.]+).*?black_end:([0-9.]+)", output):
            intervals.append((float(match.group(1)), float(match.group(2))))
    else:
        events = re.findall(r"freeze_(start|end):\s*([0-9.]+)", output)
        for action, value in events:
            if action == "start":
                intervals.append((float(value), duration))
            elif intervals:
                start, _old_end = intervals[-1]
                intervals[-1] = (start, float(value))
    covered = sum(max(0.0, end - start) for start, end in intervals)
    return min(1.0, covered / duration) if duration > 0 else 0.0


def _extract_evidence(executable: str, path: str | Path, directory: str | Path, duration: float) -> tuple[str, ...]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []
    for label, timestamp in (("start", 0.1), ("middle", max(0.1, duration / 2)), ("end", max(0.1, duration - 0.1))):
        target = root / f"{label}.jpg"
        result = subprocess.run(
            [executable, "-y", "-ss", f"{timestamp:.3f}", "-i", str(path), "-frames:v", "1", "-q:v", "3", str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError(f"无法抽取质量证据帧：{label}")
        outputs.append(str(target))
    return tuple(outputs)


def assess_video(
    path: str | Path,
    expected_duration: float | None = 10,
    evidence_dir: str | Path | None = None,
    black_ratio_limit: float = 0.45,
    freeze_ratio_limit: float = 0.85,
) -> VideoQualityReport:
    """Perform deterministic media checks without a text/vision model."""
    try:
        info = validate_video(path, expected_duration=expected_duration)
    except ValueError as exc:
        return VideoQualityReport("reject", str(exc), None)
    except Exception as exc:
        return VideoQualityReport("reject", f"视频无法解码：{exc}", None)
    executable = find_ffmpeg()
    if not executable:
        return VideoQualityReport("review", "缺少ffmpeg，需人工验收", info)
    try:
        # `pix_th` is a luminance threshold, not a percentage of black pixels.
        # 0.02 avoids classifying dark cinematic frames as all-black.
        black_ratio = _filter_ratio(executable, path, "blackdetect=d=0.1:pix_th=0.02", info.duration)
        freeze_ratio = _filter_ratio(executable, path, "freezedetect=n=-60dB:d=0.5", info.duration)
        evidence = _extract_evidence(executable, path, evidence_dir or Path(path).with_suffix(".evidence"), info.duration)
    except Exception as exc:
        return VideoQualityReport("review", f"质量检测不完整：{exc}", info)
    if black_ratio >= black_ratio_limit:
        return VideoQualityReport("review", f"黑场占比过高：{black_ratio:.2f}", info, black_ratio, freeze_ratio, evidence)
    if freeze_ratio >= freeze_ratio_limit:
        return VideoQualityReport("review", f"冻结占比过高：{freeze_ratio:.2f}", info, black_ratio, freeze_ratio, evidence)
    return VideoQualityReport("pass", "媒体闸门通过", info, black_ratio, freeze_ratio, evidence)


def chinese_episode(number: int) -> str:
    digits = "零一二三四五六七八九"
    if number < 10:
        return digits[number]
    if number < 20:
        return "十" if number == 10 else "十" + digits[number - 10]
    if number < 100:
        tens, ones = divmod(number, 10)
        return digits[tens] + "十" + (digits[ones] if ones else "")
    return str(number)


def output_name(episode: int, index: int) -> str:
    return f"第{chinese_episode(episode)}集{index}.mp4"


def concat_videos(inputs: list[str | Path], output: str | Path, copy_streams: bool = False) -> None:
    executable = find_ffmpeg()
    if not executable:
        raise RuntimeError("ffmpeg 不在 PATH，无法拼接视频")
    if not inputs:
        raise ValueError("至少需要一个输入视频")
    manifest = Path(output).with_suffix(".concat.txt")
    manifest.write_text("\n".join(f"file '{Path(item).resolve().as_posix().replace("'", "'\\''")}'" for item in inputs), encoding="utf-8")
    command = [executable, "-y", "-f", "concat", "-safe", "0", "-i", str(manifest)]
    if copy_streams:
        command += ["-c", "copy"]
    else:
        command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
    command.append(str(output))
    subprocess.run(command, check=True)
