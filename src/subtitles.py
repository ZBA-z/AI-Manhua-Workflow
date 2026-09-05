from __future__ import annotations

from pathlib import Path

from .domain import Shot


def _stamp(seconds: float) -> str:
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    if millis == 1000:
        whole += 1
        millis = 0
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _wrap_caption(text: str, width: int = 18) -> str:
    text = " ".join(text.split())
    return "\n".join(text[index:index + width] for index in range(0, len(text), width))


def write_srt(path: str | Path, shots: list[Shot], duration_map: dict[str, int] | None = None) -> int:
    lines: list[str] = []
    cursor = 0.0
    count = 0
    for shot in shots:
        if shot.dialogue and shot.dialogue != "（无台词）":
            count += 1
            duration = (duration_map or {}).get(shot.number, shot.duration)
            lines.extend([str(count), f"{_stamp(cursor)} --> {_stamp(cursor + duration)}", _wrap_caption(shot.dialogue), ""])
        cursor += (duration_map or {}).get(shot.number, shot.duration)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8-sig")
    return count
