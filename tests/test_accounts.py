import unittest

from src.accounts import AccountInspector
from src.doubao_ui import AccountUIError


class FakeAccountUI:
  def __init__(self, current="沉雪", labels=None):
    self.current = current
    self.labels = labels or ["沉雪", "为你", "魏来", "超越", "武哥", "雨落下不见你"]
    self.switches = []

  def current_account_label(self):
    return self.current

  def logged_in_account_labels(self):
    return list(self.labels)

  def switch_account(self, label):
    if label not in self.labels:
      raise AccountUIError("not_logged_in", label)
    self.switches.append(label)
    self.current = label


class AccountInspectorTests(unittest.TestCase):
  def test_ensure_account_falls_back_when_summary_is_unavailable(self):
    ui = FakeAccountUI()
    ui.current_account_label = lambda: (_ for _ in ()).throw(AccountUIError("switch_failed", "summary unavailable"))
    evidence = AccountInspector(ui).ensure_account("为你")
    self.assertEqual(evidence.method, "coordinate-switch-no-summary")
    self.assertEqual(ui.switches, ["为你"])

  def test_ensure_account_short_circuits_when_already_active(self):
    ui = FakeAccountUI()
    evidence = AccountInspector(ui).ensure_account("沉雪")
    self.assertEqual(evidence.method, "already-active")
    self.assertEqual(ui.switches, [])

  def test_audit_switches_all_six_and_restores_original(self):
    ui = FakeAccountUI(current="魏来")
    result = AccountInspector(ui).audit_accounts(ui.labels)
    self.assertEqual([item["label"] for item in result["evidence"]], ui.labels)
    self.assertEqual(ui.current, "魏来")
    self.assertEqual(result["restored"], "魏来")

  def test_audit_rejects_missing_logged_in_account_before_switching(self):
    ui = FakeAccountUI(labels=["沉雪", "为你"])
    with self.assertRaisesRegex(AccountUIError, "未出现在切换列表"):
      AccountInspector(ui).audit_accounts(["沉雪", "为你", "魏来"])
    self.assertEqual(ui.switches, [])


if __name__ == "__main__":
  unittest.main()
