"""浏览器标签清理的离线回归测试：宁可少清理，也不能关闭搜一搜页。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

import wechat_visual_rpa as rpa


class BrowserTabCleanupTests(unittest.TestCase):
    def test_recovery_never_closes_stale_window(self) -> None:
        """页面校验失败只允许无损新建，绝不能销毁用户当前窗口。"""
        stale = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
            process_name="wechatappex.exe",
        )
        recovered = rpa.WindowInfo(
            hwnd=200,
            title="搜一搜",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
            process_name="wechatappex.exe",
        )
        with (
            patch.object(rpa, "close_window") as close_window,
            patch.object(
                rpa,
                "open_sogou_from_wechat_main",
                return_value=recovered,
            ) as open_search,
            patch.object(rpa, "arrange_automation_window", return_value=recovered),
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "press_ctrl_1"),
            patch.object(rpa.time, "sleep"),
            patch.object(rpa, "log_event"),
        ):
            result = rpa.recreate_sogou_search_window(stale, "测试公众号", "页面校验失败")

        self.assertEqual(result.hwnd, recovered.hwnd)
        close_window.assert_not_called()
        open_search.assert_called_once_with("测试公众号", excluded_hwnds={stale.hwnd})

    def test_cleanup_recreates_search_window_when_search_tab_is_missing(self) -> None:
        """旧窗口找不到搜一搜标签时，应重建干净窗口而不是终止整个账号。"""
        stale = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        recovered = rpa.WindowInfo(
            hwnd=200,
            title="搜一搜",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        with (
            patch.object(rpa, "find_sogou_search_window", return_value=stale),
            patch.object(rpa, "find_and_pin_search_tab", side_effect=[False, True]) as pin,
            patch.object(rpa, "recreate_sogou_search_window", return_value=recovered) as recreate,
            patch.object(rpa, "keep_only_search_tab", return_value=0) as normalize,
            patch.object(rpa, "log_event"),
        ):
            rpa.close_article_tabs_until_search("测试公众号")

        recreate.assert_called_once_with(
            stale,
            "测试公众号",
            "遍历现有标签后未找到真正的搜一搜结果页",
        )
        self.assertEqual(pin.call_count, 2)
        normalize.assert_called_once_with(recovered, "测试公众号")

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
            patch.object(rpa, "capture_window", side_effect=[image, image, image]),
            patch.object(rpa, "press_ctrl_1"),
            patch.object(rpa, "press_ctrl_9"),
            patch.object(rpa, "press_ctrl_w") as close_tab,
            patch.object(rpa, "_tab_switch_difference", side_effect=[2.5, 8.0]),
            patch.object(rpa.PROFILE_OCR, "locate_search_box", return_value={"found": True}),
            patch.object(rpa.PROFILE_OCR, "locate_account_tab", return_value={"found": True}),
            patch.object(rpa, "log_event"),
        ):
            removed = rpa.keep_only_search_tab(window, "测试公众号")

        self.assertEqual(removed, 0)
        close_tab.assert_not_called()

    def test_cleanup_closes_false_positive_search_box_on_article_tab(self) -> None:
        """文章分享弹窗含搜索框时，只要与首标签差异显著仍应关闭该文章标签。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        image = Image.new("RGB", (1000, 800), "white")

        with (
            patch.object(rpa, "activate_window") as activate,
            patch.object(rpa, "capture_window", side_effect=[image, image, image, image, image]),
            patch.object(rpa, "press_ctrl_1") as first_tab,
            patch.object(rpa, "press_ctrl_9") as last_tab,
            patch.object(rpa, "press_ctrl_w") as close_tab,
            patch.object(rpa, "_tab_switch_difference", side_effect=[0.2, 22.0, 0.0]),
            patch.object(rpa.PROFILE_OCR, "locate_search_box", return_value={"found": True}),
            patch.object(rpa.PROFILE_OCR, "locate_account_tab", return_value={"found": True}),
            patch.object(rpa.user32, "IsWindow", return_value=True),
            patch.object(rpa, "log_event"),
        ):
            removed = rpa.keep_only_search_tab(window, "测试公众号")

        self.assertEqual(removed, 1)
        close_tab.assert_called_once()
        self.assertEqual(last_tab.call_count, 2)
        # 初始定位首标签一次，关闭文章后再次回首标签校验。
        self.assertGreaterEqual(first_tab.call_count, 2)
        self.assertGreaterEqual(activate.call_count, 4)

    def test_search_tab_is_found_and_moved_back_to_first_position(self) -> None:
        """首标签是文章时，应遍历标签并把第三个搜一搜标签移回首位。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        image = Image.new("RGB", (1000, 800), "white")
        missing = {
            "found": False,
            "search_box": {"found": False},
            "account_tab": {"found": False},
        }
        found = {
            "found": True,
            "search_box": {"found": True},
            "account_tab": {"found": True},
        }

        with (
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=image),
            patch.object(rpa, "press_ctrl_1") as first_tab,
            patch.object(rpa, "press_ctrl_tab") as next_tab,
            patch.object(rpa, "press_ctrl_shift_pageup") as move_left,
            patch.object(rpa, "_inspect_sogou_search_results", side_effect=[missing, missing, found, found]),
            patch.object(rpa, "log_event"),
        ):
            recovered = rpa.find_and_pin_search_tab(window, "测试公众号", max_tabs=5)

        self.assertTrue(recovered)
        self.assertEqual(next_tab.call_count, 2)
        self.assertEqual(move_left.call_count, 2)
        self.assertEqual(first_tab.call_count, 2)

    def test_direct_close_keeps_search_tab_without_global_probe(self) -> None:
        """文章正常采集后应直接关当前标签，不触发全量标签轮询。"""
        window = rpa.WindowInfo(
            hwnd=100,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )
        image = Image.new("RGB", (1000, 800), "white")
        article_page = {"found": False, "search_box": {"found": False}, "account_tab": {"found": False}}
        search_page = {"found": True, "search_box": {"found": True}, "account_tab": {"found": True}}

        with (
            patch.object(rpa, "find_article_window", return_value=(window.hwnd, window.rect)),
            patch.object(rpa.user32, "GetForegroundWindow", return_value=window.hwnd),
            patch.object(rpa, "capture_window", side_effect=[image, image]),
            patch.object(rpa, "_inspect_sogou_search_results", side_effect=[article_page, search_page]),
            patch.object(rpa, "press_ctrl_w") as close_tab,
            patch.object(rpa, "find_sogou_search_window", return_value=window),
            patch.object(rpa, "log_event"),
        ):
            closed = rpa.close_current_article_tab("测试公众号", "测试文章")

        self.assertTrue(closed)
        close_tab.assert_called_once()

    def test_cleanup_falls_back_when_direct_close_is_uncertain(self) -> None:
        """无法确认当前标签是文章时，仍使用原有安全恢复流程。"""
        with (
            patch.object(rpa, "close_current_article_tab", return_value=False),
            patch.object(rpa, "close_article_tabs_until_search") as recover,
            patch.object(rpa, "log_event"),
        ):
            rpa.close_article_after_attempt("测试公众号", "测试文章")

        recover.assert_called_once_with("测试公众号")


if __name__ == "__main__":
    unittest.main()
