"""浏览器标签清理的离线回归测试：宁可少清理，也不能关闭搜一搜页。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

import wechat_visual_rpa as rpa


class BrowserTabCleanupTests(unittest.TestCase):
    def test_cleanup_preserves_search_page_when_screen_changes(self) -> None:
        """页面动画造成截图差异时，搜索框仍存在就不应执行 Ctrl+W。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        image = Image.new("RGB", (1000, 800), "white")

        with (
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", side_effect=[image, image]),
            patch.object(rpa, "press_ctrl_tab"),
            patch.object(rpa, "press_ctrl_w") as close_tab,
            patch.object(rpa, "_tab_switch_difference", return_value=8.0),
            patch.object(rpa.PROFILE_OCR, "locate_search_box", return_value={"found": True}),
            patch.object(rpa, "log_event"),
        ):
            removed = rpa.keep_only_search_tab(window, "测试公众号")

        self.assertEqual(removed, 0)
        close_tab.assert_not_called()


if __name__ == "__main__":
    unittest.main()
