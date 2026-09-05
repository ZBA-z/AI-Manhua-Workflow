import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.media import assess_video, chinese_episode, output_name


class MediaTests(unittest.TestCase):
  def test_output_name(self):
    self.assertEqual(output_name(4, 1), "第四集1.mp4")
    self.assertEqual(output_name(21, 3), "第二十一集3.mp4")

  def test_quality_report_rejects_non_16x9(self):
    with tempfile.TemporaryDirectory() as folder:
      path = Path(folder) / "portrait.mp4"
      self._make_video(path, "color=c=black:s=640x480:d=1")
      report = assess_video(path, expected_duration=1, evidence_dir=Path(folder) / "evidence")
      self.assertEqual(report.status, "reject")
      self.assertIn("16:9", report.reason)

  def test_quality_report_routes_black_video_to_review_with_evidence(self):
    with tempfile.TemporaryDirectory() as folder:
      path = Path(folder) / "black.mp4"
      evidence = Path(folder) / "evidence"
      self._make_video(path, "color=c=black:s=1280x720:d=1")
      report = assess_video(path, expected_duration=1, evidence_dir=evidence)
      self.assertEqual(report.status, "review")
      self.assertTrue((evidence / "start.jpg").exists())
      self.assertTrue((evidence / "middle.jpg").exists())
      self.assertTrue((evidence / "end.jpg").exists())

  def test_quality_report_does_not_treat_dark_gray_as_black(self):
    with tempfile.TemporaryDirectory() as folder:
      path = Path(folder) / "dark-gray.mp4"
      self._make_video(path, "testsrc2=s=1280x720:d=1")
      report = assess_video(path, expected_duration=1, evidence_dir=Path(folder) / "evidence")
      self.assertEqual(report.status, "pass")

  def test_quality_report_routes_video_frozen_until_end_to_review(self):
    with tempfile.TemporaryDirectory() as folder:
      path = Path(folder) / "frozen.mp4"
      self._make_video(path, "color=c=red:s=1280x720:d=1")
      report = assess_video(path, expected_duration=1, evidence_dir=Path(folder) / "evidence")
      self.assertEqual(report.status, "review")
      self.assertGreaterEqual(report.freeze_ratio, 0.85)

  def test_freeze_ratio_uses_ordered_events(self):
    from unittest.mock import patch
    from src.media import _filter_ratio
    output = "\n".join(["freeze_start: 1.0", "freeze_end: 2.0", "freeze_start: 4.0"])
    with patch("src.media.subprocess.run") as run:
      run.return_value.returncode = 0
      run.return_value.stderr = output
      ratio = _filter_ratio("ffmpeg", "frozen.mp4", "freezedetect", 10.0)
    self.assertAlmostEqual(ratio, 0.7)

  @staticmethod
  def _make_video(path: Path, source: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
      from src.media import find_ffmpeg
      ffmpeg = find_ffmpeg()
    if not ffmpeg:
      raise unittest.SkipTest("ffmpeg unavailable")
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", source, "-an", "-c:v", "libx264", str(path)], check=True, capture_output=True)
