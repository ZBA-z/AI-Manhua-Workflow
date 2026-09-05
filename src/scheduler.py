from __future__ import annotations

import subprocess
from pathlib import Path


def prepare_on_startup(project_dir: str | Path, config: str | Path) -> int:
    command = ["python", "-m", "src.cli", "prepare", "--config", str(config)]
    return subprocess.call(command, cwd=str(project_dir))


def install_task(project_dir: str | Path, config: str | Path, task_name: str = "AIManhuaWorkflow") -> str:
    raise RuntimeError("旧版调度器入口已禁用，请使用 scripts/install_startup.ps1 安装规范任务")
