from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from .desktop import desktop_is_interactive
from .doubao_ui import DoubaoUI
from .runner import drain_pending, run_one
from .store import Store


@dataclass(frozen=True)
class StartupDecision:
    needs_gui: bool
    reason: str


@dataclass(frozen=True)
class StartupResult:
    run_id: str
    state: str
    semantic: str
    exit_code: int


class RunAction(str, Enum):
    CONTINUE = "continue"
    SKIP = "skip"
    FINISH = "finish"
    STOP = "stop"


def within_schedule_window(config: dict, now: datetime | None = None) -> bool:
    window = config.get("automation", {})
    if window.get("schedule_window_enabled", False) is not True:
        return True
    current_dt = now or datetime.now()
    activation_date = window.get("activation_date")
    if activation_date and current_dt.date().isoformat() < str(activation_date):
        return False
    current = current_dt.time()
    start = int(window.get("schedule_start_hour", 15))
    end = int(window.get("schedule_end_hour", 24))
    if not 0 <= start < 24 or not 1 <= end <= 24 or start >= end:
        raise ValueError("启动时间窗配置无效：必须满足 0<=start<end<=24")
    if end == 24:
        return current >= clock_time(hour=start)
    return clock_time(hour=start) <= current < clock_time(hour=end)


def classify_run_result(semantic: str) -> RunAction:
    if semantic.startswith("COMPLETED:") or semantic.startswith("ACCOUNT_QUOTA_EXHAUSTED:"):
        return RunAction.CONTINUE
    if semantic.startswith(("TASK_MANUAL_", "TASK_MODERATION_BLOCKED:", "TASK_QUALITY_REVIEW:", "TASK_MEDIA_REJECTED:")):
        return RunAction.SKIP
    if semantic in {"NO_READY_TASK", "NO_ACCOUNT_BUDGET", "NO_USABLE_ACCOUNT"}:
        return RunAction.FINISH
    return RunAction.STOP


def decide_startup(config: dict, store: Store) -> StartupDecision:
    if not within_schedule_window(config):
        return StartupDecision(False, "outside_schedule_window")
    automation = config.get("automation", {})
    if automation.get("enabled") is not True:
        return StartupDecision(False, "automation_disabled")
    if store.pending_tasks():
        return StartupDecision(True, "pending_recovery")
    if automation.get("allow_submit") is not True:
        return StartupDecision(False, "submit_disabled")
    if automation.get("staged_validation_passed") is not True:
        return StartupDecision(False, "staged_validation_required")
    if store.next_ready() is None:
        return StartupDecision(False, "no_ready_task")
    if config.get("accounts") and not store.account_candidates():
        return StartupDecision(False, "no_account_budget")
    return StartupDecision(True, "ready_to_submit")


def run_startup(config_path: str, prepare_callback: Callable[[str], None]) -> StartupResult:
    config_file = Path(config_path)
    config = json.loads(config_file.read_text(encoding="utf-8"))
    status_path = Path(config.get("startup_status") or (Path(config["database"]).parent / "startup-status.json"))
    previous = _recover_stale_status(status_path)
    if previous is not None:
        return StartupResult(previous["run_id"], "blocked", previous["semantic"], 0)
    run_id = str(uuid.uuid4())
    started = _now()
    payload = {
        "run_id": run_id,
        "state": "running",
        "started_at": started,
        "pid": os.getpid(),
        "ended_at": None,
        "semantic": None,
        "config_sha256": hashlib.sha256(config_file.read_bytes()).hexdigest(),
        "stage_seconds": {},
        "before": _state_summary(config),
        "after": None,
    }
    _write_status(status_path, payload)

    try:
        if not within_schedule_window(config):
            return _finish(status_path, payload, run_id, "no_work", "NO_GUI_WORK: outside_schedule_window", 0, config)
        stage_started = time.monotonic()
        prepare_callback(config_path)
        payload["stage_seconds"]["prepare"] = round(time.monotonic() - stage_started, 3)
        config = json.loads(config_file.read_text(encoding="utf-8"))
        store = Store(config["database"])
        try:
            decision = decide_startup(config, store)
        finally:
            store.db.close()
        payload["decision"] = {"needs_gui": decision.needs_gui, "reason": decision.reason}
        if not decision.needs_gui:
            return _finish(status_path, payload, run_id, "no_work", f"NO_GUI_WORK: {decision.reason}", 0, config)

        requires_unlocked = config.get("automation", {}).get("require_unlocked_desktop", True)
        payload["desktop_interactive"] = desktop_is_interactive() if requires_unlocked else None
        if requires_unlocked and not payload["desktop_interactive"]:
            return _finish(status_path, payload, run_id, "blocked", "DESKTOP_LOCKED", 0, config)

        stage_started = time.monotonic()
        _ensure_doubao_session(config)
        payload["stage_seconds"]["doubao_session"] = round(time.monotonic() - stage_started, 3)

        if decision.reason == "pending_recovery":
            stage_started = time.monotonic()
            maximum = int(config.get("automation", {}).get("max_recoveries_per_start", 10))
            recovery = drain_pending(config_path, maximum=maximum)
            payload["stage_seconds"]["recovery"] = round(time.monotonic() - stage_started, 3)
            payload["recovery"] = recovery
            if recovery.startswith("DOWNLOAD_PENDING:"):
                return _finish(status_path, payload, run_id, "blocked", recovery, 0, config)
            store = Store(config["database"])
            try:
                decision = decide_startup(config, store)
            finally:
                store.db.close()
            payload["decision_after_recovery"] = {"needs_gui": decision.needs_gui, "reason": decision.reason}
            if not decision.needs_gui:
                return _finish(status_path, payload, run_id, "blocked", f"RECOVERY_COMPLETE: {decision.reason}", 0, config)

        automation = config.get("automation", {})
        maximum = int(automation.get("max_submissions_per_start", 1))
        daily_cap = int(automation.get("daily_submission_cap", 0))
        if daily_cap > 0:
            store = Store(config["database"])
            try:
                used_today = store.daily_success_count()
            finally:
                store.db.close()
            maximum = min(maximum, max(0, daily_cap - used_today))
        if maximum <= 0:
            return _finish(status_path, payload, run_id, "success", "BATCH_FINISHED: daily submission cap reached", 0, config)
        completed = 0
        skipped = 0
        attempted = 0
        for _index in range(maximum):
            stage_started = time.monotonic()
            semantic = run_one(config_path)
            attempted += 1
            payload["stage_seconds"][f"run_one_{_index + 1}"] = round(time.monotonic() - stage_started, 3)
            action = classify_run_result(semantic)
            payload["batch"] = {
                "limit": maximum, "attempted": attempted, "completed": completed,
                "skipped": skipped, "last_result": semantic,
            }
            if action == RunAction.CONTINUE:
                if semantic.startswith("COMPLETED:"):
                    completed += 1
                payload["batch"]["completed"] = completed
                payload["batch"]["last_result"] = semantic
                continue
            if action == RunAction.SKIP:
                skipped += 1
                payload["batch"]["skipped"] = skipped
                continue
            if action == RunAction.FINISH:
                summary = f"BATCH_FINISHED: completed={completed} skipped={skipped} attempted={attempted} reason={semantic}"
                return _finish(status_path, payload, run_id, "success", summary, 0, config)
            return _finish(status_path, payload, run_id, "blocked", semantic, 0, config)
        summary = f"BATCH_LIMIT_REACHED: completed={completed} skipped={skipped} attempted={attempted} limit={maximum}"
        return _finish(status_path, payload, run_id, "success", summary, 0, config)
    except Exception as exc:
        return _finish(status_path, payload, run_id, "failed", f"FAILED: {exc}", 1, config)


def _ensure_doubao_session(config: dict, timeout: int = 90) -> None:
    ui = DoubaoUI(config.get("ui_calibration", "configs/ui_calibration.json"), dry_run=False)
    if ui.session_ready():
        return
    automation = config.get("automation", {})
    if automation.get("launch_doubao_on_startup") is not True:
        raise RuntimeError("Doubao session is not interactive and launch_doubao_on_startup=false")
    shortcut = Path(str(config.get("doubao_shortcut", "")))
    executable = Path(str(config.get("doubao_executable", "")))
    target = shortcut if shortcut.is_file() else executable
    if not target.is_file():
        raise RuntimeError("Doubao executable or shortcut not found")
    os.startfile(str(target))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        if ui.session_ready():
            return
    raise RuntimeError("Doubao main chat window is not visible or interactive")


def _state_summary(config: dict) -> dict:
    statuses: dict[str, int] = {}
    accounts: list[dict] = []
    pending = 0
    database = Path(config["database"])
    if database.is_file():
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for status, count in connection.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status"):
                statuses[str(status)] = int(count)
            accounts = [
                {
                    "label": row[0], "success_count": row[1], "failure_count": row[2],
                    "last_task_id": row[3], "availability_state": row[4],
                    "availability_reason": row[5], "daily_limit": row[6], "verified_at": row[7],
                }
                for row in connection.execute(
                    """SELECT label,success_count,failure_count,last_task_id,
                              availability_state,availability_reason,daily_limit,verified_at
                       FROM accounts ORDER BY switch_order"""
                )
            ]
            pending = int(connection.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE status IN ('submitted','download_pending','submitted_unconfirmed')
                      OR attempt_state IN ('click_pending','submitted_unconfirmed')"""
            ).fetchone()[0])
        except sqlite3.Error:
            statuses = {}
            accounts = []
            pending = 0
        finally:
            connection.close()
    output_value = str(config.get("output_dir", ""))
    output = Path(output_value) if output_value else Path("__missing_output_directory__")
    files = list(output.glob("*.mp4")) if output.is_dir() else []
    return {
        "task_statuses": statuses,
        "pending": pending,
        "accounts": accounts,
        "output_count": len(files),
        "output_bytes": sum(item.stat().st_size for item in files),
    }


def _finish(status_path: Path, payload: dict, run_id: str, state: str, semantic: str, exit_code: int, config: dict) -> StartupResult:
    payload["state"] = state
    payload["semantic"] = semantic
    payload["ended_at"] = _now()
    payload["after"] = _state_summary(config)
    _write_status(status_path, payload)
    return StartupResult(run_id, state, semantic, exit_code)


def _recover_stale_status(status_path: Path) -> dict | None:
    """Detect a previous run that never reached a terminal state.

    A previous live run blocks a new startup. A dead run is not restored
    because it has no safe end-state for quota or task mutation; this returns
    an explicit blocked diagnostic instead of silently treating it as normal.
    """
    try:
        previous = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if previous.get("state") != "running":
        return None
    pid = previous.get("pid")
    try:
        alive = bool(pid) and os.kill(int(pid), 0) is None
    except (OSError, TypeError, ValueError):
        alive = False
    if alive:
        semantic = f"PREVIOUS_RUN_RUNNING: pid={pid}"
        return {"run_id": str(previous.get("run_id") or "unknown"), "semantic": semantic}
    previous["state"] = "failed"
    previous["semantic"] = f"PREVIOUS_RUN_KILLED: pid={pid}"
    previous["ended_at"] = _now()
    previous["recovery"] = {"cleared_stale_process": True}
    _write_status(status_path, previous)
    return None


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
