"""旧版微信系统标题与可见搜索标签不一致时的回归测试。"""

import unittest
from unittest.mock import patch

from PIL import Image

import rpa_control_panel as panel
import wechat_visual_rpa as rpa


class LegacySearchPreflightTests(unittest.TestCase):
    def setUp(self):
        panel.LEGACY_SEARCH_CHECK_CACHE.clear()
        self.window = {
            "hwnd": "123", "title": "微信",
            "class_name": "Chrome_WidgetWin_0", "process_name": "wechatappex.exe",
        }

    def tearDown(self):
        panel.LEGACY_SEARCH_CHECK_CACHE.clear()

    def test_verified_legacy_window_is_ready_and_cached(self):
        with patch.object(panel, "_visible_windows", return_value=[self.window]), \
                patch.object(rpa, "find_navigation_browser", return_value=object()) as find:
            self.assertTrue(panel.collect_preflight()["ready"])
            self.assertTrue(panel.collect_preflight()["ready"])
            find.assert_called_once()
            # 窗口关闭后必须立即撤销就绪状态，不沿用正缓存。
            self.assertFalse(panel._legacy_search_ready([]))
            self.window["hwnd"] = "456"
            self.assertTrue(panel.collect_preflight()["ready"])
            self.assertEqual(find.call_count, 2)

    def test_unverified_appex_is_not_ready(self):
        with patch.object(rpa, "find_navigation_browser", side_effect=RuntimeError("未找到")):
            self.assertFalse(panel._legacy_search_ready([self.window]))

    def test_occluded_window_uses_tab_name_without_screen_capture(self):
        """被其他应用遮住或停在文章页时，搜索标签仍然存在。"""
        window = rpa.WindowInfo(123, "微信", "Chrome_WidgetWin_0",
                                rpa.Rect(0, 0, 1000, 800), "wechatappex.exe")
        with patch.object(rpa, "enumerate_wechat_windows", return_value=[window]), \
                patch.object(rpa, "has_search_tab", return_value=True), \
                patch.object(rpa, "capture_window", side_effect=AssertionError("不能截图")):
            self.assertEqual(rpa.find_sogou_search_window().hwnd, 123)

    def test_missing_search_tab_still_requires_page_evidence(self):
        window = rpa.WindowInfo(123, "微信", "Chrome_WidgetWin_0",
                                rpa.Rect(0, 0, 1000, 800), "wechatappex.exe")
        with patch.object(rpa, "enumerate_wechat_windows", return_value=[window]), \
                patch.object(rpa, "has_search_tab", return_value=False), \
                patch.object(rpa, "capture_window", return_value=Image.new("RGB", (10, 10))), \
                patch.object(rpa, "_inspect_sogou_search_results", return_value={"found": False}):
            with self.assertRaises(RuntimeError):
                rpa.find_sogou_search_window()

    def test_main_window_does_not_trigger_ocr(self):
        self.window["process_name"] = "weixin.exe"
        with patch.object(rpa, "find_sogou_search_window") as find:
            self.assertFalse(panel._legacy_search_ready([self.window]))
            find.assert_not_called()

    def test_hidden_account_tab_accepts_only_complete_filter_row(self):
        screenshot = Image.new("RGB", (1600, 1600))
        for text, search_found, expected in [
            ("不限 小程序 公众号 服务号 视频号", True, True),
            ("公众号", True, False),
            ("不限 小程序 公众号 服务号 视频号", False, False),
        ]:
            with self.subTest(text=text, search_found=search_found), \
                    patch.object(rpa.PROFILE_OCR, "locate_search_box", return_value={"found": search_found}), \
                    patch.object(rpa.PROFILE_OCR, "locate_account_tab", return_value={"found": False}), \
                    patch.object(rpa.PROFILE_OCR, "_rows", return_value=[
                        {"normalized": text, "center_x": 400, "center_y": 260},
                    ]):
                self.assertEqual(rpa._inspect_sogou_search_results(screenshot)["found"], expected)


if __name__ == "__main__":
    unittest.main()

