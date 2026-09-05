import json
import tempfile
import unittest
from pathlib import Path

from src.domain import Shot, ShotGroup
from src.runner import _quality_options, archive_pending
from src.store import Store


class ArchivePendingTests(unittest.TestCase):
  def test_archive_pending_does_not_submit(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      output = root / "output"
      output.mkdir()
      source = root / "random.mp4"
      source.write_bytes(b"video")
      store = Store(database)
      group = ShotGroup(4, 2, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "download_pending")
      store.upsert_group(group, "prompt", [])
      store.set_status(group.task_id, "download_pending", "waiting")
      store.db.close()
      config = root / "config.json"
      config.write_text(json.dumps({"database": str(database), "output_dir": str(output)}), encoding="utf-8")
      import src.runner as runner
      original = runner.archive_download
      runner.archive_download = lambda source_path, output_dir, episode, index: output / "第四集2.mp4"
      try:
        result = archive_pending(str(config), group.task_id, str(source))
      finally:
        runner.archive_download = original
      self.assertTrue(result.startswith("COMPLETED:"))
      reopened = Store(database)
      self.assertEqual(reopened.tasks()[0]["status"], "completed")
      self.assertEqual(reopened.db.execute("select count(*) from events where category='ui'").fetchone()[0], 0)
      reopened.db.close()
  def test_quality_options_scope_evidence_to_task(self):
    config = {
      "quality": {
        "enabled": True,
        "review_dir": "data/quality-review",
        "evidence_dir": "data/quality-evidence",
        "black_ratio_limit": 0.45,
        "freeze_ratio_limit": 0.85,
      }
    }
    scoped = _quality_options(config, "ep004-g001")
    self.assertEqual(Path(scoped["quality_evidence_dir"]), Path("data/quality-evidence/ep004-g001"))
    unscoped = _quality_options(config)
    self.assertEqual(Path(unscoped["quality_evidence_dir"]), Path("data/quality-evidence"))
