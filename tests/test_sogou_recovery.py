"""搜一搜启动恢复链路的离线测试，不操作真实微信窗口。"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from PIL import Image

import wechat_visual_rpa as rpa


class SogouRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.main_rect = rpa.Rect(0, 0, 1600, 1000)
        self.recovered = rpa.WindowInfo(
            22,
            "搜一搜 - 搜一搜",
            "Chrome_WidgetWin_0",
            rpa.Rect(0, 0, 1200, 900),
            "weixinappex.exe",
        )
        self.image = Image.new("RGB", (1600, 1000), "white")

    def common_patches(self):
        return (
            patch.object(rpa, "find_wechat_manager_window", return_value=(11, self.main_rect)),
            patch.object(rpa, "window_process_name", return_value="weixin.exe"),
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "press_ctrl_f"),
            patch.object(rpa, "set_clipboard_text"),
            patch.object(rpa, "press_ctrl_a"),
            patch.object(rpa, "press_ctrl_v"),
            patch.object(rpa, "press_enter"),
            patch.object(rpa, "capture_window", return_value=self.image),
            patch.object(rpa, "click"),
            patch.object(rpa, "log_event"),
            patch.object(rpa.time, "sleep"),
        )

    def test_ctrl_f_enter_is_used_before_any_visual_detector(self) -> None:
        with self.common_patches()[0] as _manager, self.common_patches()[1], self.common_patches()[2], \
            self.common_patches()[3] as ctrl_f, self.common_patches()[4], self.common_patches()[5], \
            self.common_patches()[6], self.common_patches()[7] as enter, self.common_patches()[8], \
            self.common_patches()[9], self.common_patches()[10], self.common_patches()[11], \
            patch.object(rpa, "find_sogou_search_window", return_value=self.recovered), \
            patch.object(rpa.PROFILE_OCR, "locate_wechat_search_entry") as local_detector:
            result = rpa.open_sogou_from_wechat_main("控制台预检")

        self.assertEqual(result.hwnd, self.recovered.hwnd)
        ctrl_f.assert_called_once()
        enter.assert_called_once()
        local_detector.assert_not_called()

    def test_local_ocr_is_used_after_keyboard_does_not_open_window(self) -> None:
        client = Mock()
        local_action = {
            "found": True,
            "center_x_1000": 180,
            "center_y_1000": 170,
            "confidence": 0.96,
        }
        patches = self.common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
            patches[6], patches[7], patches[8], patches[9] as click, patches[10], patches[11], \
            patch.object(rpa.time, "time", side_effect=[0, 19, 20, 21]), \
            patch.object(rpa, "find_sogou_search_window", return_value=self.recovered), \
            patch.object(rpa.PROFILE_OCR, "locate_wechat_search_entry", return_value=local_action):
            result = rpa.open_sogou_from_wechat_main("控制台预检", client=client)

        self.assertEqual(result.hwnd, self.recovered.hwnd)
        click.assert_called_once()
        client.detect_wechat_search_entry.assert_not_called()

    def test_qwen_vl_is_only_used_after_keyboard_and_local_ocr_fail(self) -> None:
        client = Mock()
        client.detect_wechat_search_entry.return_value = {
            "found": True,
            "label": "搜索网络结果",
            "center_x_1000": 210,
            "center_y_1000": 190,
            "confidence": 0.94,
        }
        patches = self.common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
            patches[6], patches[7], patches[8], patches[9] as click, patches[10], patches[11], \
            patch.object(rpa.time, "time", side_effect=[0, 19, 20, 21]), \
            patch.object(rpa, "find_sogou_search_window", return_value=self.recovered), \
            patch.object(
                rpa.PROFILE_OCR,
                "locate_wechat_search_entry",
                return_value={"found": False, "reason": "本地未识别"},
            ):
            result = rpa.open_sogou_from_wechat_main("控制台预检", client=client)

        self.assertEqual(result.hwnd, self.recovered.hwnd)
        client.detect_wechat_search_entry.assert_called_once()
        click.assert_called_once()

    def test_all_recovery_paths_fail_without_claiming_success(self) -> None:
        client = Mock()
        client.detect_wechat_search_entry.return_value = {
            "found": True,
            "label": "搜索网络结果",
            "center_x_1000": 210,
            "center_y_1000": 190,
            "confidence": 0.94,
        }
        patches = self.common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
            patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], \
            patch.object(rpa.time, "time", side_effect=[0, 19, 20, 43]), \
            patch.object(rpa, "find_sogou_search_window") as find_search, \
            patch.object(
                rpa.PROFILE_OCR,
                "locate_wechat_search_entry",
                return_value={"found": False, "reason": "本地未识别"},
            ):
            with self.assertRaisesRegex(RuntimeError, "Qwen-VL 点击后未出现"):
                rpa.open_sogou_from_wechat_main("控制台预检", client=client)

        find_search.assert_not_called()

    def test_main_weixin_process_is_not_a_legacy_search_window(self) -> None:
        main = rpa.WindowInfo(
            11,
            "微信",
            "Chrome_WidgetWin_0",
            self.main_rect,
            "weixin.exe",
        )
        embedded = rpa.WindowInfo(
            22,
            "微信",
            "Chrome_WidgetWin_0",
            self.main_rect,
            "weixinappex.exe",
        )

        self.assertFalse(rpa.is_sogou_search_window(main))
        self.assertTrue(rpa.is_sogou_search_window(embedded))

    def test_qwen_cannot_click_plain_query_suggestion(self) -> None:
        """模型只看到“搜一搜”查询词时不得误点聊天或历史记录。"""
        action = rpa.normalize_qwen_wechat_search_entry(
            {
                "found": True,
                "label": "搜一搜",
                "center_x_1000": 180,
                "center_y_1000": 90,
                "confidence": 0.99,
            }
        )

        self.assertFalse(action["found"])

    def test_local_ocr_targets_network_search_row_not_plain_query(self) -> None:
        rows = [
            {
                "text": "搜一搜",
                "normalized": "搜一搜",
                "confidence": 0.99,
                "center_x": 140,
                "center_y": 120,
            },
            {
                "text": "搜索网络结果",
                "normalized": "搜索网络结果",
                "confidence": 0.96,
                "center_x": 170,
                "center_y": 80,
            },
        ]
        with patch.object(rpa.PROFILE_OCR, "_rows", return_value=rows):
            action = rpa.PROFILE_OCR.locate_wechat_search_entry(self.image)

        self.assertTrue(action["found"])
        self.assertEqual(action["text"], "搜索网络结果")
        self.assertEqual(action["method"], "rapidocr-wechat-network-search-entry")


if __name__ == "__main__":
    unittest.main()

