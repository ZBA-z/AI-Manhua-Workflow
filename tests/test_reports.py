import json
import tempfile
import unittest
from pathlib import Path

from src.domain import Episode, Shot, ShotGroup
from src.reports import write_report


class ReportTests(unittest.TestCase):
  def test_report_separates_fatal_warnings_manual_and_unresolved(self):
    with tempfile.TemporaryDirectory() as folder:
      target = Path(folder) / "report.json"
      shot = Shot(4, "x", "1", 5, "", "prompt")
      episode = Episode(4, "x", 1, [shot])
      group = ShotGroup(4, 1, [shot], 5, 5, "manual", "需要人工确认时长")
      write_report(
        target,
        [episode],
        [group],
        ["结构错误"],
        warnings=["提示词较短"],
        manual_actions=[{"task_id": group.task_id, "reason": group.adjustment}],
        asset_coverage={"unresolved": [{"episode": 4, "shot": "1"}]},
      )
      payload = json.loads(target.read_text(encoding="utf-8"))
      self.assertEqual(payload["fatal_errors"], ["结构错误"])
      self.assertEqual(payload["warnings"], ["提示词较短"])
      self.assertEqual(payload["manual_actions"][0]["task_id"], "ep004-g001")
      self.assertEqual(payload["unresolved_assets"], [{"episode": 4, "shot": "1"}])
      self.assertEqual(payload["manual_groups"], 1)


if __name__ == "__main__":
  unittest.main()
