import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Shot, ShotGroup
from src.store import Store


class StoreTests(unittest.TestCase):
  def test_manual_reason_is_persisted_and_cleared_when_group_becomes_ready(self):
    with tempfile.TemporaryDirectory() as folder:
      database = Path(folder) / "workflow.db"
      store = Store(database)
      manual = ShotGroup(4, 1, [Shot(4, "x", "1", 5, "", "prompt")], 5, 5, "manual", "需要人工确认时长")
      store.upsert_group(manual, "prompt", [], manual_reason=manual.adjustment)
      row = store.tasks()[0]
      self.assertEqual(row["status"], "manual")
      self.assertEqual(row["error"], "需要人工确认时长")

      ready = ShotGroup(4, 1, manual.shots, 10, 10, "ready")
      store.upsert_group(ready, "prompt", [], manual_reason=None)
      row = store.tasks()[0]
      self.assertEqual(row["status"], "ready")
      self.assertIsNone(row["error"])
      store.db.close()

  def test_status_and_duration_map_survive_upsert(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready", duration_map={"1": 10})
      store.upsert_group(group, "prompt", [], duration_seconds=10, aspect_ratio="16:9")
      store.set_status(group.task_id, "moderation_blocked", "未扣额度")
      store.upsert_group(group, "prompt-v2", [], duration_seconds=10, aspect_ratio="16:9")
      row = store.tasks()[0]
      self.assertEqual(row["status"], "moderation_blocked")
      self.assertEqual(row["duration_map_json"], '{"1": 10}')
      store.db.close()

  def test_paused_task_can_be_requeued_without_touching_blocked(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      paused = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "paused")
      blocked = ShotGroup(4, 2, [Shot(4, "x", "2", 10, "", "prompt")], 10, 10, "moderation_blocked")
      store.upsert_group(paused, "prompt", [])
      store.upsert_group(blocked, "prompt", [])
      store.set_status(paused.task_id, "paused", "UI")
      store.set_status(blocked.task_id, "moderation_blocked", "审核")
      store.set_status(paused.task_id, "ready", None)
      rows = {row["task_id"]: row["status"] for row in store.tasks()}
      self.assertEqual(rows[paused.task_id], "ready")
      self.assertEqual(rows[blocked.task_id], "moderation_blocked")
      store.db.close()

  def test_empty_plan_is_rejected_without_obsoleting_queue(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      with self.assertRaisesRegex(ValueError, "空任务计划"):
        store.sync_task_ids([])
      self.assertEqual(store.tasks()[0]["status"], "ready")
      store.db.close()

  def test_duplicate_event_is_written_once(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      store.event("ep004-g001", "asset", "same finding")
      store.event("ep004-g001", "asset", "same finding")
      self.assertEqual(store.db.execute("select count(*) from events").fetchone()[0], 1)
      store.db.close()

  def test_record_submission_rejects_unknown_account(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      with self.assertRaisesRegex(ValueError, "账号未登记"):
        store.record_submission("不存在", "ep004-g001")
      self.assertEqual(store.db.execute("select count(*) from accounts").fetchone()[0], 0)
      store.db.close()

  def test_record_submission_persists_owner_and_is_idempotent(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.sync_accounts([{"label": "沉雪", "switch_order": 1}], "2026-08-28")
      store.record_submission("沉雪", group.task_id, "2026-08-28T01:00:00+00:00")
      store.record_submission("沉雪", group.task_id, "2026-08-28T01:00:00+00:00")
      row = store.tasks()[0]
      self.assertEqual(row["submitted_account"], "沉雪")
      self.assertEqual(row["quota_recorded"], 1)
      self.assertEqual(store.db.execute("select success_count from accounts where label=?", ("沉雪",)).fetchone()[0], 1)
      store.db.close()

  def test_revert_submission_clears_quota_and_marks_moderation_blocked(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.sync_accounts([{"label": "沉雪", "switch_order": 1}], "2026-08-28")
      store.record_submission("沉雪", group.task_id, "2026-08-28T01:00:00+00:00")
      self.assertTrue(store.revert_submission("沉雪", group.task_id, "版权相关限制"))
      row = store.tasks()[0]
      self.assertEqual(row["status"], "moderation_blocked")
      self.assertEqual(row["quota_recorded"], 0)
      self.assertIsNone(row["submitted_account"])
      self.assertIn("版权相关限制", row["error"])
      self.assertEqual(store.db.execute("select success_count from accounts where label='沉雪'").fetchone()[0], 0)
      store.db.close()

  def test_unconfirmed_submission_keeps_account_without_charging_quota(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.sync_accounts([{"label": "沉雪", "switch_order": 1}], "2026-08-28")
      store.mark_submission_unconfirmed("沉雪", group.task_id, "confirmation missing", "2026-08-28T01:00:00+00:00")
      row = store.tasks()[0]
      self.assertEqual(row["status"], "submitted_unconfirmed")
      self.assertEqual(row["submitted_account"], "沉雪")
      self.assertEqual(row["quota_recorded"], 0)
      self.assertEqual(store.db.execute("select success_count from accounts where label=?", ("沉雪",)).fetchone()[0], 0)
      store.db.close()

  def test_account_candidates_skip_exhausted_and_fenced_accounts(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      for index, label in enumerate(("沉雪", "为你", "魏来"), 1):
        group = ShotGroup(4, index, [Shot(4, "x", str(index), 10, "", "prompt")], 10, 10, "ready")
        store.upsert_group(group, "prompt", [])
      store.sync_accounts([
        {"label": "沉雪", "switch_order": 1},
        {"label": "为你", "switch_order": 2},
        {"label": "魏来", "switch_order": 3},
      ], datetime.now(timezone(timedelta(hours=8))).date().isoformat())
      store.set_account_state("沉雪", "quota_exhausted", "daily limit")
      candidates = store.account_candidates()
      self.assertEqual([row["label"] for row in candidates], ["为你", "魏来"])
      store.begin_submission_attempt("ep004-g002", "为你", "2026-08-28T01:00:00+00:00")
      row = store.tasks()[1]
      self.assertEqual(row["attempt_state"], "click_pending")
      with self.assertRaisesRegex(ValueError, "已有提交栅栏"):
        store.begin_submission_attempt("ep004-g002", "魏来")
      store.db.close()

  def test_daily_success_count_sums_current_accounts(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(str(Path(folder) / "workflow.db"))
      from datetime import datetime, timedelta, timezone
      today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
      store.sync_accounts([
        {"label": "沉雪", "switch_order": 1, "daily_limit": 3},
        {"label": "为你", "switch_order": 2, "daily_limit": 3},
      ], today)
      store.db.execute("UPDATE accounts SET success_count=2 WHERE label='沉雪'")
      store.db.execute("UPDATE accounts SET success_count=1 WHERE label='为你'")
      store.db.commit()
      self.assertEqual(store.daily_success_count(), 3)
      store.db.close()

  def test_daily_success_count_ignores_non_current_day(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(str(Path(folder) / "workflow.db"))
      from datetime import datetime, timedelta, timezone
      today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
      store.sync_accounts([
        {"label": "沉雪", "switch_order": 1, "daily_limit": 3},
        {"label": "为你", "switch_order": 2, "daily_limit": 3},
      ], today)
      store.db.execute("UPDATE accounts SET success_count=2 WHERE label='沉雪'")
      store.db.execute("UPDATE accounts SET day='2026-08-30', success_count=3 WHERE label='为你'")
      store.db.commit()
      self.assertEqual(store.daily_success_count(), 2)
      store.db.close()

  def test_store_reset_stale_account_day_on_startup(self):
    with tempfile.TemporaryDirectory() as folder:
      database = Path(folder) / "workflow.db"
      store = Store(database)
      store.sync_accounts([
        {"label": "沉雪", "switch_order": 1, "daily_limit": 3},
      ], "2026-08-30")
      store.set_account_state("沉雪", "quota_exhausted", "daily limit")
      store.db.execute("UPDATE accounts SET success_count=3, failure_count=1")
      store.db.commit()
      store.db.close()

      reopened = Store(database)
      row = reopened.db.execute(
        "SELECT day,success_count,failure_count,availability_state,availability_reason FROM accounts WHERE label='沉雪'"
      ).fetchone()
      from datetime import datetime, timedelta, timezone
      self.assertEqual(row[0], datetime.now(timezone(timedelta(hours=8))).date().isoformat())
      self.assertEqual((row[1], row[2]), (0, 0))
      self.assertEqual((row[3], row[4]), ("unknown", None))
      self.assertEqual(reopened.account_candidates()[0]["label"], "沉雪")
      reopened.db.close()

  def test_record_submission_cannot_exceed_daily_limit(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      store.sync_accounts([{"label": "沉雪", "switch_order": 1, "daily_limit": 1}], "2026-08-28")
      first = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "one")], 10, 10, "ready")
      second = ShotGroup(4, 2, [Shot(4, "x", "2", 10, "", "two")], 10, 10, "ready")
      store.upsert_group(first, "one", [])
      store.upsert_group(second, "two", [])
      store.record_submission("沉雪", first.task_id)
      with self.assertRaisesRegex(ValueError, "额度已达上限"):
        store.record_submission("沉雪", second.task_id)
      self.assertEqual(store.db.execute("select success_count from accounts where label='沉雪'").fetchone()[0], 1)
      store.db.close()

  def test_record_account_rejects_unknown_and_cools_failed_account(self):
    with tempfile.TemporaryDirectory() as folder:
      store = Store(f"{folder}/workflow.db")
      with self.assertRaisesRegex(ValueError, "账号未登记"):
        store.record_account("不存在", "ep004-g001", success=False)
      store.sync_accounts([{"label": "沉雪", "switch_order": 1}], "2026-08-28")
      store.record_account("沉雪", "ep004-g001", success=False)
      row = store.db.execute("select failure_count,cooldown_until from accounts where label=?", ("沉雪",)).fetchone()
      self.assertEqual(row[0], 1)
      self.assertIsNotNone(row[1])
      self.assertIsNone(store.choose_account())
      store.db.close()
