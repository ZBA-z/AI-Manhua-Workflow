from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from .accounts import AccountInspector
from .doubao_ui import AccountUIError, DoubaoTaskSpec, DoubaoUI, ModerationBlocked, QuotaExhausted, SubmissionUnconfirmed, UIBlocked
from .downloads import archive_download, snapshot_mp4, wait_for_new_mp4_in_dirs
from .media import MediaRejected, QualityReviewRequired
from .store import Store


def _quality_review_dir(config: dict) -> str | None:
    quality = config.get("quality", {})
    if quality.get("enabled") is not True:
        return None
    return str(quality.get("review_dir") or Path(config["database"]).parent / "quality-review")


def _quality_options(config: dict, task_id: str | None = None) -> dict:
    quality = config.get("quality", {})
    if quality.get("enabled") is not True:
        return {}
    evidence_dir = quality.get("evidence_dir")
    if evidence_dir and task_id:
        evidence_dir = str(Path(evidence_dir) / task_id)
    return {
        "quality_review_dir": _quality_review_dir(config),
        "quality_evidence_dir": evidence_dir,
        "black_ratio_limit": float(quality.get("black_ratio_limit", 0.45)),
        "freeze_ratio_limit": float(quality.get("freeze_ratio_limit", 0.85)),
    }


@contextmanager
def _run_lock(path: str | Path):
    lock = Path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        stale = False
        try:
            content = lock.read_text(encoding="ascii", errors="ignore")
            pid = int(content.split("pid=", 1)[1].split()[0])
            os.kill(pid, 0)
        except (FileNotFoundError, ValueError, IndexError, ProcessLookupError, PermissionError, OSError):
            stale = True
        if stale:
            try:
                lock.unlink()
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileNotFoundError, FileExistsError, OSError) as exc:
                raise UIBlocked(f"无法清理陈旧工作流锁：{lock}") from exc
        else:
            raise UIBlocked(f"已有另一个工作流实例运行：{lock}")
    try:
        os.write(fd, f"pid={os.getpid()} time={time.time()}".encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def run_one(config_path: str, validation_run: bool = False) -> str:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    store = Store(config["database"])
    try:
        return _run_one(config, store, validation_run=validation_run)
    finally:
        store.db.close()


def audit_accounts(config_path: str) -> str:
    """Switch through configured logged-in accounts without opening generation."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    labels = [item["label"] for item in config.get("accounts", [])]
    if not labels:
        raise ValueError("配置中没有账号")
    store = Store(config["database"])
    try:
        with _run_lock(Path(config["database"]).with_suffix(".lock")):
            ui = DoubaoUI(config.get("ui_calibration", "configs/ui_calibration.json"), dry_run=False)
            restore_label = str(config.get("account_audit_restore") or labels[0])
            result = AccountInspector(ui).audit_accounts(labels, restore_original=True, restore_label=restore_label)
            for label in labels:
                store.set_account_state(label, "verified", "account audit passed")
            store.event(None, "account_audit", f"六账号盘点通过并恢复原账号：{result['original']}")
            return json.dumps(result, ensure_ascii=False)
    finally:
        store.db.close()


def _run_one(config: dict, store: Store, validation_run: bool = False) -> str:
    with _run_lock(Path(config["database"]).with_suffix(".lock")):
        automation = config.get("automation") or {}
        if automation.get("allow_submit") is not True:
            return "BLOCKED_SUBMIT_DISABLED"
        if automation.get("staged_validation_passed") is not True and not validation_run:
            return "BLOCKED_STAGED_VALIDATION"
        if validation_run:
            store.event(None, "validation", "受控单任务验收运行；未打开开机批量阶段闸门")
        pending = store.pending_tasks()
        if pending:
            task_ids = ",".join(row["task_id"] for row in pending[:3])
            return f"BLOCKED_PENDING_RECOVERY: {task_ids}"
        row = store.next_ready()
        if row is None:
            return "NO_READY_TASK"
        references = json.loads(row["references_json"])
        if any(str(item).startswith("UNMAPPED_REFERENCE_") for item in references):
            store.set_status(row["task_id"], "manual", "参考图编号未映射")
            return "TASK_MANUAL_REFERENCE"
        if "本镜存在未匹配实体" in row["prompt"]:
            store.set_status(row["task_id"], "manual", "分镜实体缺少可确认参考图")
            store.event(row["task_id"], "asset", "阻断生成，先补齐人物/场景/道具参考图")
            return "TASK_MANUAL_UNRESOLVED_ASSET"
        missing = [str(item) for item in references if not Path(str(item)).exists()]
        if missing:
            store.set_status(row["task_id"], "manual", f"参考图文件不存在：{missing[:3]}")
            store.event(row["task_id"], "asset", f"阻断上传，缺失参考图{len(missing)}个")
            return "TASK_MANUAL_MISSING_ASSET"
        max_refs = int(config.get("max_reference_images", 8))
        if len(references) > max_refs:
            store.set_status(row["task_id"], "manual", f"参考图数量{len(references)}超过上限{max_refs}")
            store.event(row["task_id"], "asset", "阻断上传，需人工挑选最相关参考图")
            return "TASK_MANUAL_TOO_MANY_ASSETS"
        candidates = store.account_candidates()
        if not candidates:
            return "NO_ACCOUNT_BUDGET"
        ui = DoubaoUI(config.get("ui_calibration", "configs/ui_calibration.json"), dry_run=False)
        task = DoubaoTaskSpec(row["prompt"], references, row["model"], f"{row['duration_seconds']}s", row["aspect_ratio"])
        discovery_dirs = config.get("download_discovery_dirs") or [config.get("download_dir", "")]
        before = snapshot_mp4([item for item in discovery_dirs if item])
        account = None
        try:
            inspector = AccountInspector(ui)
            for candidate in candidates:
                candidate_label = candidate["label"]
                try:
                    inspector.ensure_account(candidate_label)
                except AccountUIError as exc:
                    store.set_account_state(candidate_label, "login_required" if exc.kind == "not_logged_in" else "ui_cooldown", str(exc), 5)
                    store.event(row["task_id"], "account", f"跳过账号{candidate_label}：{exc.kind}")
                    continue
                account = candidate_label
                ui.prepare_task(task)
                break
            if account is None:
                return "NO_USABLE_ACCOUNT"
            store.begin_submission_attempt(row["task_id"], account)
            result = ui.submit(task)
            submitted_after = ui.last_submit_at or time.time()
            store.record_submission(account, row["task_id"], submitted_at=datetime.fromtimestamp(submitted_after, tz=timezone.utc).isoformat())
            store.event(row["task_id"], "ui", f"账号{account}确认提交：{result}；已消耗1个生成额度")
            if config.get("download", {}).get("enabled", False):
                try:
                    ui.download_latest(int(config.get("download", {}).get("button_timeout_seconds", 60)), expected_prompt=row["prompt"])
                    source = wait_for_new_mp4_in_dirs(discovery_dirs, before, int(config.get("download", {}).get("timeout_seconds", 900)), submitted_after=submitted_after)
                    output = archive_download(source, config["output_dir"], int(row["episode"]), int(row["group_index"]), expected_duration=float(row["duration_seconds"]), **_quality_options(config, row["task_id"]))
                except QualityReviewRequired as exc:
                    store.set_status(row["task_id"], "quality_review", str(exc))
                    store.event(row["task_id"], "quality", str(exc))
                    return f"TASK_QUALITY_REVIEW: {exc}"
                except MediaRejected as exc:
                    store.set_status(row["task_id"], "media_rejected", str(exc))
                    store.event(row["task_id"], "quality", str(exc))
                    return f"TASK_MEDIA_REJECTED: {exc}"
                except RuntimeError as exc:
                    store.set_status(row["task_id"], "download_pending", str(exc))
                    store.event(row["task_id"], "archive", f"已提交但下载/验收待处理：{exc}")
                    return f"DOWNLOAD_PENDING: {exc}"
                except Exception as exc:
                    # The quota was already consumed. Preserve that fact and
                    # leave a recoverable state instead of making the task
                    # look retryable or generating a duplicate.
                    store.set_status(row["task_id"], "download_pending", str(exc))
                    store.event(row["task_id"], "archive", f"已提交但下载/验收待处理：{exc}")
                    return f"DOWNLOAD_PENDING: {exc}"
                store.set_status(row["task_id"], "completed", output_path=str(output))
                store.event(row["task_id"], "archive", f"验收并归档：{output}")
                return f"COMPLETED: {output}"
            return "SUBMITTED_AWAITING_DOWNLOAD"
        except QuotaExhausted as exc:
            store.clear_submission_attempt(row["task_id"])
            store.set_account_state(account, "quota_exhausted", str(exc))
            store.event(row["task_id"], "account", f"账号{account}额度耗尽，释放任务并轮换账号")
            return f"ACCOUNT_QUOTA_EXHAUSTED: {exc}"
        except SubmissionUnconfirmed as exc:
            store.mark_submission_unconfirmed(
                account,
                row["task_id"],
                str(exc),
                submitted_at=datetime.now(timezone.utc).isoformat(),
            )
            store.event(row["task_id"], "ui", str(exc))
            return f"SUBMITTED_UNCONFIRMED: {exc}"
        except ModerationBlocked as exc:
            store.clear_submission_attempt(row["task_id"])
            status = "moderation_blocked"
            store.set_status(row["task_id"], status, str(exc))
            store.event(row["task_id"], "ui", str(exc))
            return f"TASK_MODERATION_BLOCKED: {exc}"
        except UIBlocked as exc:
            if account and "账号" in str(exc) and "提交" not in str(exc):
                try:
                    store.set_account_state(account, "ui_cooldown", str(exc), 5)
                except Exception:
                    pass
            store.set_status(row["task_id"], "paused", str(exc))
            store.event(row["task_id"], "ui", str(exc))
            return f"GLOBAL_BLOCKED: {exc}"
        except Exception as exc:
            store.set_status(row["task_id"], "failed", str(exc))
            store.event(row["task_id"], "workflow", str(exc))
            return f"FAILED: {exc}"


def validate_one(config_path: str) -> str:
    """Run one quota-capped validation task without opening the startup gate."""
    return run_one(config_path, validation_run=True)


def confirm_generated(config_path: str, task_id: str, account: str, output_path: str | None = None) -> str:
    """Record a user-verified generation without clicking or retrying the UI."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    store = Store(config["database"])
    try:
        row = next((item for item in store.tasks() if item["task_id"] == task_id), None)
        if row is None:
            raise ValueError(f"任务不存在: {task_id}")
        if row["status"] in {"completed", "submitted", "download_pending"} and row["quota_recorded"]:
            return f"ALREADY_RECORDED: {task_id} status={row['status']}"
        if row["status"] not in {"ready", "paused", "submitted_unconfirmed"}:
            raise ValueError(f"任务状态不允许人工确认: {task_id} status={row['status']}")
        labels = {item["label"] for item in config.get("accounts", [])}
        if account not in labels:
            raise ValueError(f"账号不在配置中: {account}")
        if output_path and not Path(output_path).exists():
            raise ValueError(f"成片文件不存在: {output_path}")
        if row["submitted_account"] and row["submitted_account"] != account:
            raise ValueError(f"任务已绑定其他提交账号: {row['submitted_account']}")
        store.record_submission(account, task_id, submitted_at=datetime.now(timezone.utc).isoformat())
        store.set_status(task_id, "download_pending", "用户确认豆包已生成；等待下载或人工归档", output_path=output_path)
        store.event(task_id, "manual_confirmation", f"用户确认已生成；账号={account}；已记1次额度；未再次点击提交")
        return f"CONFIRMED_GENERATED: {task_id} account={account}"
    finally:
        store.db.close()


def archive_pending(config_path: str, task_id: str, source_path: str) -> str:
    """Archive an already-generated MP4 without submitting another task."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    store = Store(config["database"])
    try:
        row = next((item for item in store.tasks() if item["task_id"] == task_id), None)
        if row is None:
            raise ValueError(f"任务不存在: {task_id}")
        if row["status"] not in {"download_pending", "submitted", "submitted_unconfirmed"}:
            raise ValueError(f"任务不是待下载状态: {task_id} status={row['status']}")
        if not row["quota_recorded"] and not row["submitted_account"]:
            raise ValueError(f"未确认提交任务缺少提交账号，拒绝归档: {task_id}")
        quality_options = _quality_options(config, row["task_id"])
        if quality_options:
            output = archive_download(source_path, config["output_dir"], int(row["episode"]), int(row["group_index"]), expected_duration=float(row["duration_seconds"]), **quality_options)
        else:
            output = archive_download(source_path, config["output_dir"], int(row["episode"]), int(row["group_index"]))
        if not row["quota_recorded"]:
            store.record_submission(row["submitted_account"], task_id, submitted_at=row["submitted_at"])
        store.set_status(task_id, "completed", output_path=str(output))
        store.event(task_id, "archive", f"人工选择下载文件并验收归档：{output}")
        return f"COMPLETED: {output}"
    finally:
        store.db.close()


def download_pending(config_path: str) -> str:
    """Download and archive the oldest already-submitted task, without submitting."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    store = Store(config["database"])
    try:
        with _run_lock(Path(config["database"]).with_suffix(".lock")):
            result, _ui = _recover_one_pending(config, store, None)
            return result
    finally:
        store.db.close()


def drain_pending(config_path: str, maximum: int = 10) -> str:
    """Recover pending tasks serially while holding one workflow lock."""
    if maximum < 1:
        raise ValueError("maximum must be at least 1")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    store = Store(config["database"])
    recovered = 0
    ui = None
    try:
        with _run_lock(Path(config["database"]).with_suffix(".lock")):
            while True:
                rows = store.pending_tasks()
                if not rows:
                    return "NO_PENDING_DOWNLOAD" if recovered == 0 else f"RECOVERY_DRAINED: count={recovered}"
                if recovered >= maximum:
                    return f"DOWNLOAD_PENDING: recovery cap reached count={recovered} remaining={len(rows)}"
                result, ui = _recover_one_pending(config, store, ui)
                if not result.startswith(("COMPLETED:", "TASK_QUALITY_REVIEW:", "TASK_RETRY_UNCONFIRMED:")):
                    return result
                recovered += 1
    finally:
        store.db.close()


def _recover_one_pending(config: dict, store: Store, ui):
    rows = store.pending_tasks()
    if not rows:
        return "NO_PENDING_DOWNLOAD", ui
    missing_owner = [row["task_id"] for row in rows if not row["submitted_account"]]
    if missing_owner:
        return f"DOWNLOAD_PENDING: 待下载任务缺少提交账号：{','.join(missing_owner[:3])}", ui
    if ui is None:
        ui = DoubaoUI(config.get("ui_calibration", "configs/ui_calibration.json"), dry_run=False)
    row = None
    accounts = list(dict.fromkeys(candidate["submitted_account"] for candidate in rows))
    for account in accounts:
        try:
            ui.switch_account(account)
            for candidate in rows:
                if candidate["submitted_account"] != account:
                    continue
                moderation = ui.moderation_for_prompt(candidate["prompt"])
                if moderation:
                    if store.revert_submission(account, candidate["task_id"], moderation):
                        store.event(candidate["task_id"], "account", f"恢复阶段识别到审核拦截，撤销误记额度：{moderation}")
                    return f"TASK_MODERATION_BLOCKED: {moderation}", ui
            matches = [
                candidate for candidate in rows
                if candidate["submitted_account"] == account and ui.completed_prompt_visible(candidate["prompt"])
            ]
            if not matches:
                for candidate in rows:
                    if candidate["submitted_account"] != account:
                        continue
                    if ui.open_prompt_search(candidate["prompt"]):
                        moderation = ui.moderation_for_prompt(candidate["prompt"])
                        if moderation:
                            if store.revert_submission(account, candidate["task_id"], moderation):
                                store.event(candidate["task_id"], "account", f"恢复阶段识别到审核拦截，撤销误记额度：{moderation}")
                            return f"TASK_MODERATION_BLOCKED: {moderation}", ui
                        matches = [candidate]
                        break
        except Exception as exc:
            store.event(None, "recovery", f"账号{account}页面恢复失败：{exc}")
            return f"DOWNLOAD_PENDING: 账号页面恢复失败：{account}：{exc}", ui
        if len(matches) > 1:
            ids = ",".join(candidate["task_id"] for candidate in matches[:3])
            return f"DOWNLOAD_PENDING: 同一账号存在多个匹配卡片，拒绝自动下载：{ids}", ui
        if matches:
            row = matches[0]
            break
    if row is None:
        retry_limit = int(config.get("automation", {}).get("unconfirmed_retry_limit", 0))
        retryable = [candidate for candidate in rows if candidate["status"] == "submitted_unconfirmed" and not candidate["quota_recorded"] and store.recovery_retry_count(candidate["task_id"]) < retry_limit]
        if retryable:
            candidate = retryable[0]
            store.clear_submission_attempt(candidate["task_id"])
            store.set_status(candidate["task_id"], "ready", "未确认提交且未发现完成卡片，自动重试一次")
            store.event(candidate["task_id"], "recovery", "未确认提交无完成卡片证据，释放任务进入一次性重试")
            return f"TASK_RETRY_UNCONFIRMED: {candidate['task_id']}", ui
        if any(candidate["status"] == "submitted_unconfirmed" for candidate in rows):
            return "DOWNLOAD_PENDING: 提交结果未确认，禁止自动重试；需人工确认已生成后再登记", ui
        return "DOWNLOAD_PENDING: 未找到与待下载任务匹配的已完成卡片", ui
    discovery_dirs = config.get("download_discovery_dirs") or [config.get("download_dir", "")]
    before = snapshot_mp4(discovery_dirs)
    try:
        if not row["quota_recorded"]:
            store.record_submission(row["submitted_account"], row["task_id"], submitted_at=row["submitted_at"])
        ui.download_latest(int(config.get("download", {}).get("button_timeout_seconds", 60)), expected_prompt=row["prompt"])
        submitted_after = time.time() - 900
        if row["submitted_at"]:
            try:
                submitted_after = datetime.fromisoformat(row["submitted_at"]).timestamp()
            except ValueError:
                pass
        source = wait_for_new_mp4_in_dirs(discovery_dirs, before, int(config.get("download", {}).get("timeout_seconds", 900)), submitted_after=submitted_after)
        output = archive_download(source, config["output_dir"], int(row["episode"]), int(row["group_index"]), expected_duration=float(row["duration_seconds"]), **_quality_options(config, row["task_id"]))
        store.set_status(row["task_id"], "completed", output_path=str(output))
        store.event(row["task_id"], "archive", f"恢复下载并归档：{output}")
        return f"COMPLETED: {output}", ui
    except QualityReviewRequired as exc:
        store.set_status(row["task_id"], "quality_review", str(exc))
        store.event(row["task_id"], "quality", str(exc))
        return f"TASK_QUALITY_REVIEW: {exc}", ui
    except MediaRejected as exc:
        store.set_status(row["task_id"], "media_rejected", str(exc))
        store.event(row["task_id"], "quality", str(exc))
        return f"TASK_MEDIA_REJECTED: {exc}", ui
    except RuntimeError as exc:
        store.set_status(row["task_id"], "download_pending", str(exc))
        store.event(row["task_id"], "archive", f"待下载恢复失败：{exc}")
        return f"DOWNLOAD_PENDING: {exc}", ui
    except Exception as exc:
        store.set_status(row["task_id"], "download_pending", str(exc))
        store.event(row["task_id"], "archive", f"待下载恢复失败：{exc}")
        return f"DOWNLOAD_PENDING: {exc}", ui
