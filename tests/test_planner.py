import unittest
from src.domain import Episode, Shot
from src.planner import plan_episode, split_by_reference_limit


def shot(number: int, seconds: int, dialogue: str = "") -> Shot:
    return Shot(1, "测试", number, seconds, "", "prompt", dialogue=dialogue)


class PlannerTests(unittest.TestCase):
  def test_three_five_four_hits_ten(self):
    groups = plan_episode(Episode(1, "测试", 3, [shot(1, 3), shot(2, 5), shot(3, 4)]))
    self.assertEqual(groups[0].original_seconds, 12)
    self.assertEqual(groups[0].planned_seconds, 10)
    self.assertEqual(groups[0].status, "ready")
    self.assertEqual(sum(groups[0].duration_map.values()), 10)
    self.assertEqual(groups[0].duration_map, {1: 3, 2: 3, 3: 4})


  def test_dialogue_group_can_be_extended_with_caution(self):
    groups = plan_episode(Episode(1, "测试", 2, [shot(1, 3, "台词"), shot(2, 5, "台词")]))
    self.assertEqual(groups[0].status, "ready")
    self.assertIn("需确认", groups[0].adjustment)

  def test_overfilled_dialogue_group_chooses_shortest_dialogue(self):
    groups = plan_episode(Episode(1, "测试", 3, [shot(1, 5, "很长的一段台词"), shot(2, 5, "短"), shot(3, 4, "中等")]))
    self.assertEqual(groups[0].status, "ready")
    self.assertIn("镜号2", groups[0].adjustment)


  def test_max_three_shots(self):
    groups = plan_episode(Episode(1, "测试", 4, [shot(1, 2), shot(2, 2), shot(3, 2), shot(4, 2)]))
    self.assertTrue(all(len(group.shots) <= 3 for group in groups))

  def test_custom_target_is_materialized(self):
    groups = plan_episode(Episode(1, "测试", 2, [shot(1, 3), shot(2, 3)]), target_seconds=8)
    self.assertEqual(groups[0].planned_seconds, 8)
    self.assertEqual(sum(groups[0].duration_map.values()), 8)

  def test_reference_limit_splits_and_renumbers(self):
    episode = Episode(1, "测试", 3, [shot(1, 4), shot(2, 4), shot(3, 6)])
    groups = plan_episode(episode)
    refs = {(1, "1"): {"a", "b"}, (1, "2"): {"c"}, (1, "3"): {"d", "e"}}
    split = split_by_reference_limit(groups, refs, max_references=3)
    self.assertEqual([g.index for g in split], [1, 2])
    self.assertEqual([len(g.shots) for g in split], [2, 1])
    self.assertTrue(all(g.planned_seconds == 10 for g in split))

  def test_reference_limit_does_not_stretch_single_dialogue(self):
    episode = Episode(1, "测试", 2, [shot(1, 4, "台词"), shot(2, 4)])
    groups = plan_episode(episode)
    split = split_by_reference_limit(groups, {(1, "1"): {"a", "b"}, (1, "2"): {"c", "d"}}, max_references=2)
    self.assertEqual(split[0].status, "manual")
    self.assertEqual(split[0].duration_map, {1: 4})
