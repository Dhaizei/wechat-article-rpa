"""远端 9 月 14 日日志暴露的卡片定位问题。"""
import unittest
import wechat_visual_rpa as rpa
from browser_navigation import Page, Checkpoint, return_action


class RemoteDeploymentTests(unittest.TestCase):
    def test_tiny_truncated_fragment_rejected_but_short_real_title_kept(self):
        self.assertIsNone(rpa.build_card_title_signature("今天", {"title": "背...."}))
        self.assertIsNotNone(rpa.build_card_title_signature("今天", {"title": "开源"}))

    def test_fresh_ocr_requires_unique_date_and_visible_point(self):
        title = "20万元奖金！2026年第十三届百度奖学金开启申报"
        card = {"title": title, "center_x_1000": 500, "center_y_1000": 300}
        feed = {"time_labels": [{"text": "今天", "center_y_1000": 100}], "articles": [card]}
        rect = rpa.Rect(10, 20, 1010, 1020)
        self.assertEqual(rpa.locate_ocr_card(feed, title, "今天", rect), (510, 320))
        self.assertIsNone(rpa.locate_ocr_card(feed, title, "昨天", rect))
        self.assertIsNone(rpa.locate_ocr_card({**feed, "time_labels": []}, title, "今天", rect))
        with self.assertRaisesRegex(RuntimeError, "多个匹配"):
            rpa.locate_ocr_card({**feed, "articles": [card, {**card, "center_y_1000": 500}]}, title, "今天", rect)

    def test_profile_reuse_does_not_accept_disappearing_article(self):
        search = Page(1, "s", "search", "搜一搜")
        old_article = Page(1, "a", "article", "用户文章", "甲")
        new = Page(1, "p", "profile", "乙", "乙")
        self.assertEqual(return_action(Checkpoint(search, (search, old_article), "乙"), new, (search, new)), "unknown")

