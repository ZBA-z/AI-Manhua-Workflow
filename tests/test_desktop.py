import unittest

from src.desktop import desktop_is_interactive


class _FakeUser32:
  def __init__(self, handle=123, switch_result=1):
    self.handle = handle
    self.switch_result = switch_result
    self.closed = []

  def OpenInputDesktop(self, *_args):
    return self.handle

  def SwitchDesktop(self, handle):
    return self.switch_result if handle == self.handle else 0

  def CloseDesktop(self, handle):
    self.closed.append(handle)
    return 1


class DesktopTests(unittest.TestCase):
  def test_open_input_desktop_failure_is_not_interactive(self):
    api = _FakeUser32(handle=0)
    self.assertFalse(desktop_is_interactive(api=api, platform_name="nt"))
    self.assertEqual(api.closed, [])

  def test_secure_desktop_is_not_interactive_and_handle_is_closed(self):
    api = _FakeUser32(switch_result=0)
    self.assertFalse(desktop_is_interactive(api=api, platform_name="nt"))
    self.assertEqual(api.closed, [123])

  def test_switchable_input_desktop_is_interactive_and_handle_is_closed(self):
    api = _FakeUser32()
    self.assertTrue(desktop_is_interactive(api=api, platform_name="nt"))
    self.assertEqual(api.closed, [123])


if __name__ == "__main__":
  unittest.main()
