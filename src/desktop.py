from __future__ import annotations

import ctypes
import os


DESKTOP_SWITCHDESKTOP = 0x0100


def desktop_is_interactive(api=None, platform_name: str | None = None) -> bool:
    """Return whether the current Windows input desktop accepts switching."""
    platform_name = os.name if platform_name is None else platform_name
    if platform_name != "nt":
        return True
    try:
        user32 = api if api is not None else ctypes.windll.user32
        handle = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not handle:
            return False
        try:
            return bool(user32.SwitchDesktop(handle))
        finally:
            user32.CloseDesktop(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return False
