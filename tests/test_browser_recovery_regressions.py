"""批量日志暴露的恢复和告警问题；所有窗口操作均使用替身。"""

import unittest
from unittest.mock import patch

import wechat_visual_rpa as rpa
import rpa_control_panel as panel
from browser_navigation import Page


class BrowserRecoveryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.window = rpa.WindowInfo(1, "微信", "Chrome_WidgetWin_0", rpa.Rect(0, 0, 1000, 800), "weixinappex.exe")

    def test_unknown_document_does_not_hide_browser(self):
        with patch.object(rpa, "find_sogou_search_window", side_effect=RuntimeError("未找到")), \
                patch.object(rpa, "enumerate_wechat_windows", return_value=[self.window]), \
                patch.object(rpa, "observe_browser_page", return_value=Page(1, "doc", "unknown", "视频")):
            self.assertEqual(rpa.find_navigation_browser(), self.window)

    def test_nonstandard_article_can_reach_strict_link_validation(self):
        page = Page(1, "doc", "unknown", "这是一篇有完整标题的视频文章")
        with patch.object(rpa, "observe_browser_page", return_value=page):
            self.assertEqual(rpa.wait_for_article_page(self.window, "甲", page.name), page)

    def test_unmatched_article_still_rejected(self):
        with patch.object(rpa, "log_event"):
            with self.assertRaisesRegex(RuntimeError, "未确认目标标题"):
                rpa.wait_for_article_page(self.window, "甲", "目标标题", timeout=0)

    def test_navigation_errors_are_not_interaction_ocr(self):
        cases = {
            "搜一搜连续3次打开公众号失败：本地识别失败，未出现搜一搜窗口": "search_recovery",
            "文章标签清理失败：页面跳转归属不确定": "navigation_return",
            "文章加载后仍未确认目标标题和公众号，保留页面": "article_identity",
        }
        for text, category in cases.items():
            self.assertEqual(rpa.classify_collection_error(RuntimeError(text)), category)
            self.assertIn(category, panel.FAILURE_RECOVERY_HINTS)

    def test_warning_uses_parsed_title(self):
        message = panel.format_process_event_message({"event": "article_title_evidence_warning", "parsed_title": "实际文章标题"})
        self.assertIn("实际文章标题", message)

    def test_batch_requires_verified_search_before_continuing(self):
        with patch.object(rpa, "find_navigation_browser", return_value=self.window), \
                patch.object(rpa, "browser_navigator") as navigator, patch.object(rpa, "log_event") as log:
            navigator.return_value.select.return_value = False
            with self.assertRaisesRegex(RuntimeError, "未找到可确认"):
                rpa.recover_batch_navigation("甲")
            log.assert_not_called()
            navigator.return_value.select.return_value = True
            rpa.recover_batch_navigation("甲")
            log.assert_called_once_with("batch_navigation_recovered", account="甲")

    def test_duplicate_control_server_cannot_bind_same_port(self):
        server = panel.ExclusiveControlServer(("127.0.0.1", 0), panel.ControlHandler)
        try:
            with self.assertRaises(OSError):
                other = panel.ExclusiveControlServer(server.server_address, panel.ControlHandler)
                other.server_close()
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()

