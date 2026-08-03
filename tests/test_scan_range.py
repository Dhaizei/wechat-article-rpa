"""日期范围筛选的无桌面依赖回归测试。"""

from __future__ import annotations

import unittest

from wechat_visual_rpa import is_recent_time_group


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


if __name__ == "__main__":
    unittest.main()
