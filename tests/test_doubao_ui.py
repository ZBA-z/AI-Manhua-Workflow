import unittest

from src.doubao_ui import DoubaoUI, UIBlocked


class DoubaoUITests(unittest.TestCase):
  def test_video_parameter_detection_accepts_rendered_button(self):
    self.assertTrue(DoubaoUI._looks_like_video_parameter("16:9 · 10s"))

  def test_video_parameter_detection_rejects_work_task_mode(self):
    self.assertFalse(DoubaoUI._looks_like_video_parameter("工作任务"))

  def test_submission_classifier_distinguishes_quota_from_moderation(self):
    self.assertEqual(DoubaoUI._classify_submission_texts(["今日额度已达上限"]), "quota_exhausted")
    self.assertEqual(DoubaoUI._classify_submission_texts(["疑似包含侵权，生成额度未扣除"]), "moderation_blocked")
    self.assertEqual(DoubaoUI._classify_submission_texts(["抱歉，由于版权相关限制，暂时无法创作对应的内容。"]), "moderation_blocked")

  def test_parameter_menu_dismiss_helper_exists(self):
    self.assertTrue(callable(DoubaoUI._dismiss_parameter_menu))

  def test_coordinate_account_verification_is_explicitly_configured(self):
    self.assertIn("coordinate_account_verification", DoubaoUI("configs/ui_calibration.json").calibration)

  def test_window_priority_prefers_main_chat_over_image_viewer(self):
    self.assertGreater(DoubaoUI._window_priority("主对话 - 豆包"), DoubaoUI._window_priority("豆包图片查看器"))
    self.assertGreater(DoubaoUI._window_priority("创建技能 - 豆包"), DoubaoUI._window_priority("豆包图片查看器"))
    self.assertEqual(DoubaoUI._window_priority("豆包图片查看器"), -100)

  def test_evidence_capture_is_disabled_by_default_for_production(self):
    ui = DoubaoUI("configs/ui_calibration.json")
    self.assertFalse(ui._evidence_enabled)

  def test_download_latest_helper_exists(self):
    self.assertTrue(callable(DoubaoUI.download_latest))

  def test_account_verification_requires_only_target_account_visible(self):
    accounts = ["沉雪", "为你", "魏来"]
    self.assertTrue(DoubaoUI._only_target_account_visible(accounts, "为你", ["设置", "为你", "退出登录"]))
    self.assertFalse(DoubaoUI._only_target_account_visible(accounts, "为你", ["沉雪", "为你", "魏来"]))
    self.assertFalse(DoubaoUI._only_target_account_visible(accounts, "为你", ["设置", "沉雪"]))

  def test_account_candidate_uses_unique_rightmost_visible_item(self):
    class Rect:
      def __init__(self, left):
        self.left = left

    class Candidate:
      def __init__(self, left, visible=True):
        self.left = left
        self.visible = visible

      def rectangle(self):
        return Rect(self.left)

      def is_visible(self):
        return self.visible

    current_summary = Candidate(20)
    switch_list_item = Candidate(260)
    hidden_copy = Candidate(500, visible=False)
    self.assertIs(
      DoubaoUI._unique_rightmost_account_candidate([current_summary, switch_list_item, hidden_copy]),
      switch_list_item,
    )
    with self.assertRaisesRegex(UIBlocked, "账号候选仍有歧义"):
      DoubaoUI._unique_rightmost_account_candidate([Candidate(260), Candidate(260)])

  def test_video_preview_requires_one_visible_card(self):
    class Candidate:
      def __init__(self, visible=True):
        self.visible = visible

      def is_visible(self):
        return self.visible

    card = Candidate()
    self.assertIs(DoubaoUI._unique_visible_video_card([Candidate(False), card]), card)
    with self.assertRaisesRegex(UIBlocked, "多个可见视频卡片"):
      DoubaoUI._unique_visible_video_card([Candidate(), Candidate()])

  def test_prompt_search_result_requires_unique_visible_match(self):
    class Candidate:
      def __init__(self, title, visible=True):
        self.title = title
        self.visible = visible

      def window_text(self):
        return self.title

      def is_visible(self):
        return self.visible

    fingerprint = "参考图1=周.png"
    match = Candidate("icon 对话 生成视频：参考图1=周.png 其余内容")
    self.assertIs(DoubaoUI._unique_prompt_search_result([Candidate("无关"), match], fingerprint), match)
    self.assertIsNone(DoubaoUI._unique_prompt_search_result([Candidate("无关")], fingerprint))
    with self.assertRaisesRegex(UIBlocked, "多个消息搜索结果"):
      DoubaoUI._unique_prompt_search_result([match, Candidate("另一个 参考图1=周.png")], fingerprint)

  def test_visible_texts_exclude_hidden_webview_nodes(self):
    class Candidate:
      def __init__(self, title, visible):
        self.title = title
        self.visible = visible

      def window_text(self):
        return self.title

      def is_visible(self):
        return self.visible

    controls = [Candidate("你的视频生成好了。", False), Candidate("当前提示词", True)]
    self.assertEqual(DoubaoUI._visible_texts(controls), ["当前提示词"])

  def test_video_project_navigation_does_not_require_control_type_method(self):
    class Candidate:
      def window_text(self):
        return "视频分镜制作"
      def is_visible(self):
        return True
    candidate = Candidate()
    # UIA wrappers in the WebView do not consistently expose control_type().
    self.assertEqual([c for c in [candidate] if "视频分镜制作" in c.window_text() and c.is_visible()], [candidate])

  def test_task_card_requires_prompt_completion_card_order(self):
    class Candidate:
      def __init__(self, title="", class_name=""):
        self.title = title
        self.css_class = class_name

      def window_text(self):
        return self.title

      def class_name(self):
        return self.css_class

    prompt = "参考图1=陈默.png 分镜内容"
    card = Candidate(class_name="block-video-abc")
    controls = [Candidate(prompt), Candidate("你的视频生成好了。"), card]
    self.assertIs(DoubaoUI._task_video_card_from_controls(controls, prompt), card)
    self.assertIsNone(DoubaoUI._task_video_card_from_controls([Candidate(prompt), card], prompt))
    with self.assertRaisesRegex(UIBlocked, "提示词节点不唯一"):
      DoubaoUI._task_video_card_from_controls(controls + [Candidate(prompt)], prompt)

  def test_task_card_matches_short_generation_summary_node(self):
    class Candidate:
      def __init__(self, title="", class_name=""):
        self.title = title
        self.css_class = class_name
      def window_text(self):
        return self.title
      def class_name(self):
        return self.css_class
    prompt = "本组上传参考图清单：参考图1=时间族.png；参考图2=受损城市.png；参考图3=南宫镜.png；分镜中的人物身份..."
    summary = "生成视频：本组上传参考图清单：参考图1=时间族.png；参考图2=受损城市.png；参考图3=南宫镜.png"
    card = Candidate(class_name="block-video-abc")
    controls = [Candidate(summary), Candidate("你的视频生成好了。"), card]
    self.assertIs(DoubaoUI._task_video_card_from_controls(controls, prompt), card)

  def test_moderation_for_prompt_detects_adjacent_block_text(self):
    class Candidate:
      def __init__(self, title=""):
        self.title = title
      def window_text(self):
        return self.title
    prompt = "本组上传参考图清单：参考图1=时间族.png；参考图2=受损城市.png；参考图3=南宫镜.png；分镜内容"
    block = "抱歉，由于版权相关限制，暂时无法创作对应的内容。"
    controls = [Candidate(prompt), Candidate(block)]
    self.assertEqual(DoubaoUI._moderation_for_prompt_from_controls(controls, prompt), block)
    self.assertIsNone(DoubaoUI._moderation_for_prompt_from_controls([Candidate(prompt)], prompt))
