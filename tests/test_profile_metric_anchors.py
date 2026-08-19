from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

from wechat_profile_ocr import WeChatProfileOCR


def row(text: str, left: float, top: float, right: float, bottom: float) -> dict[str, object]:
    return {
        "text": text,
        "normalized": "".join(text.split()),
        "confidence": 0.99,
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "center_x": (left + right) / 2,
        "center_y": (top + bottom) / 2,
    }


class ProfileMetricAnchorTests(unittest.TestCase):
    def test_cover_text_is_not_treated_as_article_title(self) -> None:
        screenshot = Image.new("RGB", (1000, 1600), "white")
        rows = [
            row("昨天", 100, 200, 160, 230),
            row("DeepSeek-V4-Pro-Preview", 260, 350, 520, 375),
            row("19. airorb", 260, 390, 390, 415),
            row("Datawhale首次进入全球前25！", 260, 620, 570, 655),
            row("阅读3.7万 赞816", 260, 675, 470, 705),
        ]
        detector = WeChatProfileOCR()
        with patch.object(detector, "_rows", return_value=rows):
            feed = detector.inspect_profile_feed(screenshot)

        self.assertEqual(feed["recognition_method"], "rapidocr-profile-feed-metric-anchored")
        self.assertEqual([item["title"] for item in feed["articles"]], ["Datawhale首次进入全球前25！"])
        self.assertEqual(feed["articles"][0]["list_read_count"], 37000)
        self.assertEqual(feed["articles"][0]["list_like_count"], 816)

    def test_two_column_metrics_keep_titles_in_their_own_cards(self) -> None:
        screenshot = Image.new("RGB", (1000, 1600), "white")
        rows = [
            row("左侧封面噪声", 250, 420, 430, 445),
            row("右侧封面噪声", 560, 420, 740, 445),
            row("左侧真实文章标题", 250, 610, 470, 640),
            row("右侧真实文章标题", 560, 610, 780, 640),
            row("阅读1.2万 赞1121", 250, 660, 470, 690),
            row("阅读2.7万 赞6691", 560, 660, 780, 690),
        ]
        detector = WeChatProfileOCR()
        with patch.object(detector, "_rows", return_value=rows):
            feed = detector.inspect_profile_feed(screenshot)

        self.assertEqual(
            [item["title"] for item in feed["articles"]],
            ["左侧真实文章标题", "右侧真实文章标题"],
        )
        self.assertEqual(
            [item["list_read_count"] for item in feed["articles"]],
            [12000, 27000],
        )

    def test_friend_forward_suffix_remains_a_metric_anchor(self) -> None:
        screenshot = Image.new("RGB", (1000, 1600), "white")
        rows = [
            row("FDE彻底火了！500人报名", 250, 610, 620, 640),
            row("阅读5424 赞77 1个朋友转发", 250, 660, 590, 690),
        ]
        detector = WeChatProfileOCR()
        with patch.object(detector, "_rows", return_value=rows):
            feed = detector.inspect_profile_feed(screenshot)

        self.assertEqual([item["title"] for item in feed["articles"]], ["FDE彻底火了！500人报名"])
        self.assertEqual(feed["articles"][0]["list_read_count"], 5424)
        self.assertEqual(feed["articles"][0]["list_like_count"], 77)


if __name__ == "__main__":
    unittest.main()
