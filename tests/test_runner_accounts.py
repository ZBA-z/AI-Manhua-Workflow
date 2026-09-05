import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.domain import Shot, ShotGroup
from src.doubao_ui import AccountUIError, ModerationBlocked, SubmissionUnconfirmed, UIBlocked
from src.runner import run_one
from src.store import Store


class BaseFakeUI:
  switched = []
  submitted = 0

  def __init__(self, *_args, **_kwargs):
    self.current = "沉雪"
    self.last_submit_at = None

  def current_account_label(self):
    return self.current

  def switch_account(self, label):
    type(self).switched.append(label)
    self.current = label

  def prepare_task(self, _task):
    return None

  def submit(self, _task):
    type(self).submitted += 1
    return "SUBMITTED_CONFIRMED"


class RunnerAccountTests(unittest.TestCase):
  def setUp(self):
    BaseFakeUI.switched = []
    BaseFakeUI.submitted = 0

  def _fixture(self, folder):
    root = Path(folder)
    database = root / "workflow.db"
    store = Store(database)
    group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
    store.upsert_group(group, "prompt", [])
    store.sync_accounts([
      {"label": "沉雪", "switch_order": 1},
      {"label": "为你", "switch_order": 2},
    ], date.today().isoformat())
    store.db.close()
    config = root / "config.json"
    config.write_text(json.dumps({
      "database": str(database),
      "accounts": [{"label": "沉雪"}, {"label": "为你"}],
      "automation": {"allow_submit": True, "staged_validation_passed": True},
      "download": {"enabled": False},
      "ui_calibration": str(root / "ui.json"),
    }), encoding="utf-8")
    return config, database

  def test_pre_submit_account_failure_rotates_to_next_account(self):
    class RotateUI(BaseFakeUI):
      def __init__(self, *_args, **_kwargs):
        super().__init__()
        self.current = "未识别"

      def switch_account(self, label):
        type(self).switched.append(label)
        if label == "沉雪":
          raise AccountUIError("switch_failed", "ambiguous")
        self.current = label

    with tempfile.TemporaryDirectory() as folder:
      config, database = self._fixture(folder)
      with patch("src.runner.DoubaoUI", RotateUI), patch("src.runner.snapshot_mp4", return_value={}):
        self.assertEqual(run_one(str(config)), "SUBMITTED_AWAITING_DOWNLOAD")
      store = Store(database)
      row = store.tasks()[0]
      self.assertEqual(row["submitted_account"], "为你")
      self.assertEqual(store.db.execute("select failure_count from accounts where label='沉雪'").fetchone()[0], 0)
      store.db.close()

  def test_moderation_does_not_penalize_or_rotate_account(self):
    class ModeratedUI(BaseFakeUI):
      def submit(self, _task):
        type(self).submitted += 1
        raise ModerationBlocked("审核拦截，额度未扣除")

    with tempfile.TemporaryDirectory() as folder:
      config, database = self._fixture(folder)
      with patch("src.runner.DoubaoUI", ModeratedUI), patch("src.runner.snapshot_mp4", return_value={}):
        self.assertTrue(run_one(str(config)).startswith("TASK_MODERATION_BLOCKED:"))
      store = Store(database)
      task = store.tasks()[0]
      self.assertEqual(task["status"], "moderation_blocked")
      self.assertIsNone(task["attempt_state"])
      self.assertEqual(sum(row[0] for row in store.db.execute("select failure_count from accounts")), 0)
      self.assertEqual(ModeratedUI.submitted, 1)
      store.db.close()

  def test_unconfirmed_submit_binds_account_and_blocks_restart(self):
    class UnconfirmedUI(BaseFakeUI):
      def submit(self, _task):
        type(self).submitted += 1
        raise SubmissionUnconfirmed("unknown")

    with tempfile.TemporaryDirectory() as folder:
      config, database = self._fixture(folder)
      with patch("src.runner.DoubaoUI", UnconfirmedUI), patch("src.runner.snapshot_mp4", return_value={}):
        self.assertIn("SUBMITTED_UNCONFIRMED", run_one(str(config)))
        self.assertIn("BLOCKED_PENDING_RECOVERY", run_one(str(config)))
      store = Store(database)
      task = store.tasks()[0]
      self.assertEqual(task["submitted_account"], "沉雪")
      self.assertEqual(task["attempt_state"], "submitted_unconfirmed")
      self.assertEqual(UnconfirmedUI.submitted, 1)
      store.db.close()

  def test_task_prepare_failure_does_not_rotate(self):
    class PrepareFailureUI(BaseFakeUI):
      def prepare_task(self, _task):
        raise UIBlocked("参数无法验证")

    with tempfile.TemporaryDirectory() as folder:
      config, database = self._fixture(folder)
      with patch("src.runner.DoubaoUI", PrepareFailureUI), patch("src.runner.snapshot_mp4", return_value={}):
        self.assertIn("参数无法验证", run_one(str(config)))
      self.assertEqual(PrepareFailureUI.switched, [])
      self.assertEqual(PrepareFailureUI.submitted, 0)


if __name__ == "__main__":
  unittest.main()
