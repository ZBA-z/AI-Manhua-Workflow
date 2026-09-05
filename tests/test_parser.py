import unittest
from pathlib import Path

from src.parser import parse_script, validate_episodes


class ParserTests(unittest.TestCase):
  def test_parse_episode_and_references(self):
    source = Path(self.id().replace("test_parse_episode_and_references", "")) / "x.txt"
    source = Path("test-parser-fixture.txt")
    source.write_text("第4集：测试（2镜）\n镜号：1；时长：3s；旁白音效：甲：走。\n电影级文生视频：人物（参考图1）进入场景。\n镜号：2；时长：7s；旁白音效：（无台词）。\n电影级文生视频：空镜。\n", encoding="utf-8")
    try:
      episodes = parse_script(source)
      self.assertEqual(episodes[0].number, 4)
      self.assertEqual(episodes[0].shots[0].number, "1")
      self.assertEqual(episodes[0].shots[0].references, [1])
      self.assertTrue(episodes[0].shots[0].prompt.startswith("人物"))
      self.assertEqual(validate_episodes(episodes), [])
    finally:
      source.unlink(missing_ok=True)


  def test_composite_shot_numbers_are_supported(self):
    source = Path("test-parser-composite.txt")
    source.write_text("第11集：测试（2镜）\n镜号：11-1；时长：5s；旁白音效：（无台词）。\n电影级文生视频：空镜。\n镜号：11-2；时长：5s；旁白音效：（无台词）。\n电影级文生视频：空镜。\n", encoding="utf-8")
    try:
      episode = parse_script(source)[0]
      self.assertEqual([shot.number for shot in episode.shots], ["11-1", "11-2"])
      self.assertEqual(validate_episodes([episode]), [])
    finally:
      source.unlink(missing_ok=True)


  def test_missing_prompt_is_reported(self):
    source = Path("test-parser-missing.txt")
    source.write_text("第1集：测试（1镜）\n镜号：1；时长：3s；旁白音效：（无台词）。\n", encoding="utf-8")
    try:
      self.assertIn("缺少视频提示词", validate_episodes(parse_script(source))[0])
    finally:
      source.unlink(missing_ok=True)

  def test_episode_heading_is_not_a_reference(self):
    source = Path("test-parser-heading.txt")
    source.write_text("第4集：测试（1镜）\n镜号：4-1；时长：10s；旁白音效：（无台词）。\n电影级文生视频：人物进入场景。\n", encoding="utf-8")
    try:
      self.assertEqual(parse_script(source)[0].shots[0].references, [])
    finally:
      source.unlink(missing_ok=True)

  def test_episode_21_known_gap_is_renumbered(self):
    source = Path("test-parser-episode21.txt")
    source.write_text("第21集：测试（4镜）\n镜号：1；时长：3s；旁白音效：（无台词）。\n电影级文生视频：空镜。\n镜号：2；时长：3s；旁白音效：（无台词）。\n电影级文生视频：空镜。\n镜号：3；时长：3s；旁白音效：（无台词）。\n电影级文生视频：空镜。\n镜号：4；时长：4s；旁白音效：（无台词）。\n电影级文生视频：空镜。\n镜号：6；时长：4s；旁白音效：（无台词）。\n电影级文生视频：空镜。\n", encoding="utf-8")
    try:
      episode = parse_script(source)[0]
      self.assertEqual([shot.number for shot in episode.shots], ["1", "2", "3", "4", "5"])
      self.assertEqual(validate_episodes([episode]), [])
    finally:
      source.unlink(missing_ok=True)
