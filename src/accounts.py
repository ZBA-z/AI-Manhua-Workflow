from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .doubao_ui import AccountUIError, DoubaoUI


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    label: str | None
    visible_labels: tuple[str, ...]
    method: str
    captured_at: str
    screenshot: str | None = None


class AccountInspector:
    """Evidence-first account inspection; it never prepares or submits a task."""

    def __init__(self, ui: DoubaoUI):
        self.ui = ui

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def current_account(self) -> AccountEvidence:
        label = self.ui.current_account_label()
        return AccountEvidence(label, (label,), "uia-profile-menu", self._now())

    def list_logged_in_accounts(self) -> tuple[str, ...]:
        return tuple(self.ui.logged_in_account_labels())

    def ensure_account(self, label: str) -> AccountEvidence:
        try:
            current = self.current_account()
        except AccountUIError:
            # WebView builds often hide the bottom-left summary from UIA. The
            # calibrated account switcher remains usable, so switch directly
            # instead of poisoning every account with a false UI cooldown.
            self.ui.switch_account(label)
            return AccountEvidence(label, (label,), "coordinate-switch-no-summary", self._now())
        if current.label == label:
            return AccountEvidence(label, current.visible_labels, "already-active", self._now())
        self.ui.switch_account(label)
        # DoubaoUI.switch_account is a proof-producing contract: it returns
        # only after reopening the profile menu and uniquely verifying label.
        # Reading it again during the post-switch page repaint is redundant
        # and can observe a transient empty WebView tree.
        return AccountEvidence(label, (label,), "uia-switch-and-verify", self._now())

    def audit_accounts(self, expected_labels: list[str], restore_original: bool = True, restore_label: str | None = None) -> dict:
        original = self.current_account().label
        target_restore = restore_label or original
        evidence: list[dict] = []
        try:
            logged_in = self.list_logged_in_accounts()
            missing = [label for label in expected_labels if label not in logged_in]
            if missing:
                raise AccountUIError("not_logged_in", f"账号未出现在切换列表：{','.join(missing)}")
            for label in expected_labels:
                evidence.append(asdict(self.ensure_account(label)))
        finally:
            if restore_original and target_restore:
                self.ensure_account(target_restore)
        return {"original": original, "logged_in": list(logged_in), "evidence": evidence, "restored": target_restore if restore_original else None}
