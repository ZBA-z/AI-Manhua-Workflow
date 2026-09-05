import tempfile
import unittest
from pathlib import Path

from src.domain import Shot
from src.subtitles import write_srt


class SubtitleTests(unittest.TestCase):
  def test_non_overlapping_srt(self):
    shots = [Shot(1, "x", "1", 3, "", "", dialogue="甲：走"), Shot(1, "x", "2", 2, "", "", dialogue="（无台词）")]
    with tempfile.TemporaryDirectory() as folder:
      path = Path(folder) / "x.srt"
      self.assertEqual(write_srt(path, shots), 1)
      self.assertIn("00:00:00,000 --> 00:00:03,000", path.read_text(encoding="utf-8-sig"))
