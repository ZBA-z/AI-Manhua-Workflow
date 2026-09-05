import json
import tempfile
import unittest
from pathlib import Path

from src.domain import Shot, ShotGroup
from src.runner import confirm_generated
from src.store import Store


class RunnerConfirmationTests(unittest.TestCase):
  def test_user_confirmation_records_once_without_retry(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      store = Store(database)
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "paused")
      store.upsert_group(group, "prompt", [])
      store.set_status(group.task_id, "paused", "UI")
      store.sync_accounts([{"label": "沉雪", "switch_order": 1}], "2026-08-27")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({"database": str(database), "accounts": [{"label": "沉雪"}]}), encoding="utf-8")
      self.assertTrue(confirm_generated(str(config), group.task_id, "沉雪").startswith("CONFIRMED_GENERATED"))
      reopened = Store(database)
      row = reopened.tasks()[0]
      self.assertEqual(row["status"], "download_pending")
      self.assertIsNotNone(row["submitted_at"])
      self.assertEqual(reopened.db.execute("select success_count from accounts where label=?", ("沉雪",)).fetchone()[0], 1)
      reopened.db.close()
      self.assertTrue(confirm_generated(str(config), group.task_id, "沉雪").startswith("ALREADY_RECORDED"))
