"""微信新版窗口标题的角色识别回归测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import wechat_visual_rpa as rpa


def window(hwnd: int, title: str, *, width: int, height: int) -> rpa.WindowInfo:
    """构造无需真实桌面的测试窗口。"""
    return rpa.WindowInfo(
        hwnd=hwnd,
        title=title,
        class_name="Chrome_WidgetWin_0",
        rect=rpa.Rect(0, 0, width, height),
        process_name="wechatappex.exe",
    )


class WindowRoleDetectionTests(unittest.TestCase):
    def test_plain_chrome_titled_wechat_is_never_treated_as_sogou(self) -> None:
        """普通 Chrome 标签即使标题恰好叫“微信”，也不能进入自动化窗口集合。"""
        chrome = rpa.WindowInfo(
            hwnd=99,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1600, 1000),
            process_name="chrome.exe",
        )

        self.assertFalse(rpa.is_sogou_search_window(chrome))

    def test_unknown_process_is_rejected_fail_closed(self) -> None:
        """进程归属查询失败时必须拒绝，不能退回仅凭标题猜测。"""
        unknown = rpa.WindowInfo(
            hwnd=98,
            title="搜一搜",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 1000, 800),
        )

        self.assertFalse(rpa.is_sogou_search_window(unknown))

    def test_search_lookup_can_exclude_stale_window(self) -> None:
        """无损恢复时只接受新 HWND，避免再次选回旧页面。"""
        stale = window(101, "微信", width=1200, height=900)
        recovered = window(202, "搜一搜", width=1000, height=800)

        with patch.object(
            rpa, "enumerate_wechat_windows", return_value=[stale, recovered]
        ):
            detected = rpa.find_sogou_search_window({stale.hwnd})

        self.assertEqual(detected.hwnd, recovered.hwnd)

    def test_close_window_refuses_non_wechat_process(self) -> None:
        """底层整窗关闭也要独立校验，避免未来调用方绕过上层筛选。"""
        with (
            patch.object(rpa, "window_process_name", return_value="chrome.exe"),
            patch.object(rpa.user32, "PostMessageW") as post_message,
        ):
            with self.assertRaisesRegex(RuntimeError, "拒绝关闭非微信窗口"):
                rpa.close_window(99)

        post_message.assert_not_called()

    def test_new_account_search_title_is_recognized_as_sogou(self) -> None:
        """新版标题“公众号名 - 公众号搜一搜”必须被当作左侧搜索窗口。"""
        search = window(101, "GameLook - 公众号搜一搜", width=1050, height=1320)

        with patch.object(rpa, "enumerate_wechat_windows", return_value=[search]):
            detected = rpa.find_sogou_search_window()

        self.assertEqual(detected.hwnd, search.hwnd)

    def test_profile_detection_excludes_larger_sogou_window(self) -> None:
        """搜一搜即使更大且标题含“公众号”，也不能挤掉真正的资料页。"""
        search = window(101, "GameLook - 公众号搜一搜", width=1050, height=1320)
        profile = window(202, "公众号", width=640, height=820)

        with patch.object(rpa, "enumerate_wechat_windows", return_value=[search, profile]):
            detected = rpa.find_official_profile_window()

        self.assertEqual(detected.hwnd, profile.hwnd)

    def test_explicit_sogou_title_wins_over_legacy_wechat_candidate(self) -> None:
        """同时存在旧版候选时，应优先使用标题明确包含“搜一搜”的窗口。"""
        legacy = window(303, "微信", width=1400, height=1000)
        explicit = window(404, "GameLook - 公众号搜一搜", width=1050, height=900)

        with patch.object(rpa, "enumerate_wechat_windows", return_value=[legacy, explicit]):
            detected = rpa.find_sogou_search_window()

        self.assertEqual(detected.hwnd, explicit.hwnd)


if __name__ == "__main__":
    unittest.main()
