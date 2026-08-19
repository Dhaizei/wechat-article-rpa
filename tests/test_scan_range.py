"""日期范围筛选的无桌面依赖回归测试。"""

from __future__ import annotations

import unittest
from datetime import date, datetime

from wechat_visual_rpa import (
    build_card_title_signature,
    is_recent_time_group,
    publish_time_matches_scan_range,
)


class ScanRangeTests(unittest.TestCase):
    def test_today_excludes_yesterday(self) -> None:
        self.assertTrue(is_recent_time_group("08:40", "today"))
        self.assertTrue(is_recent_time_group("今天 08:40", "today"))
        self.assertFalse(is_recent_time_group("昨天", "today"))

    def test_yesterday_excludes_today(self) -> None:
        self.assertTrue(is_recent_time_group("昨天", "yesterday"))
        self.assertFalse(is_recent_time_group("08:40", "yesterday"))
        self.assertFalse(is_recent_time_group("今天", "yesterday"))

    def test_both_range_and_older_boundaries(self) -> None:
        self.assertTrue(is_recent_time_group("昨天", "today_yesterday"))
        self.assertTrue(is_recent_time_group("08:40", "today_yesterday"))
        self.assertFalse(is_recent_time_group("7月20日", "today_yesterday"))
        self.assertFalse(is_recent_time_group("星期三", "today_yesterday"))

    def test_real_publish_time_is_rechecked_against_beijing_day(self) -> None:
        today = date(2026, 8, 5)
        self.assertTrue(
            publish_time_matches_scan_range("2026-08-05 10:08", "today", reference_date=today)
        )
        self.assertFalse(
            publish_time_matches_scan_range("2026-08-03 14:09", "today", reference_date=today)
        )
        self.assertTrue(
            publish_time_matches_scan_range(
                datetime(2026, 8, 4, 23, 59), "today_yesterday", reference_date=today
            )
        )

    def test_title_signature_does_not_require_interaction_counts(self) -> None:
        signature = build_card_title_signature(
            "今天",
            {"title": "同一篇文章", "list_read_count": None, "list_like_count": None},
        )
        self.assertEqual(signature, ("今天", "同一篇文章"))

    def test_title_signature_ignores_ellipsis_variants(self) -> None:
        first = build_card_title_signature("昨天", {"title": "同一篇较长文章.."})
        second = build_card_title_signature("昨天", {"title": "同一篇较长文章..."})
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
