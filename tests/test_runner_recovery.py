import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.domain import Shot, ShotGroup
from src.runner import drain_pending, download_pending, run_one, validate_one
from src.store import Store


class RunnerRecoveryTests(unittest.TestCase):
  def _group(self, episode, index, prompt, status="ready"):
    return ShotGroup(episode, index, [Shot(episode, "x", str(index), 10, "", prompt)], 10, 10, status)

  def test_run_one_refuses_new_submit_while_recovery_is_pending(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      pending = self._group(4, 1, "pending")
      ready = self._group(4, 2, "ready")
      store.upsert_group(pending, "pending", [])
      store.upsert_group(ready, "ready", [])
      store.set_status(pending.task_id, "download_pending", "waiting")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({
        "database": str(database),
        "automation": {"allow_submit": True, "staged_validation_passed": True},
      }), encoding="utf-8")
      with patch("src.runner.DoubaoUI") as ui:
        result = run_one(str(config))
      self.assertTrue(result.startswith("BLOCKED_PENDING_RECOVERY:"))
      ui.assert_not_called()

  def test_run_one_requires_explicit_automation_gate(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      group = self._group(4, 1, "ready")
      store.upsert_group(group, "ready", [])
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({"database": str(database)}), encoding="utf-8")
      with patch("src.runner.DoubaoUI") as ui:
        result = run_one(str(config))
      self.assertEqual(result, "BLOCKED_SUBMIT_DISABLED")
      ui.assert_not_called()

  def test_run_one_cannot_bypass_staged_validation_gate(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      group = self._group(4, 1, "ready")
      store.upsert_group(group, "ready", [])
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({
        "database": str(database),
        "automation": {"allow_submit": True, "staged_validation_passed": False},
      }), encoding="utf-8")
      with patch("src.runner.DoubaoUI") as ui:
        result = run_one(str(config))
      self.assertEqual(result, "BLOCKED_STAGED_VALIDATION")
      ui.assert_not_called()

  def test_validate_one_bypasses_only_staging_flag(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      group = self._group(4, 1, "ready")
      store.upsert_group(group, "ready", [])
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({
        "database": str(database),
        "automation": {"allow_submit": False, "staged_validation_passed": False},
      }), encoding="utf-8")
      with patch("src.runner.DoubaoUI") as ui:
        result = validate_one(str(config))
      self.assertEqual(result, "BLOCKED_SUBMIT_DISABLED")
      ui.assert_not_called()

  def test_download_recovery_switches_to_submitting_account(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      output = root / "output"
      output.mkdir()
      source = root / "new.mp4"
      source.write_bytes(b"video")
      store = Store(database)
      first = self._group(4, 1, "prompt-one")
      second = self._group(4, 2, "prompt-two")
      store.upsert_group(first, "prompt-one", [])
      store.upsert_group(second, "prompt-two", [])
      store.sync_accounts([
        {"label": "沉雪", "switch_order": 1},
        {"label": "为你", "switch_order": 2},
      ], "2026-08-28")
      store.record_submission("沉雪", first.task_id, "2026-08-28T01:00:00+00:00")
      store.mark_submission_unconfirmed("为你", second.task_id, "click not confirmed", "2026-08-28T01:01:00+00:00")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({
        "database": str(database),
        "output_dir": str(output),
        "download_discovery_dirs": [str(root)],
        "download": {"button_timeout_seconds": 1, "timeout_seconds": 1},
      }), encoding="utf-8")

      class FakeUI:
        switched = []

        def __init__(self, *_args, **_kwargs):
          self.current = None

        def switch_account(self, label):
          self.current = label
          self.switched.append(label)

        def completed_prompt_visible(self, prompt):
          return self.current == "为你" and prompt == "prompt-two"

        def moderation_for_prompt(self, _prompt):
          return None

        def open_prompt_search(self, _prompt):
          return False

        def download_latest(self, _timeout, expected_prompt=None):
          self.downloaded_prompt = expected_prompt
          return "DOWNLOAD_CLICKED"

      with patch("src.runner.DoubaoUI", FakeUI), \
           patch("src.runner.snapshot_mp4", return_value={}), \
           patch("src.runner.wait_for_new_mp4_in_dirs", return_value=source), \
           patch("src.runner.archive_download", return_value=output / "第四集2.mp4"):
        result = download_pending(str(config))

      self.assertTrue(result.startswith("COMPLETED:"))
      self.assertEqual(FakeUI.switched, ["沉雪", "为你"])
      reopened = Store(database)
      rows = {row["task_id"]: row for row in reopened.tasks()}
      self.assertEqual(rows[first.task_id]["status"], "submitted")
      self.assertEqual(rows[second.task_id]["status"], "completed")
      reopened.db.close()

  def test_unconfirmed_task_is_not_automatically_retried(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      group = self._group(4, 1, "prompt")
      store.upsert_group(group, "prompt", [])
      store.sync_accounts([{"label": "沉雪", "switch_order": 1}], "2026-08-28")
      store.mark_submission_unconfirmed("沉雪", group.task_id, "click not confirmed", "2026-08-28T01:00:00+00:00")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({
        "database": str(database),
        "automation": {"unconfirmed_retry_limit": 0},
      }), encoding="utf-8")
      with patch("src.runner.DoubaoUI") as ui:
        ui.return_value.moderation_for_prompt.return_value = None
        ui.return_value.completed_prompt_visible.return_value = False
        ui.return_value.open_prompt_search.return_value = False
        result = drain_pending(str(config))
      self.assertIn("禁止自动重试", result)
      reopened = Store(database)
      row = reopened.tasks()[0]
      self.assertEqual(row["status"], "submitted_unconfirmed")
      reopened.db.close()
      reopened.db.close()

  def test_download_recovery_blocks_when_submission_owner_is_unknown(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      group = self._group(4, 1, "prompt")
      store.upsert_group(group, "prompt", [])
      store.set_status(group.task_id, "download_pending", "waiting")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({"database": str(database)}), encoding="utf-8")
      with patch("src.runner.DoubaoUI") as ui:
        result = download_pending(str(config))
      self.assertIn("缺少提交账号", result)
      ui.assert_not_called()

  def test_download_recovery_converts_account_switch_error_to_pending(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      group = self._group(4, 1, "prompt")
      store.upsert_group(group, "prompt", [])
      store.sync_accounts([{"label": "沉雪", "switch_order": 1}], "2026-08-28")
      store.record_submission("沉雪", group.task_id, "2026-08-28T01:00:00+00:00")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({"database": str(database)}), encoding="utf-8")

      class BrokenUI:
        def __init__(self, *_args, **_kwargs):
          pass

        def switch_account(self, _label):
          raise RuntimeError("ambiguous account")

      with patch("src.runner.DoubaoUI", BrokenUI):
        result = download_pending(str(config))
      self.assertIn("账号页面恢复失败", result)
      reopened = Store(database)
      self.assertEqual(reopened.tasks()[0]["status"], "submitted")
      reopened.db.close()

  def test_download_recovery_reclassifies_moderation_and_reverts_quota(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      output = root / "output"
      output.mkdir()
      store = Store(database)
      prompt = "本组上传参考图清单：参考图1=时间族.png；参考图2=受损城市.png；参考图3=南宫镜.png"
      group = self._group(4, 1, prompt)
      store.upsert_group(group, prompt, [])
      store.sync_accounts([{"label": "为你", "switch_order": 1}], "2026-08-28")
      store.record_submission("为你", group.task_id, "2026-08-28T01:00:00+00:00")
      store.set_status(group.task_id, "download_pending", "waiting")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({
        "database": str(database),
        "output_dir": str(output),
        "download_discovery_dirs": [str(root)],
        "download": {"button_timeout_seconds": 1, "timeout_seconds": 1},
      }), encoding="utf-8")

      class FakeUI:
        def __init__(self, *_args, **_kwargs):
          pass

        def switch_account(self, _label):
          pass

        def moderation_for_prompt(self, _prompt):
          return "抱歉，由于版权相关限制，暂时无法创作对应的内容。"

        def completed_prompt_visible(self, _prompt):
          return False

        def open_prompt_search(self, _prompt):
          return False

      with patch("src.runner.DoubaoUI", FakeUI):
        result = download_pending(str(config))
      self.assertTrue(result.startswith("TASK_MODERATION_BLOCKED:"))
      reopened = Store(database)
      row = reopened.tasks()[0]
      self.assertEqual(row["status"], "moderation_blocked")
      self.assertEqual(row["quota_recorded"], 0)
      self.assertIsNone(row["submitted_account"])
      self.assertEqual(reopened.db.execute("SELECT success_count FROM accounts WHERE label='为你'").fetchone()[0], 0)
      reopened.db.close()

  def test_drain_pending_recovers_multiple_tasks_in_one_locked_batch(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      for index in (1, 2):
        group = self._group(4, index, f"prompt-{index}")
        store.upsert_group(group, f"prompt-{index}", [])
        store.set_status(group.task_id, "download_pending", "waiting")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({"database": str(database)}), encoding="utf-8")

      def recover_one(_config, active_store, _ui):
        row = active_store.pending_tasks()[0]
        active_store.set_status(row["task_id"], "completed")
        return f"COMPLETED: {row['task_id']}", object()

      with patch("src.runner._recover_one_pending", side_effect=recover_one) as recover:
        result = drain_pending(str(config), maximum=3)
      self.assertEqual(result, "RECOVERY_DRAINED: count=2")
      self.assertEqual(recover.call_count, 2)

  def test_drain_pending_empty_queue_does_not_create_ui(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      Store(database).db.close()
      config = root / "config.json"
      config.write_text(json.dumps({"database": str(database)}), encoding="utf-8")
      with patch("src.runner.DoubaoUI") as ui:
        result = drain_pending(str(config), maximum=3)
      self.assertEqual(result, "NO_PENDING_DOWNLOAD")
      ui.assert_not_called()

  def test_drain_pending_stops_at_cap_with_remaining_tasks(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      for index in (1, 2):
        group = self._group(4, index, f"prompt-{index}")
        store.upsert_group(group, f"prompt-{index}", [])
        store.set_status(group.task_id, "download_pending", "waiting")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({"database": str(database)}), encoding="utf-8")

      def recover_one(_config, active_store, _ui):
        row = active_store.pending_tasks()[0]
        active_store.set_status(row["task_id"], "completed")
        return f"COMPLETED: {row['task_id']}", object()

      with patch("src.runner._recover_one_pending", side_effect=recover_one):
        result = drain_pending(str(config), maximum=1)
      self.assertEqual(result, "DOWNLOAD_PENDING: recovery cap reached count=1 remaining=1")

  def test_drain_pending_stops_on_first_failure(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      group = self._group(4, 1, "prompt")
      store.upsert_group(group, "prompt", [])
      store.set_status(group.task_id, "download_pending", "waiting")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({"database": str(database)}), encoding="utf-8")
      with patch("src.runner._recover_one_pending", return_value=("DOWNLOAD_PENDING: ambiguous", object())) as recover:
        result = drain_pending(str(config), maximum=3)
      self.assertEqual(result, "DOWNLOAD_PENDING: ambiguous")
      self.assertEqual(recover.call_count, 1)
