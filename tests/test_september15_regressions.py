"""远端九月十五日失败类型：折行、短标题和解析重试。"""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import wechat_visual_rpa as rpa
from browser_navigation import Page, feed_from_probe
from title_geometry import adjacent_title_lines


class RemoteRegressionTests(unittest.TestCase):
    def test_multiline_title_keeps_first_line(self):
        first = dict(text="ECCV 2026｜不靠「上帝视角」，也不等", left=100, right=600, top=200, bottom=220)
        last = dict(text="「完美指令」：REAL让具身智能体走向开…", left=100, right=590, top=225, bottom=245)
        poster = dict(text="封面文案", left=100, right=600, top=120, bottom=150)
        self.assertEqual(adjacent_title_lines([poster, first, last], last), [first, last])

    def test_uia_multiline_title_keeps_first_line(self):
        def row(text, top, right=600):
            return dict(text=text, rect=[100, top, right, top+20])
        data = {"hwnd": 1, "documents": [{"id": [1], "name": "甲", "rows": [
            row("全部", 10), row("文章", 35), row("私信", 60), row("今天", 100),
            row("ECCV 2026｜不靠「上帝视角」，也不等", 200),
            row("「完美指令」：REAL让具身智能体走向开…", 225, 590),
            row("阅读100赞3", 260)]}]}
        title = feed_from_probe(data, (0, 0, 1000, 1000), "甲")["articles"][0]["title"]
        self.assertTrue(title.startswith("ECCV 2026"))
        self.assertIn("完美指令", title)

    def test_ambiguous_or_different_size_lines_not_joined(self):
        last = dict(left=100, right=500, top=200, bottom=220)
        a = dict(left=100, right=500, top=175, bottom=195)
        self.assertEqual(adjacent_title_lines([a, dict(a), last], last), [last])
        a["top"] = 150
        self.assertEqual(adjacent_title_lines([a, last], last), [last])

    def test_short_title_still_requires_unique_date(self):
        card = dict(title="RSI闭环", center_x_1000=500, center_y_1000=300)
        feed = dict(time_labels=[dict(text="今天", center_y_1000=100)], articles=[card])
        rect = SimpleNamespace(left=0, top=0, width=1000, height=1000)
        self.assertEqual(rpa.locate_ocr_card(feed, "RSI闭环", "今天", rect), (500, 300))
        self.assertIsNone(rpa.locate_ocr_card(feed, "RSI闭环", "昨天", rect))
        feed["articles"].append(dict(card))
        with self.assertRaises(RuntimeError):
            rpa.locate_ocr_card(feed, "RSI闭环", "今天", rect)

    def test_stable_wrong_title_fails_early(self):
        page = Page(1, "a", "article", "突发！Opus 5.2深夜上线，RSI真来了？", "新智元")
        with patch.object(rpa, "observe_browser_page", return_value=page) as observe, \
                patch.object(rpa.time, "sleep"), patch.object(rpa, "log_event"):
            with self.assertRaises(rpa.ArticleMismatchError):
                rpa.wait_for_article_page(SimpleNamespace(hwnd=1), "新智元", "真的RSI来了")
            self.assertEqual(observe.call_count, 3)

    def test_transient_empty_response_retried_in_place(self):
        page = {"title": "标题"}
        with patch.object(rpa, "parse_page", side_effect=[ValueError("文章页面没有标题"), page]) as parse, \
                patch.object(rpa.time, "sleep"), patch.object(rpa, "log_event"):
            self.assertEqual(rpa.parse_article_with_retry("url"), page)
            self.assertEqual(parse.call_count, 2)

    def test_persistent_empty_response_remains_failure(self):
        with patch.object(rpa, "parse_page", side_effect=ValueError("文章页面没有标题")) as parse, \
                patch.object(rpa.time, "sleep"), patch.object(rpa, "log_event"):
            with self.assertRaises(ValueError):
                rpa.parse_article_with_retry("url")
            self.assertEqual(parse.call_count, 3)

