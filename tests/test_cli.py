import json
import io
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from src.cli import main, preflight_inputs, prepare
from src.domain import Shot, ShotGroup
from src.store import Store
from src.startup import StartupResult


class PreflightTests(unittest.TestCase):
  def test_missing_source_directories_fail_together(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      config = {
        "script_dir": str(root / "scripts"),
        "character_dir": str(root / "characters"),
        "scene_dir": str(root / "scenes"),
      }
      with self.assertRaisesRegex(RuntimeError, "script_dir") as caught:
        preflight_inputs(config)
      self.assertIn("character_dir", str(caught.exception))
      self.assertIn("scene_dir", str(caught.exception))

  def test_threshold_failure_happens_before_stateful_prepare(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      scripts = root / "scripts"
      characters = root / "characters"
      scenes = root / "scenes"
      for path in (scripts, characters, scenes):
        path.mkdir()
      (scripts / "story.txt").write_text(
        "第4集：测试（1镜）\n镜号：1；时长：10s；旁白音效：（无台词）。\n电影级文生视频：黑场。\n",
        encoding="utf-8",
      )
      config = {
        "script_dir": str(scripts),
        "character_dir": str(characters),
        "scene_dir": str(scenes),
        "validation": {"min_episodes": 2, "min_shots": 2},
      }
      with self.assertRaisesRegex(RuntimeError, "集数不足") as caught:
        preflight_inputs(config)
      self.assertIn("镜头数不足", str(caught.exception))

  def test_failed_preflight_preserves_database_and_report(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      database = root / "workflow.db"
      report = root / "report.json"
      store = Store(database)
      group = ShotGroup(4, 1, [Shot(4, "x", "1", 10, "", "prompt")], 10, 10, "ready")
      store.upsert_group(group, "prompt", [])
      store.db.close()
      report.write_text("OLD_REPORT", encoding="utf-8")
      config = {
        "script_dir": str(root / "missing-scripts"),
        "character_dir": str(root / "missing-characters"),
        "scene_dir": str(root / "missing-scenes"),
        "output_dir": str(root / "output"),
        "database": str(database),
        "report": str(report),
      }
      config_path = root / "config.json"
      config_path.write_text(json.dumps(config), encoding="utf-8")
      with self.assertRaisesRegex(RuntimeError, "输入预检失败"):
        prepare(str(config_path))
      reopened = Store(database)
      self.assertEqual([(row["task_id"], row["status"]) for row in reopened.tasks()], [(group.task_id, "ready")])
      reopened.db.close()
      self.assertEqual(report.read_text(encoding="utf-8"), "OLD_REPORT")

  def test_download_directory_can_be_locked_to_output_directory(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      scripts = root / "scripts"
      characters = root / "characters"
      scenes = root / "scenes"
      output = root / "output"
      for path in (scripts, characters, scenes, output):
        path.mkdir()
      (scripts / "story.txt").write_text(
        "第1集：测试（1镜）\n镜号：1；时长：10s；旁白音效：（无台词）。\n电影级文生视频：黑场。\n",
        encoding="utf-8",
      )
      config = {
        "script_dir": str(scripts), "character_dir": str(characters), "scene_dir": str(scenes),
        "output_dir": str(output), "download_dir": str(root / "downloads"),
        "download_dir_must_equal_output": True,
      }
      with self.assertRaisesRegex(RuntimeError, "下载目录必须"):
        preflight_inputs(config)

  def test_download_pending_result_is_printed_for_startup_gate(self):
    output = io.StringIO()
    with patch.object(sys, "argv", ["workflow", "download-pending", "--config", "x.json"]), \
         patch("src.cli.download_pending", return_value="DOWNLOAD_PENDING: blocked"), \
         patch("sys.stdout", output):
      main()
    self.assertIn("DOWNLOAD_PENDING: blocked", output.getvalue())

  def test_startup_command_prints_machine_readable_terminal_result(self):
    output = io.StringIO()
    result = StartupResult("run-123", "no_work", "NO_GUI_WORK: staged_validation_required", 0)
    with patch.object(sys, "argv", ["workflow", "startup", "--config", "x.json"]), \
         patch("src.cli.run_startup", return_value=result), \
         patch("sys.stdout", output):
      with self.assertRaises(SystemExit) as caught:
        main()
    self.assertEqual(caught.exception.code, 0)
    payload = json.loads(output.getvalue())
    self.assertEqual(payload["run_id"], "run-123")
    self.assertEqual(payload["state"], "no_work")
    self.assertEqual(payload["semantic"], "NO_GUI_WORK: staged_validation_required")


if __name__ == "__main__":
  unittest.main()
