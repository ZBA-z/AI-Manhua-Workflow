from __future__ import annotations

import json
import hashlib
import os
import subprocess
import time
import ctypes
import re
from dataclasses import dataclass
from pathlib import Path

_PROMPT_FINGERPRINT_LEN = 48


class UIBlocked(RuntimeError):
    """The desktop session is not safe or ready for UI automation."""


class SubmissionUnconfirmed(UIBlocked):
    """The submit action may have happened, but the UI gave no proof."""


class AccountUIError(UIBlocked):
    """A pre-submit account availability problem that may allow rotation."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


class QuotaExhausted(AccountUIError):
    def __init__(self, message: str):
        super().__init__("quota_exhausted", message)


class ModerationBlocked(UIBlocked):
    """Task moderation is not an account failure."""


@dataclass(slots=True)
class DoubaoTaskSpec:
    prompt: str
    references: list[str]
    model: str = "Seedance 2.0 Fast"
    duration: str = "10s"
    aspect_ratio: str = "16:9"


class DoubaoUI:
    """Safety-gated adapter. Dry-run is default until UI calibration is verified."""

    def __init__(self, calibration: str | Path, dry_run: bool = True):
        self.calibration = json.loads(Path(calibration).read_text(encoding="utf-8"))
        self.dry_run = dry_run
        self.last_submit_at: float | None = None
        self._evidence_enabled = self.calibration.get("evidence_enabled", False) is True
        self._last_capture_at = 0.0

    def session_ready(self) -> bool:
        # Lock-screen detection is intentionally conservative. A live backend
        # must refuse to click if the interactive desktop cannot be confirmed.
        return bool(self._process_window_handles(lambda **kwargs: [kwargs["handle"]])) if os.name == "nt" else False

    def page_state(self, window) -> str:
        """Return one of video, video_entry, other_page, unreadable."""
        try:
            controls = self._uia_window(window).descendants(control_type="Button")
            titles = [control.window_text() for control in controls if control.is_visible()]
        except Exception:
            return "unreadable"
        if any(self._looks_like_video_parameter(title) for title in titles):
            return "video"
        if any("视频生成" in title for title in titles):
            return "video_entry"
        return "other_page"

    def submit(self, task: DoubaoTaskSpec) -> str:
        if task.aspect_ratio != "16:9":
            raise ValueError("拒绝提交：任务必须显式使用16:9")
        if not self.session_ready():
            raise UIBlocked("未检测到可用豆包进程或交互会话")
        if self.dry_run:
            return "DRY_RUN: 未点击豆包；请用校准后的 live backend 执行"
        try:
            from pywinauto import Application
            from pywinauto.findwindows import find_windows
        except ImportError as exc:
            raise UIBlocked("未安装 pywinauto，无法安全定位豆包控件") from exc
        window = self._find_window(Application, find_windows)
        state = self.page_state(window)
        if state == "unreadable":
            raise UIBlocked("豆包页面控件树不可读，拒绝继续")
        if state not in {"video", "video_entry"}:
            self._enter_video_mode(window)
            window = self._find_window(Application, find_windows)
            state = self.page_state(window)
            if state == "unreadable":
                raise UIBlocked("豆包页面控件树不可读，拒绝继续")
            if state not in {"video", "video_entry"}:
                raise UIBlocked("豆包页面未进入视频生成入口，拒绝继续")
        self._enter_video_mode(window)
        self._select_model(window, task.model)
        self._coordinate_select(window, "parameter_menu")
        self._coordinate_select(window, "aspect_16_9_in_menu")
        self._dismiss_parameter_menu()
        self._verify_parameters(window, task)
        self._capture(window, "before-submit")
        before_submit_digest = self._screen_digest() if self._evidence_enabled else ""
        self.last_submit_at = time.time()
        self._coordinate_select(window, "submit_button")
        self._capture(window, "after-submit-attempt")
        time.sleep(1.5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(1)
            evidence = self._uia_window(window)
            texts = [control.window_text() for control in evidence.descendants()]
            outcome = self._classify_submission_texts(texts)
            if outcome == "moderation_blocked":
                moderation = next(text for text in texts if self._is_moderation_text(text))
                raise ModerationBlocked(f"豆包审核拦截，额度未扣除：{moderation}")
            if outcome == "quota_exhausted":
                quota = next(text for text in texts if any(phrase in text for phrase in ("额度已用完", "额度不足", "今日额度已达上限", "生成次数已用尽")))
                raise QuotaExhausted(f"豆包账号额度耗尽：{quota}")
            if any("视频生成已提交" in text or "视频生成中" in text for text in texts):
                self._capture(window, "after-submit-confirmed")
                # The block/limit text can arrive a few seconds after the
                # transient "generating" confirmation. Hold the confirmation
                # briefly so a late moderation decision is not recorded as a
                # paid success.
                recheck_deadline = time.monotonic() + 8
                while time.monotonic() < recheck_deadline:
                    time.sleep(1)
                    evidence = self._uia_window(window)
                    texts = [control.window_text() for control in evidence.descendants()]
                    outcome = self._classify_submission_texts(texts)
                    if outcome == "moderation_blocked":
                        moderation = next(text for text in texts if self._is_moderation_text(text))
                        raise ModerationBlocked(f"豆包审核拦截，额度未扣除：{moderation}")
                    if outcome == "quota_exhausted":
                        quota = next(text for text in texts if any(phrase in text for phrase in ("额度已用完", "额度不足", "今日额度已达上限", "生成次数已用尽")))
                        raise QuotaExhausted(f"豆包账号额度耗尽：{quota}")
                return "SUBMITTED_CONFIRMED"
        raise SubmissionUnconfirmed("点击后未发现豆包提交确认，无法确认是否已提交")

    @staticmethod
    def _classify_submission_texts(texts: list[str]) -> str | None:
        if any(DoubaoUI._is_moderation_text(text) for text in texts):
            return "moderation_blocked"
        if any(any(phrase in text for phrase in ("额度已用完", "额度不足", "今日额度已达上限", "生成次数已用尽")) for text in texts):
            return "quota_exhausted"
        return None

    @staticmethod
    def _is_moderation_text(text: str) -> bool:
        return any(phrase in text for phrase in ("疑似包含侵权", "生成额度未扣除", "版权相关限制", "暂时无法创作对应的内容"))

    def _screen_digest(self) -> str:
        """Return a lightweight screenshot digest for submit-state fallback."""
        # Screenshots are diagnostic evidence only. Keep the production path
        # completely screenshot-free unless explicitly enabled in calibration.
        if not self._evidence_enabled:
            return ""
        try:
            import pyautogui
            image = pyautogui.screenshot()
            return hashlib.sha256(image.tobytes()).hexdigest()
        except Exception:
            return ""

    def download_latest(self, timeout: int = 900, expected_prompt: str | None = None) -> str:
        """Click a completed card's save button only with task-specific evidence."""
        try:
            from pywinauto import Application
            from pywinauto.findwindows import find_windows
        except ImportError as exc:
            raise UIBlocked("未安装 pywinauto，无法定位下载按钮") from exc
        window = self._find_window(Application, find_windows)
        deadline = time.monotonic() + timeout
        preview_opened = False
        while time.monotonic() < deadline:
            uia = self._uia_window(window)
            # The current Doubao build labels the video-card action simply
            # "保存". Require the completion marker and (when supplied) a
            # prompt fingerprint before considering that button actionable.
            controls = uia.descendants()
            task_card = None
            if expected_prompt:
                task_card = self._task_video_card_from_controls(controls, expected_prompt)
                if task_card is None:
                    time.sleep(1)
                    continue
            else:
                texts = self._visible_texts(controls)
                if not any("你的视频生成好了" in text for text in texts):
                    time.sleep(1)
                    continue
            candidates = [
                candidate for candidate in uia.descendants(title="保存", control_type="Button")
                if candidate.is_visible()
            ]
            if len(candidates) > 1:
                raise UIBlocked("当前页面存在多个可见保存按钮，拒绝自动下载")
            if not candidates and not preview_opened:
                if task_card is not None:
                    card = task_card
                else:
                    cards = [
                        control for control in uia.descendants(control_type="Group")
                        if "block-video-" in str(control.class_name())
                    ]
                    card = self._unique_visible_video_card(cards)
                if card is not None:
                    try:
                        card.scroll_into_view()
                        time.sleep(0.3)
                    except Exception:
                        pass
                    card.click_input()
                    preview_opened = True
                    time.sleep(1)
                    continue
            if not candidates:
                time.sleep(1)
                continue
            candidate = candidates[0]
            try:
                if candidate.is_visible():
                    candidate.click_input()
                    self._capture(window, "download-clicked")
                    return "DOWNLOAD_CLICKED"
            except Exception:
                pass
            time.sleep(1)
        raise UIBlocked(f"{timeout}s内未找到生成卡片下载按钮")

    @staticmethod
    def _unique_visible_video_card(candidates: list):
        visible = [candidate for candidate in candidates if candidate.is_visible()]
        if len(visible) > 1:
            raise UIBlocked("当前页面存在多个可见视频卡片，无法唯一匹配下载目标")
        return visible[0] if visible else None

    def completed_prompt_visible(self, expected_prompt: str) -> bool:
        """Check prompt -> completion -> video-card adjacency in the UI tree."""
        try:
            from pywinauto import Application
            from pywinauto.findwindows import find_windows
            window = self._find_window(Application, find_windows)
            controls = self._uia_window(window).descendants()
        except Exception:
            return False
        try:
            return self._task_video_card_from_controls(controls, expected_prompt) is not None
        except UIBlocked:
            raise

    def open_prompt_search(self, expected_prompt: str, timeout: int = 8) -> bool:
        """Open one exact message search result and expose its completed card."""
        try:
            import pyautogui
            import pyperclip
            from pywinauto import Application
            from pywinauto.findwindows import find_windows
        except ImportError as exc:
            raise UIBlocked("缺少消息搜索所需的桌面自动化依赖") from exc
        window = self._find_window(Application, find_windows)
        pyautogui.press("esc", presses=2, interval=0.1)
        search_buttons = [
            button for button in self._uia_window(window).descendants(title="搜索", control_type="Button")
            if button.is_visible()
        ]
        if len(search_buttons) != 1:
            raise UIBlocked("未找到唯一豆包消息搜索入口")
        search_buttons[0].click_input()
        time.sleep(0.7)
        edits = [edit for edit in self._uia_window(window).descendants(control_type="Edit") if edit.is_visible()]
        if len(edits) != 1:
            raise UIBlocked("消息搜索框不唯一，拒绝输入")
        query = " ".join(expected_prompt.split())[:45]
        edits[0].click_input()
        pyperclip.copy(query)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.hotkey("ctrl", "v")
        deadline = time.monotonic() + timeout
        result = None
        while time.monotonic() < deadline:
            buttons = self._uia_window(window).descendants(control_type="Button")
            result = self._unique_prompt_search_result(buttons, query)
            if result is not None:
                break
            time.sleep(0.5)
        if result is None:
            pyautogui.press("esc")
            return False
        result.click_input()
        time.sleep(1.2)
        return self.completed_prompt_visible(expected_prompt)

    @staticmethod
    def _compact_text(value: str) -> str:
        return "".join(str(value).split())

    @staticmethod
    def _visible_texts(controls: list) -> list[str]:
        texts: list[str] = []
        for control in controls:
            try:
                if control.is_visible():
                    texts.append(str(control.window_text()))
            except Exception:
                continue
        return texts

    @classmethod
    def _task_video_card_from_controls(cls, controls: list, expected_prompt: str):
        fingerprint = cls._compact_text(expected_prompt)[:_PROMPT_FINGERPRINT_LEN]
        if not fingerprint:
            return None
        prompt_indexes = [
            index for index, control in enumerate(controls)
            if fingerprint in cls._compact_text(control.window_text())
        ]
        if len(prompt_indexes) > 1:
            raise UIBlocked("当前聊天中提示词节点不唯一，拒绝绑定视频卡片")
        if not prompt_indexes:
            return None
        completion_seen = False
        for control in controls[prompt_indexes[0] + 1:prompt_indexes[0] + 81]:
            title = str(control.window_text())
            css_class = str(control.class_name())
            if "你的视频生成好了" in title:
                completion_seen = True
            if "block-video-" in css_class:
                return control if completion_seen else None
        return None

    def moderation_for_prompt(self, expected_prompt: str) -> str | None:
        """Return a moderation message adjacent to a prompt node, if present."""
        try:
            from pywinauto import Application
            from pywinauto.findwindows import find_windows
            window = self._find_window(Application, find_windows)
            controls = self._uia_window(window).descendants()
        except Exception:
            return None
        return self._moderation_for_prompt_from_controls(controls, expected_prompt)

    @classmethod
    def _moderation_for_prompt_from_controls(cls, controls: list, expected_prompt: str) -> str | None:
        fingerprint = cls._compact_text(expected_prompt)[:_PROMPT_FINGERPRINT_LEN]
        if not fingerprint:
            return None
        prompt_indexes = [
            index for index, control in enumerate(controls)
            if fingerprint in cls._compact_text(control.window_text())
        ]
        if len(prompt_indexes) > 1:
            raise UIBlocked("当前聊天中提示词节点不唯一，拒绝判定审核拦截")
        if not prompt_indexes:
            return None
        for control in controls[prompt_indexes[0] + 1:prompt_indexes[0] + 81]:
            title = str(control.window_text())
            if cls._is_moderation_text(title):
                return title
        return None

    @classmethod
    def _unique_prompt_search_result(cls, candidates: list, fingerprint: str):
        compact = cls._compact_text(fingerprint)
        matches = [
            candidate for candidate in candidates
            if candidate.is_visible() and compact in cls._compact_text(candidate.window_text())
        ]
        if len(matches) > 1:
            raise UIBlocked("豆包返回多个消息搜索结果，拒绝猜测任务")
        return matches[0] if matches else None

    def _verify_parameters(self, window, task: DoubaoTaskSpec) -> None:
        """Verify the rendered parameter button after menu selection."""
        expected = f"{task.aspect_ratio} · {task.duration}"
        try:
            controls = self._uia_window(window).descendants(control_type="Button")
            titles = [control.window_text().replace("·", "·").strip() for control in controls]
        except Exception as exc:
            raise UIBlocked("无法读取豆包视频参数控件，拒绝提交") from exc
        if not any(expected in title for title in titles):
            raise UIBlocked(f"视频参数未验证为 {expected}，拒绝提交")

    @staticmethod
    def _dismiss_parameter_menu() -> None:
        """Close the WebView popover before clicking the submit control."""
        try:
            import pyautogui
        except ImportError as exc:
            raise UIBlocked("未安装 pyautogui，无法关闭参数菜单") from exc
        pyautogui.press("esc")
        time.sleep(0.4)

    def switch_account(self, label: str) -> None:
        labels = self.calibration.get("accounts", [])
        if labels and label not in labels:
            raise AccountUIError("not_logged_in", f"账号不在已校准列表：{label}")
        try:
            from pywinauto import Application
            from pywinauto.findwindows import find_windows
        except ImportError as exc:
            raise UIBlocked("未安装 pywinauto") from exc
        window = self._find_window(Application, find_windows)
        # Previous recovery attempts may leave a popover open. Normalize the
        # page before opening the account menu so the avatar click is not a toggle.
        try:
            import pyautogui
            pyautogui.press("esc", presses=2, interval=0.1)
            time.sleep(0.25)
        except ImportError:
            pass
        self._coordinate_select(window, "account_avatar")
        try:
            index = labels.index(label)
        except ValueError:
            raise AccountUIError("not_logged_in", "配置缺少账号标签顺序")
        time.sleep(0.6)
        uia_window = self._uia_window(window)
        switch = uia_window.child_window(title="切换账号", control_type="MenuItem")
        if switch.exists(timeout=1):
            switch.click_input()
        else:
            self._coordinate_select(window, "switch_account", self._responsive_point(window, "switch_account"))
        time.sleep(0.6)
        uia_window = self._uia_window(window)
        candidates = [
            control for control in uia_window.descendants(control_type="Text")
            if str(control.window_text()).strip() == label
        ]
        if candidates:
            self._unique_rightmost_account_candidate(candidates).click_input()
        else:
            points = self.calibration.get("coordinate_fallback", {}).get("account_items", [])
            if index >= len(points):
                raise AccountUIError("switch_failed", f"缺少账号坐标校准：{label}")
            self._coordinate_select(window, "account_items", self._responsive_point(window, "account_items", points[index]))
        time.sleep(1.5)
        self._verify_account_selected(window, label)
        self._capture(window, f"account-{index + 1}")

    def current_account_label(self) -> str:
        """Read the active account from the bottom-left account summary."""
        try:
            from pywinauto import Application
            from pywinauto.findwindows import find_windows
        except ImportError as exc:
            raise AccountUIError("switch_failed", "未安装 pywinauto") from exc
        window = self._find_window(Application, find_windows)
        labels = self.calibration.get("accounts", [])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            active = self._active_account_from_summary(window, labels)
            if active:
                return active
            time.sleep(0.5)
        self._capture(window, "account-current-summary-missing")
        raise AccountUIError("switch_failed", "左下角账号摘要未提供唯一账号证据")

    def logged_in_account_labels(self) -> list[str]:
        """Enumerate exact configured labels shown by Doubao's account switcher."""
        try:
            from pywinauto import Application
            from pywinauto.findwindows import find_windows
        except ImportError as exc:
            raise AccountUIError("switch_failed", "未安装 pywinauto") from exc
        window = self._find_window(Application, find_windows)
        expected = self.calibration.get("accounts", [])
        best: list[str] = []
        for attempt in range(4):
            self._dismiss_open_popovers()
            self._coordinate_select(window, "account_avatar")
            time.sleep(0.5)
            uia_window = self._uia_window(window)
            switch = uia_window.child_window(title="切换账号", control_type="MenuItem")
            if switch.exists(timeout=1):
                switch.click_input()
            else:
                self._coordinate_select(window, "switch_account", self._responsive_point(window, "switch_account"))
            time.sleep(0.8)
            labels = self._visible_account_labels(window, expected)
            if len(labels) > len(best):
                best = labels
            self._capture(window, f"account-list-{attempt + 1}")
            self._dismiss_open_popovers()
            if len(best) == len(expected):
                return best
            time.sleep(1)
        return best

    @staticmethod
    def _dismiss_open_popovers() -> None:
        try:
            import pyautogui
            pyautogui.press("esc", presses=2, interval=0.1)
            time.sleep(0.2)
        except ImportError:
            return

    def _visible_account_labels(self, window, labels: list[str]) -> list[str]:
        texts = self._visible_texts(self._uia_window(window).descendants())
        return [label for label in labels if label in texts]

    def _active_account_from_summary(self, window, labels: list[str]) -> str | None:
        rect = window.rectangle()
        candidates: list[str] = []
        try:
            buttons = self._uia_window(window).descendants(control_type="Button")
        except Exception:
            return None
        for control in buttons:
            try:
                box = control.rectangle()
                if not control.is_visible() or box.bottom < rect.bottom - 80 or box.left > rect.left + rect.width() * 0.25:
                    continue
                title = str(control.window_text()).strip()
                matches = [label for label in labels if title == label or title.startswith(label + " ")]
                candidates.extend(matches)
            except Exception:
                continue
        unique = list(dict.fromkeys(candidates))
        return unique[0] if len(unique) == 1 else None

    def _verify_account_selected(self, window, label: str) -> None:
        """Require the bottom-left account summary to identify the target."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._active_account_from_summary(window, self.calibration.get("accounts", [])) == label:
                return
            time.sleep(0.5)
        self._capture(window, f"account-verify-{label}-failed")
        current = self._active_account_from_summary(window, self.calibration.get("accounts", []))
        if current is not None and current != label:
            raise AccountUIError("switch_failed", f"账号切换后确认到其他账号：{current}，目标{label}")
        # Chromium/WebView builds may expose no UIA text at all. When the
        # account list has been explicitly coordinate-calibrated and no
        # conflicting summary is readable, the successful target click plus a
        # closed menu is the only available local evidence; keep this fallback
        # opt-in and screenshot-backed.
        if self.calibration.get("coordinate_account_verification") is True:
            self._dismiss_open_popovers()
            self._capture(window, f"account-coordinate-verified-{label}")
            accounts = self.calibration.get("accounts", [])
            visible_accounts = self._visible_texts(self._uia_window(window).descendants())
            if not self._only_target_account_visible(accounts, label, visible_accounts):
                raise AccountUIError("switch_failed", f"账号切换后仍能识别到其他账号，目标{label}")
            return
        raise AccountUIError("switch_failed", f"账号切换后未确认当前账号：{label}")

    @staticmethod
    def _only_target_account_visible(accounts: list[str], target: str, visible_texts: list[str]) -> bool:
        visible_accounts = {account for account in accounts if account in visible_texts}
        return visible_accounts == {target}

    @staticmethod
    def _unique_rightmost_account_candidate(candidates: list):
        visible = [candidate for candidate in candidates if candidate.is_visible()]
        if not visible:
            raise UIBlocked("账号候选均不可见，拒绝坐标猜测")
        visible.sort(key=lambda candidate: candidate.rectangle().left)
        if len(visible) > 1 and visible[-1].rectangle().left == visible[-2].rectangle().left:
            raise UIBlocked("账号候选仍有歧义，拒绝点击")
        return visible[-1]

    def prepare_task(self, task: DoubaoTaskSpec) -> None:
        """Fill a task and references, stopping before quota-consuming submit."""
        try:
            from pywinauto import Application
            from pywinauto.findwindows import find_windows
        except ImportError as exc:
            raise UIBlocked("未安装 pywinauto") from exc
        if find_windows(title_re=".*(打开|Open).*"):
            raise UIBlocked("检测到遗留文件框未关闭，请先关闭后再运行")
        window = self._find_window(Application, find_windows)
        self._enter_video_mode(window)
        # Entering video mode can rebuild the Chromium/WebView host and
        # invalidate the old wrapper even though the visible window looks
        # unchanged. Reacquire it before any coordinate-based upload action.
        window = self._find_window(Application, find_windows)
        # The plus button may open either the native dialog directly or a
        # short-lived Doubao menu. Retry only when neither appears; this also
        # absorbs the account-switch repaint race seen on slower starts.
        dialog_found = False
        for _attempt in range(3):
            if find_windows(title_re=".*(打开|Open).*"):
                dialog_found = True
                break
            self._coordinate_select(window, "plus_upload")
            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline:
                if find_windows(title_re=".*(打开|Open).*"):
                    dialog_found = True
                    break
                menu = self._uia_window(window).child_window(title="上传文件或图片")
                if menu.exists(timeout=0.15):
                    menu.click_input()
                    time.sleep(0.4)
                    dialog_found = bool(find_windows(title_re=".*(打开|Open).*"))
                    break
                time.sleep(0.2)
            if dialog_found:
                break
            time.sleep(0.4)
        if not dialog_found:
            raise UIBlocked("未找到视频生成的上传文件入口，拒绝继续")
        self._fill_file_dialog(task.references)
        time.sleep(1)
        self._paste_prompt(window, task.prompt)

    @staticmethod
    def _fill_file_dialog(paths: list[str]) -> None:
        from pywinauto import Application
        from pywinauto.findwindows import find_windows
        handles = find_windows(title_re=".*(打开|Open).*")
        if not handles:
            raise UIBlocked("参考图文件对话框未出现")
        if len(handles) > 1:
            raise UIBlocked("检测到多个文件对话框，拒绝猜测上传目标")
        # Standard Windows Open dialogs expose reliable Win32 child controls;
        # UIA often returns an empty tree for the same dialog on this system.
        dialog = Application(backend="win32").connect(handle=handles[-1]).window(handle=handles[-1])
        edits = [control for control in dialog.descendants(class_name="Edit") if control.is_visible()]
        bottom_edits = [control for control in edits if control.rectangle().top > dialog.rectangle().bottom - 120]
        edit = bottom_edits[0] if bottom_edits else None
        if edit is None:
            raise UIBlocked("文件对话框缺少文件名输入框")
        value = " ".join(f'"{path}"' for path in paths)
        import pyperclip
        edit.click_input()
        pyperclip.copy(value)
        import pyautogui
        pyautogui.hotkey("ctrl", "a")
        pyautogui.hotkey("ctrl", "v")
        buttons = [control for control in dialog.descendants(class_name="Button") if control.is_visible() and control.rectangle().top > dialog.rectangle().bottom - 100]
        if not buttons:
            raise UIBlocked("文件对话框缺少打开按钮")
        # In a native Open dialog the leftmost bottom button is Open and the
        # rightmost is Cancel. Text matching is deliberately avoided because
        # pywinauto may decode localized captions as replacement characters.
        buttons.sort(key=lambda control: (control.rectangle().left, control.rectangle().top))
        buttons[0].click_input()
        time.sleep(0.8)
        if find_windows(title_re=".*(打开|Open).*"):
            raise UIBlocked("文件框点击后仍未关闭，拒绝继续提交")

    def _paste_prompt(self, window, prompt: str) -> None:
        import pyperclip
        import pyautogui
        rect = window.rectangle()
        point = self.calibration["coordinate_fallback"]["prompt_input"]
        pyautogui.click(rect.left + round((rect.right - rect.left) * point[0]), rect.top + round((rect.bottom - rect.top) * point[1]))
        pyperclip.copy(prompt)
        pyautogui.hotkey("ctrl", "v")

    def _find_window(self, application_cls, find_windows):
        pattern = self.calibration.get("window_title_regex", ".*豆包.*")
        handles = []
        for _attempt in range(4):
            # Prefer the visible, large top-level window owned by Doubao.
            # Title matching can return hidden/stale WebView handles after an
            # update, which then fail the foreground safety guard.
            handles = self._process_window_handles(find_windows)
            if handles:
                break
            # Window titles can be mojibake; use them only as a bounded
            # fallback when process enumeration is temporarily unavailable.
            handles = [handle for handle in find_windows(title_re=pattern)
                       if self._is_visible_usable_handle(handle)]
            if handles:
                break
            time.sleep(1.5)
        if not handles:
            raise UIBlocked("未找到可见的豆包窗口；请打开豆包并保持窗口可见")
        if len(handles) > 1:
            foreground = ctypes.windll.user32.GetForegroundWindow() if os.name == "nt" else 0
            handles = [handle for handle in handles if handle == foreground]
        if len(handles) != 1:
            raise UIBlocked("检测到多个可见豆包主窗口，拒绝猜测提交目标")
        window = application_cls(backend="win32").connect(handle=handles[0]).window(handle=handles[0])
        self._normalize_window(window)
        self._guard_foreground(window)
        return window

    @staticmethod
    def _is_visible_usable_handle(handle: int) -> bool:
        try:
            from pywinauto import Desktop
            window = Desktop(backend="win32").window(handle=handle)
            if not window.is_visible():
                return False
            rect = window.rectangle()
            return rect.width() >= 800 and rect.height() >= 500
        except Exception:
            return False

    @staticmethod
    def _process_window_handles(find_windows) -> list[int]:
        """Return only the visible, large top-level Doubao window handles.

        Doubao's MainWindowHandle can point at a 52x52 floating launcher. The
        actual chat window is another Chrome_WidgetWin child, so choose by
        process ownership and usable area rather than MainWindowHandle.
        """
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "(Get-Process -Name Doubao -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            pids = {int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()}
        except (OSError, ValueError, subprocess.SubprocessError):
            return []
        if not pids:
            return []
        try:
            from pywinauto import Desktop
            windows = Desktop(backend="win32").windows()
        except Exception:
            return []
        candidates: list[tuple[int, int, int]] = []
        for window in windows:
            try:
                if window.process_id() not in pids or not window.is_visible():
                    continue
                rect = window.rectangle()
                width, height = rect.width(), rect.height()
                if width < 800 or height < 500:
                    continue
                if window.class_name() not in {"Chrome_WidgetWin_1", "Chrome_WidgetWin_0"}:
                    continue
                priority = DoubaoUI._window_priority(window.window_text())
                if priority < 0:
                    continue
                candidates.append((priority, width * height, window.handle))
            except Exception:
                continue
        candidates.sort(reverse=True)
        ordered = [handle for _, _, handle in candidates]
        visible: list[int] = []
        for handle in ordered:
            try:
                visible.extend(find_windows(handle=handle))
            except Exception:
                visible.append(handle)
        return list(dict.fromkeys(visible))

    @staticmethod
    def _window_priority(title: str) -> int:
        """Score a Doubao window: chat/video windows win over auxiliary panes."""
        text = str(title or "")
        if any(key in text for key in ("图片查看器", "图片预览", "API 服务", "云盘", "设置", "更多", "技能 · 连接器 · 伙伴")):
            return -100
        if any(key in text for key in ("主对话", "新对话", "对话", "豆包", "视频", "分镜", "工作任务", "创建技能")):
            return 10
        return 0

    def _normalize_window(self, window) -> None:
        reference = self.calibration.get("coordinate_fallback", {}).get("reference_window_size", [1200, 800])
        rect = window.rectangle()
        if rect.width() == reference[0] and rect.height() == reference[1]:
            return
        try:
            window.restore()
            origin = self.calibration.get("reference_window_origin", [360, 120])
            window.move_window(x=int(origin[0]), y=int(origin[1]), width=int(reference[0]), height=int(reference[1]), repaint=True)
            time.sleep(0.5)
        except Exception as exc:
            raise UIBlocked("豆包窗口无法调整到已校准尺寸，拒绝坐标操作") from exc

    @staticmethod
    def _try_click(window, title: str) -> bool:
        control = window.child_window(title=title)
        if not control.exists(timeout=1):
            return False
        control.click_input()
        return True

    def _coordinate_select(self, window, key: str, point_override: list[float] | None = None) -> None:
        try:
            import pyautogui
        except ImportError as exc:
            raise UIBlocked("未安装 pyautogui，无法使用坐标备用定位") from exc
        self._guard_foreground(window)
        rect = window.rectangle()
        point = point_override or self.calibration.get("coordinate_fallback", {}).get(key)
        if not point:
            raise UIBlocked(f"缺少坐标校准：{key}")
        x = rect.left + round((rect.right - rect.left) * point[0])
        y = rect.top + round((rect.bottom - rect.top) * point[1])
        pyautogui.click(x, y)
        time.sleep(0.35)

    def _responsive_point(self, window, key: str, fallback: list[float] | None = None) -> list[float]:
        responsive = self.calibration.get("responsive_coordinates", {}).get(key)
        if responsive:
            size_key = "large" if window.rectangle().height() >= 900 else "reference"
            return responsive.get(size_key, fallback or [0.5, 0.5])
        return fallback or self.calibration.get("coordinate_fallback", {}).get(key)

    @staticmethod
    def _uia_window(window):
        from pywinauto import Application
        return Application(backend="uia").connect(handle=window.handle).window(handle=window.handle)

    def _enter_video_mode(self, window) -> None:
        uia_window = self._uia_window(window)
        if any(self._looks_like_video_parameter(control.window_text()) for control in uia_window.descendants(control_type="Button")):
            return
        video_candidates = [c for c in uia_window.descendants(control_type="Button") if "视频生成" in c.window_text() and c.is_visible()]
        if video_candidates:
            video_candidates[-1].click_input()
            time.sleep(0.7)
            return
        # Prefer the dedicated reusable video-storyboard project before
        # manipulating the composer mode. This remains safe (navigation only)
        # and works when the current conversation is another work-task page.
        project_candidates = [c for c in uia_window.descendants()
                              if "视频分镜制作" in c.window_text() and c.is_visible()]
        if project_candidates:
            project_candidates[-1].click_input()
            # Reacquire the WebView after navigation; the host handle may be
            # replaced during page transition.
            time.sleep(1.2)
            try:
                from pywinauto import Application
                from pywinauto.findwindows import find_windows
                window = self._find_window(Application, find_windows)
                uia_window = self._uia_window(window)
            except Exception as exc:
                raise UIBlocked("视频项目导航后豆包窗口未恢复，拒绝继续") from exc
            video_candidates = [c for c in uia_window.descendants(control_type="Button")
                                if "视频生成" in c.window_text() and c.is_visible()]
            if video_candidates:
                video_candidates[-1].click_input()
                time.sleep(0.7)
                return
        # The composer mode menu labels the current work-task mode as
        # "工作任务 本地电脑/云电脑", so an exact "工作任务" lookup is brittle.
        # Open the menu when needed, then select the stable "对话" item.
        menu = uia_window.child_window(title_re=".*对话.*工作任务.*", control_type="Menu")
        if not menu.exists(timeout=0.3):
            self._coordinate_select(window, "composer_plus")
            time.sleep(0.5)
            uia_window = self._uia_window(window)
            menu = uia_window.child_window(title_re=".*对话.*工作任务.*", control_type="Menu")
        dialog = uia_window.child_window(title_re="^对话$", control_type="MenuItem")
        if dialog.exists(timeout=1):
            dialog.click_input()
            time.sleep(0.7)
        else:
            # Some builds expose the menu but omit localized item titles.
            self._coordinate_select(window, "dialog_mode")
            time.sleep(0.7)
        uia_window = self._uia_window(window)
        video_candidates = [c for c in uia_window.descendants(control_type="Button") if "视频生成" in c.window_text() and c.is_visible()]
        if not video_candidates:
            project_candidates = [c for c in uia_window.descendants()
                                  if "视频分镜制作" in c.window_text() and c.is_visible()]
            if project_candidates:
                project_candidates[-1].click_input()
                time.sleep(1.0)
                uia_window = self._uia_window(window)
                video_candidates = [c for c in uia_window.descendants(control_type="Button")
                                    if "视频生成" in c.window_text() and c.is_visible()]
        if not video_candidates:
            # The video chip is visible in the conversation composer after the
            # mode switch. Use the calibrated fallback only after selector
            # lookup, and verify the page exposes a prompt editor afterwards.
            self._coordinate_select(window, "video_generation")
            time.sleep(0.7)
            uia_window = self._uia_window(window)
            if not any(self._looks_like_video_parameter(control.window_text()) for control in uia_window.descendants(control_type="Button")):
                raise UIBlocked("未找到豆包的视频生成入口，拒绝继续")
            return
        video_candidates[-1].click_input()
        time.sleep(0.7)

    @staticmethod
    def _looks_like_video_parameter(title: str) -> bool:
        normalized = title.replace(" ", "")
        return ("·" in title or "·" in normalized) and "s" in normalized

    def _select_model(self, window, model: str) -> None:
        uia_window = self._uia_window(window)
        buttons = [control for control in uia_window.descendants(control_type="Button") if control.window_text().startswith("模型 ")]
        if not buttons:
            raise UIBlocked("未找到模型选择控件，拒绝使用未知模型提交")
        current = buttons[-1].window_text()
        if model in current:
            return
        buttons[-1].click_input()
        time.sleep(0.5)
        options = [control for control in uia_window.descendants(control_type="MenuItem") if control.window_text().startswith(model)]
        if not options:
            raise UIBlocked(f"豆包中未找到指定模型：{model}")
        options[0].click_input()
        time.sleep(0.7)
        updated = [control for control in self._uia_window(window).descendants(control_type="Button") if control.window_text().startswith("模型 ")]
        if not updated or model not in updated[-1].window_text():
            raise UIBlocked(f"模型选择未验证成功：{model}")

    @staticmethod
    def _guard_foreground(window) -> None:
        if not window.exists(timeout=1):
            raise UIBlocked("豆包窗口不存在，拒绝坐标操作")
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                # A minimized window is recoverable. The previous order
                # rejected it before restore(), producing false GLOBAL_BLOCKED
                # results even though Doubao was running normally.
                window.restore()
                window.set_focus()
                time.sleep(0.2)
                if window.is_visible() and (os.name != "nt" or ctypes.windll.user32.GetForegroundWindow() == window.handle):
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(0.25)
        if last_error:
            raise UIBlocked("无法将豆包窗口置前，拒绝坐标操作") from last_error
        raise UIBlocked("豆包窗口未成为前台窗口，拒绝坐标操作")

    def _capture(self, window, label: str) -> str | None:
        if not self._evidence_enabled:
            return None
        now = time.monotonic()
        interval = float(self.calibration.get("evidence_min_interval_seconds", 2.0))
        if now - self._last_capture_at < interval:
            return None
        self._last_capture_at = now
        try:
            import pyautogui
            target = Path(self.calibration.get("evidence_dir", "data/ui-evidence"))
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"{label}-{int(time.time())}.png"
            pyautogui.screenshot(timeout=float(self.calibration.get("screenshot_timeout_seconds", 5))).save(path)
            return str(path)
        except TypeError:
            try:
                pyautogui.screenshot().save(path)
                return str(path)
            except Exception:
                return None
        except Exception:
            # Evidence is useful but must never weaken the safety gate.
            return None
