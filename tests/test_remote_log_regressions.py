"""远程 928×1160 窗口日志回归；不操作真实微信或数据库。"""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from browser_navigation import feed_from_probe
import wechat_visual_rpa as rpa
import rpa_control_panel as panel


class RemoteLogRegressions(unittest.TestCase):
    def test_repeated_metric_nodes_never_become_titles(self):
        def row(text, y):
            return {"text": text, "rect": [20, y, 500, y + 20]}
        metric = "阅读\u20063.0万\u2004\u2005赞\u2006295"
        rows = [row(t, 80) for t in ["全部", "文章", "私信", "昨天"]]
        card = [row("谷歌DeepMind，已经破解RSI?", 200), row(metric, 240)]
        rows += card + [row(metric, 240)] * 3 + card
        data = {"hwnd": 1, "documents": [{"id": [1], "name": "新智元", "rows": rows}]}
        feed = feed_from_probe(data, (0, 0, 928, 1160), "新智元")
        self.assertEqual([a["title"] for a in feed["articles"]], [card[0]["text"]])
        self.assertEqual(feed["articles"][0]["list_read_count"], 30000)

    def test_empty_normalized_titles_cannot_match(self):
        self.assertFalse(rpa.titles_match("阅读 3.0万 赞295", "阅读 2.7万 赞240"))
        self.assertFalse(rpa.titles_match("", ""))

    def collect_repeated_card(self, title):
        card = {"title": title, "center_y_1000": 200, "center_x_1000": 500,
                "screen_point": (200, 200), "list_read_count": 30000, "list_like_count": 295}
        feed = {"time_labels": [{"text": "今天", "center_y_1000": 100}], "articles": [card]}
        end = {"time_labels": [{"text": "结束", "center_y_1000": 100}], "articles": []}
        window = rpa.WindowInfo(0, "公众号", "test", rpa.Rect(0, 0, 928, 1160))
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            for name in ["activate_window", "click", "close_article_after_attempt", "scroll_window_down", "press_ctrl_home", "log_event"]:
                stack.enter_context(patch.object(rpa, name))
            stack.enter_context(patch.object(rpa.time, "sleep"))
            stack.enter_context(patch.object(rpa, "search_and_open_profile", return_value=(window, "新智元")))
            stack.enter_context(patch.object(rpa, "analyze_profile_window", side_effect=[feed, feed, feed, end]))
            stack.enter_context(patch.object(rpa, "is_older_time_boundary", side_effect=lambda t: t == "结束"))
            collect = stack.enter_context(patch.object(rpa, "collect_open_article", side_effect=RuntimeError("同一日期存在多个匹配标题，拒绝猜测文章")))
            result = rpa.collect_profile_account(None, "新智元", Path(directory), 20, None, None, scan_range="today")
            return result, collect.call_count

    def test_ambiguous_card_is_terminal_across_pages(self):
        result, calls = self.collect_repeated_card("一篇身份不明确的测试文章")
        self.assertEqual(calls, 1)
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["detected_articles"], 1)

    def test_metric_card_is_rejected_before_opening(self):
        result, calls = self.collect_repeated_card("阅读\u20063.0万\u2004\u2005赞\u2006295")
        self.assertEqual(calls, 0)
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["detected_articles"], 0)

    def test_partial_account_is_not_reported_successful(self):
        event = {"event": "account_collection_finished", "account": "新智元", "detected_articles": 17,
                 "collected": [{"title": "已完成"}], "failures": [{"error": "标题歧义", "category": "card_identity"}]}
        state = panel.ControlState()
        state._record_progress_event(event)
        summary = state._run_summary_locked()
        self.assertEqual(summary["accounts_succeeded"], 0)
        self.assertEqual(summary["accounts_failed"], 1)
        self.assertEqual(summary["articles_failed"], 1)
        self.assertIn("成功 1 篇，失败 1 篇", panel.format_process_event_message(event))

    def test_partial_article_success_produces_partial_run_status(self):
        status, _ = panel.determine_final_run_status(exit_code=0, manually_stopped=False,
            summary={"accounts_failed": 1, "accounts_succeeded": 0, "articles_collected": 1})
        self.assertEqual(status, "partial")

    def test_all_article_failures_are_counted_even_when_detected_positive(self):
        state = panel.ControlState()
        state._record_progress_event({"event": "account_collection_finished", "account": "甲",
            "detected_articles": 17, "collected": [], "failures": [{"error": "身份歧义"}]})
        summary = state._run_summary_locked()
        self.assertEqual(summary["accounts_failed"], 1)
        self.assertEqual(summary["accounts_succeeded"], 0)


if __name__ == "__main__":
    unittest.main()

