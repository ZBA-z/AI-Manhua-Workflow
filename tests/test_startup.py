import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.domain import Shot, ShotGroup
from src.startup import RunAction, classify_run_result, decide_startup, run_startup, within_schedule_window
from src.store import Store


class StartupTests(unittest.TestCase):
  def test_schedule_window_is_15_to_midnight(self):
    config = {"automation": {"schedule_window_enabled": True, "schedule_start_hour": 15, "schedule_end_hour": 24}}
    from datetime import datetime
    self.assertFalse(within_schedule_window(config, datetime(2026, 8, 29, 14, 59)))
    self.assertTrue(within_schedule_window(config, datetime(2026, 8, 29, 15, 0)))
    self.assertTrue(within_schedule_window(config, datetime(2026, 8, 29, 23, 59)))
    self.assertFalse(within_schedule_window(config, datetime(2026, 8, 30, 0, 0)))

  def test_activation_date_blocks_today_and_allows_tomorrow(self):
    config = {"automation": {"schedule_window_enabled": True, "activation_date": "2026-08-30", "schedule_start_hour": 15, "schedule_end_hour": 24}}
    from datetime import datetime
    self.assertFalse(within_schedule_window(config, datetime(2026, 8, 29, 23, 59)))
    self.assertFalse(within_schedule_window(config, datetime(2026, 8, 30, 14, 59)))
    self.assertTrue(within_schedule_window(config, datetime(2026, 8, 30, 15, 0)))

  def test_outside_schedule_window_needs_no_gui(self):
    config = self._config(Path("D:/tmp"), staged=True)
    config["automation"].update({"schedule_window_enabled": True, "schedule_start_hour": 15, "schedule_end_hour": 24})
    with patch("src.startup.datetime") as now:
      now.now.return_value = __import__("datetime").datetime(2026, 8, 29, 9, 0)
      store = Store(":memory:")
      decision = decide_startup(config, store)
      store.db.close()
    self.assertEqual(decision.reason, "outside_schedule_window")
  def _config(self, root: Path, staged: bool = False) -> dict:
    return {
      "database": str(root / "workflow.db"),
      "output_dir": str(root / "output"),
      "automation": {
        "enabled": True,
        "allow_submit": True,
        "staged_validation_passed": staged,
        "launch_doubao_on_startup": True,
        "max_submissions_per_start": 1,
      },
    }

  def test_closed_staging_without_pending_needs_no_gui(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root)
      store = Store(config["database"])
      decision = decide_startup(config, store)
      store.db.close()
      self.assertFalse(decision.needs_gui)
      self.assertEqual(decision.reason, "staged_validation_required")

  def test_pending_recovery_needs_gui_even_when_staging_is_closed(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root)
      store = Store(config["database"])
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.set_status(group.task_id, "download_pending", "waiting")
      decision = decide_startup(config, store)
      store.db.close()
      self.assertTrue(decision.needs_gui)
      self.assertEqual(decision.reason, "pending_recovery")

  def test_open_staging_with_ready_task_needs_gui(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root, staged=True)
      store = Store(config["database"])
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      decision = decide_startup(config, store)
      store.db.close()
      self.assertTrue(decision.needs_gui)
      self.assertEqual(decision.reason, "ready_to_submit")

  def test_safe_no_work_run_never_creates_doubao_ui_and_writes_terminal_status(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root)
      config_path = root / "config.json"
      config_path.write_text(json.dumps(config), encoding="utf-8")
      with patch("src.startup.DoubaoUI") as ui:
        result = run_startup(str(config_path), prepare_callback=lambda _path: None)
      ui.assert_not_called()
      self.assertEqual(result.state, "no_work")
      self.assertEqual(result.exit_code, 0)
      status = json.loads((root / "startup-status.json").read_text(encoding="utf-8"))
      self.assertEqual(status["run_id"], result.run_id)
      self.assertEqual(status["state"], "no_work")
      self.assertIn("prepare", status["stage_seconds"])
      self.assertEqual(status["before"]["pending"], 0)
      self.assertEqual(status["after"]["pending"], 0)

  def test_each_run_gets_a_distinct_run_id(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root)
      config_path = root / "config.json"
      config_path.write_text(json.dumps(config), encoding="utf-8")
      first = run_startup(str(config_path), prepare_callback=lambda _path: None)
      second = run_startup(str(config_path), prepare_callback=lambda _path: None)
      self.assertNotEqual(first.run_id, second.run_id)

  def test_dead_previous_running_status_is_cleared_and_new_run_finishes(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root)
      config_path = root / "config.json"
      config_path.write_text(json.dumps(config), encoding="utf-8")
      status_path = root / "startup-status.json"
      status_path.write_text(json.dumps({
        "run_id": "previous", "state": "running", "semantic": None,
        "pid": 99999999,
      }), encoding="utf-8")
      result = run_startup(str(config_path), prepare_callback=lambda _path: None)
      self.assertEqual(result.state, "no_work")
      status = json.loads(status_path.read_text(encoding="utf-8"))
      self.assertEqual(status["run_id"], result.run_id)

  def test_failed_prepare_does_not_create_the_workflow_database(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root)
      config_path = root / "config.json"
      config_path.write_text(json.dumps(config), encoding="utf-8")

      def fail_prepare(_path):
        raise RuntimeError("invalid input")

      result = run_startup(str(config_path), prepare_callback=fail_prepare)
      self.assertEqual(result.state, "failed")
      self.assertEqual(result.exit_code, 1)
      self.assertFalse(Path(config["database"]).exists())

  def test_recovery_block_writes_terminal_state_and_never_submits(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root)
      config_path = root / "config.json"
      config_path.write_text(json.dumps(config), encoding="utf-8")
      store = Store(config["database"])
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.set_status(group.task_id, "download_pending", "waiting")
      store.db.close()
      with patch("src.startup._ensure_doubao_session"), \
           patch("src.startup.drain_pending", return_value="DOWNLOAD_PENDING: ambiguous"), \
           patch("src.startup.run_one") as submit:
        result = run_startup(str(config_path), prepare_callback=lambda _path: None)
      self.assertEqual(result.state, "blocked")
      self.assertEqual(result.exit_code, 0)
      submit.assert_not_called()
      status = json.loads((root / "startup-status.json").read_text(encoding="utf-8"))
      self.assertEqual(status["state"], "blocked")
      self.assertEqual(status["semantic"], "DOWNLOAD_PENDING: ambiguous")

  def test_click_pending_fence_requires_recovery_and_is_reported(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root)
      store = Store(config["database"])
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.sync_accounts([{"label": "沉雪", "switch_order": 1}], "2026-08-28")
      store.begin_submission_attempt(group.task_id, "沉雪")
      decision = decide_startup(config, store)
      store.db.close()
      self.assertTrue(decision.needs_gui)
      self.assertEqual(decision.reason, "pending_recovery")

  def test_locked_desktop_stops_before_doubao_ui(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root, staged=True)
      config["automation"]["require_unlocked_desktop"] = True
      config_path = root / "config.json"
      config_path.write_text(json.dumps(config), encoding="utf-8")
      store = Store(config["database"])
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.db.close()
      with patch("src.startup.desktop_is_interactive", return_value=False), \
           patch("src.startup.DoubaoUI") as ui:
        result = run_startup(str(config_path), prepare_callback=lambda _path: None)
      ui.assert_not_called()
      self.assertEqual(result.state, "blocked")
      self.assertEqual(result.semantic, "DESKTOP_LOCKED")

  def test_machine_results_have_stable_batch_actions(self):
    cases = {
      "COMPLETED: x.mp4": RunAction.CONTINUE,
      "TASK_MODERATION_BLOCKED: policy": RunAction.SKIP,
      "TASK_MANUAL_MISSING_ASSET": RunAction.SKIP,
      "TASK_QUALITY_REVIEW: black": RunAction.SKIP,
      "TASK_MEDIA_REJECTED: decode": RunAction.SKIP,
      "ACCOUNT_QUOTA_EXHAUSTED: account": RunAction.CONTINUE,
      "NO_READY_TASK": RunAction.FINISH,
      "NO_ACCOUNT_BUDGET": RunAction.FINISH,
      "DOWNLOAD_PENDING: ambiguous": RunAction.STOP,
      "SUBMITTED_UNCONFIRMED: unknown": RunAction.STOP,
      "GLOBAL_BLOCKED: parameters": RunAction.STOP,
    }
    for semantic, expected in cases.items():
      with self.subTest(semantic=semantic):
        self.assertEqual(classify_run_result(semantic), expected)

  def test_batch_continues_after_task_block_and_finishes_without_budget(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root, staged=True)
      config["automation"]["max_submissions_per_start"] = 18
      config["automation"]["require_unlocked_desktop"] = True
      config_path = root / "config.json"
      config_path.write_text(json.dumps(config), encoding="utf-8")
      store = Store(config["database"])
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.db.close()
      sequence = [
        "COMPLETED: one.mp4",
        "TASK_MODERATION_BLOCKED: policy",
        "COMPLETED: two.mp4",
        "NO_ACCOUNT_BUDGET",
      ]
      with patch("src.startup.desktop_is_interactive", return_value=True), \
           patch("src.startup._ensure_doubao_session"), \
           patch("src.startup.run_one", side_effect=sequence) as run:
        result = run_startup(str(config_path), prepare_callback=lambda _path: None)
      self.assertEqual(run.call_count, 4)
      self.assertEqual(result.state, "success")
      self.assertIn("completed=2", result.semantic)
      self.assertIn("skipped=1", result.semantic)

  def test_batch_does_not_count_quota_exhaustion_as_completed(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root, staged=True)
      config["automation"]["max_submissions_per_start"] = 2
      config["automation"]["require_unlocked_desktop"] = True
      config_path = root / "config.json"
      config_path.write_text(json.dumps(config), encoding="utf-8")
      store = Store(config["database"])
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.db.close()
      sequence = ["ACCOUNT_QUOTA_EXHAUSTED: 沉雪", "NO_ACCOUNT_BUDGET"]
      with patch("src.startup.desktop_is_interactive", return_value=True), \
           patch("src.startup._ensure_doubao_session"), \
           patch("src.startup.run_one", side_effect=sequence) as run:
        result = run_startup(str(config_path), prepare_callback=lambda _path: None)
      self.assertEqual(run.call_count, 2)
      self.assertEqual(result.state, "success")
      self.assertIn("completed=0", result.semantic)
      self.assertIn("attempted=2", result.semantic)

  def test_batch_stops_immediately_after_unconfirmed_submission(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = self._config(root, staged=True)
      config["automation"]["max_submissions_per_start"] = 18
      config_path = root / "config.json"
      config_path.write_text(json.dumps(config), encoding="utf-8")
      store = Store(config["database"])
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.db.close()
      with patch("src.startup.desktop_is_interactive", return_value=True), \
           patch("src.startup._ensure_doubao_session"), \
           patch("src.startup.run_one", side_effect=["COMPLETED: one.mp4", "SUBMITTED_UNCONFIRMED: unknown"]) as run:
        result = run_startup(str(config_path), prepare_callback=lambda _path: None)
      self.assertEqual(run.call_count, 2)
      self.assertEqual(result.state, "blocked")
      self.assertIn("SUBMITTED_UNCONFIRMED", result.semantic)


if __name__ == "__main__":
  unittest.main()
