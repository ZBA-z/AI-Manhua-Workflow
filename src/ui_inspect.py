from __future__ import annotations

import argparse
import json
from pathlib import Path


def inspect_window(output: str | Path, title_regex: str = ".*豆包.*") -> int:
    try:
        from pywinauto import Application
        from pywinauto.findwindows import find_windows
    except ImportError as exc:
        raise RuntimeError("请先安装 requirements.txt 中的 pywinauto") from exc
    handles = find_windows(title_re=title_regex)
    if not handles:
        raise RuntimeError("未找到豆包窗口；请打开豆包并保持窗口可见后重试")
    app = Application(backend="uia").connect(handle=handles[0])
    window = app.window(handle=handles[0])
    controls = []
    for control in window.descendants():
        try:
            rectangle = control.rectangle()
            parent = control.parent()
            controls.append({
                "title": control.window_text(),
                "control_type": control.element_info.control_type,
                "class_name": control.class_name(),
                "automation_id": control.element_info.automation_id,
                "visible": control.is_visible(),
                "enabled": control.is_enabled(),
                "rectangle": [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom],
                "parent_title": parent.window_text() if parent else "",
                "parent_control_type": parent.element_info.control_type if parent else "",
            })
        except Exception:
            continue
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps({"title": window.window_text(), "controls": controls}, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(controls)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/ui-controls.json")
    args = parser.parse_args()
    print(f"controls={inspect_window(args.output)} output={args.output}")


if __name__ == "__main__":
    main()
