"""observe/screen.py — ScreenSource：mss 截屏 + pywin32 窗口信息。"""

from __future__ import annotations

import mss
import mss.tools
import win32gui
from PIL import Image


class ScreenSource:
    def __init__(self, monitor: int = 1) -> None:
        self._monitor_index = monitor
        self._sct = mss.MSS()

    def capture(self) -> Image.Image:
        """全屏截图（RGB）。mss: raw.rgb + raw.size 与 mss.tools.to_png 同一口径。"""
        raw = self._sct.grab(self._sct.monitors[self._monitor_index])
        return Image.frombytes("RGB", raw.size, raw.rgb)

    def monitor_size(self) -> tuple[int, int]:
        mon = self._sct.monitors[self._monitor_index]
        return mon["width"], mon["height"]

    @staticmethod
    def foreground_title() -> str:
        try:
            return win32gui.GetWindowText(win32gui.GetForegroundWindow())
        except Exception:
            return ""

    def is_fullscreen(self) -> bool:
        """前台窗口覆盖 ≥95% 屏幕判全屏。"""
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return False
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            win_area = max(0, right - left) * max(0, bottom - top)
            mon_w, mon_h = self.monitor_size()
            if win_area >= 0.95 * mon_w * mon_h:
                return True
        except Exception:
            return False
        return False

    def title_blacklisted(self, blacklist: tuple[str, ...]) -> bool:
        title = self.foreground_title()
        return any(word and word in title for word in blacklist)

    def close(self) -> None:
        self._sct.close()
