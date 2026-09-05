import unittest

from src.domain import Shot
from src.prompt_check import check_shot, safe_rewrite


class PromptCheckTests(unittest.TestCase):
  def test_short_prompt_warning(self):
    issues = check_shot(Shot(1, "x", "1", 3, "", "人走"))
    self.assertTrue(any(item.code == "short_prompt" for item in issues))

  def test_rights_reference_manual(self):
    issues = check_shot(Shot(1, "x", "1", 3, "", "像某某明星的角色在场景中"))
    self.assertTrue(any(item.code == "rights_reference" for item in issues))

  def test_safe_rewrite_is_bounded(self):
    prompt, changes = safe_rewrite("超写实3D，UE5 Lumen，刀刃划过，暗红色液体飞溅")
    self.assertNotIn("UE5 Lumen", prompt)
    self.assertNotIn("暗红色液体飞溅", prompt)
    self.assertGreaterEqual(len(changes), 3)

  def test_yuan_age_is_rewritten_without_changing_identity(self):
    prompt, _ = safe_rewrite("约8岁的小女孩（渊）站在广场")
    self.assertNotIn("8岁", prompt)
    self.assertIn("年轻女孩（渊）", prompt)

  def test_parasite_scene_keeps_story_action_without_graphic_targeting(self):
    source = "精准点在对方后颈脑干位置，用高频刀刀背精准击打被寄生平民的后颈，用麻醉枪射击另一个的后颈，眼睛纯黑嘴部裂开"
    prompt, changes = safe_rewrite(source)
    for risky in ("脑干", "击打", "射击另一个的后颈", "嘴部裂开"):
      self.assertNotIn(risky, prompt)
    self.assertIn("解除", prompt)
    self.assertIn("非致伤", prompt)
    self.assertIn("非人化异变", prompt)
    self.assertEqual(len(changes), 4)
