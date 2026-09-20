"""只读标签探测的容错回归测试。"""

import subprocess
import unittest
from unittest.mock import patch

from wechat_window_probe import has_search_tab


class WindowProbeTests(unittest.TestCase):
    def test_search_tab_can_be_in_background(self):
        result = subprocess.CompletedProcess([], 0, '["文章标题", "机器之心 - 账号 - 搜一搜"]')
        with patch("wechat_window_probe.subprocess.run", return_value=result):
            self.assertTrue(has_search_tab(123))

    def test_unrelated_tabs_are_rejected(self):
        result = subprocess.CompletedProcess([], 0, '["公众号", "微信"]')
        with patch("wechat_window_probe.subprocess.run", return_value=result):
            self.assertFalse(has_search_tab(123))

    def test_unresponsive_provider_is_bounded(self):
        with patch("wechat_window_probe.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("probe", 5)):
            self.assertFalse(has_search_tab(123))


if __name__ == "__main__":
    unittest.main()

