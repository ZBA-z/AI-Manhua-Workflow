import unittest
import tempfile
import time
from unittest.mock import patch
from pathlib import Path

from src.downloads import archive_download, snapshot_mp4, wait_for_new_mp4, wait_for_new_mp4_in_dirs
from src.media import QualityReviewRequired, VideoQualityReport


class DownloadTests(unittest.TestCase):
  def test_naming_contract(self):
    with tempfile.TemporaryDirectory() as folder:
      source = Path(folder) / "source.mp4"
      source.write_bytes(b"video")
      output = Path(folder) / "out"
      with patch("src.downloads.require_16x9"):
        destination = archive_download(source, output, 30, 7)
        self.assertEqual(destination.name, "第三十集7.mp4")
        with self.assertRaises(FileExistsError):
          archive_download(source, output, 30, 7)

  def test_archive_renames_in_same_output_directory(self):
    with tempfile.TemporaryDirectory() as folder:
      source = Path(folder) / "doubao-random.mp4"
      source.write_bytes(b"video")
      with patch("src.downloads.require_16x9"):
        destination = archive_download(source, folder, 4, 1)
      self.assertEqual(destination.name, "第四集1.mp4")
      self.assertTrue(destination.exists())
      self.assertFalse(source.exists())

  def test_wait_for_new_mp4_ignores_existing_named_video(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      old = root / "第四集1.mp4"
      old.write_bytes(b"old")
      before = {str(old)}
      with self.assertRaises(TimeoutError):
        wait_for_new_mp4(root, before, timeout=0)

  def test_wait_for_new_mp4_rejects_file_older_than_submission(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      candidate = root / "old.mp4"
      candidate.write_bytes(b"x")
      old_time = time.time() - 10
      import os
      os.utime(candidate, (old_time, old_time))
      with self.assertRaises(TimeoutError):
        wait_for_new_mp4(root, set(), timeout=0, submitted_after=time.time())

  def test_snapshot_and_multi_directory_wait(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      downloads = root / "downloads"
      output = root / "output"
      downloads.mkdir()
      output.mkdir()
      existing = output / "old.mp4"
      existing.write_bytes(b"old")
      before = snapshot_mp4([downloads, output])
      fresh = downloads / "new.mp4"
      fresh.write_bytes(b"new")
      with patch("src.downloads.time.sleep", return_value=None):
        found = wait_for_new_mp4_in_dirs([downloads, output], before, timeout=1)
      self.assertEqual(found, fresh)

  def test_multi_directory_wait_rejects_ambiguous_candidates(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      left, right = root / "left", root / "right"
      left.mkdir(); right.mkdir()
      before = snapshot_mp4([left, right])
      (left / "a.mp4").write_bytes(b"a")
      (right / "b.mp4").write_bytes(b"b")
      with self.assertRaisesRegex(RuntimeError, "多个新MP4"):
        wait_for_new_mp4_in_dirs([left, right], before, timeout=1)

  def test_quality_review_isolated_without_overwriting_output(self):
    with tempfile.TemporaryDirectory() as folder:
      root = Path(folder)
      source = root / "source.mp4"
      source.write_bytes(b"video")
      review = root / "quality-review"
      report = VideoQualityReport("review", "black", None)
      with patch("src.downloads.require_16x9"), patch("src.downloads.assess_video", return_value=report):
        with self.assertRaisesRegex(QualityReviewRequired, "black"):
          archive_download(source, root / "out", 4, 1, quality_review_dir=review)
      self.assertFalse(source.exists())
      self.assertTrue((review / "source.mp4").exists())
      self.assertFalse((root / "out" / "第四集1.mp4").exists())
