import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.assets import Asset, infer_category, match_by_prompt, normalize_text


class AssetTests(unittest.TestCase):
  def test_prompt_selects_character_scene_and_prop(self):
    assets = [
      Asset("/x/南宫镜.png", "南宫镜.png", normalize_text("南宫镜.png"), "a", "character", ["南宫镜"]),
      Asset("/x/废弃医院.png", "废弃医院.png", normalize_text("废弃医院.png"), "b", "scene", ["废弃医院"]),
      Asset("/x/金色匣子.png", "金色匣子.png", normalize_text("金色匣子.png"), "c", "prop", ["金色匣子"]),
    ]
    found = match_by_prompt("南宫镜进入废弃医院，手持金色匣子", assets)
    self.assertEqual({item.category for item in found}, {"character", "scene", "prop"})

  def test_variant_tie_is_not_silently_selected(self):
    assets = [
      Asset("/x/南宫镜不可控.png", "南宫镜不可控.png", "", "a", "character", ["南宫镜"]),
      Asset("/x/南宫镜；紫微.png", "南宫镜；紫微.png", "", "b", "character", ["南宫镜"]),
    ]
    self.assertEqual(match_by_prompt("南宫镜站在房间里", assets), [])

  def test_named_artifact_without_generic_weapon_suffix_is_prop(self):
    self.assertEqual(infer_category("谎言之角.png", "character"), "prop")
