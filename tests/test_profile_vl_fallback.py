"""“搜一搜 → 资料页”默认采集链路的 Qwen-VL 兼容回归测试。

测试不连接真实微信、Qwen-VL 或 MongoDB，只验证模型具备明确的触发条件和不可通过的安全边界。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import wechat_visual_rpa as rpa


class ProfileVLFallbackTests(unittest.TestCase):
    def test_profile_header_accepts_exact_qwen_name_when_matched_flag_is_false(self) -> None:
        """兼容网关误写 matched 时，精确名称和高置信度仍可安全确认。"""
        self.assertTrue(
            rpa._qwen_profile_header_confirmed(
                {"matched": False, "name": "书生Intern", "confidence": 0.98},
                "书生Intern",
            )
        )

    def test_profile_header_rejects_wrong_name_or_low_confidence(self) -> None:
        """模型不得凭 matched=true 接受其他公众号，也不得接受低置信结果。"""
        self.assertFalse(
            rpa._qwen_profile_header_confirmed(
                {"matched": True, "name": "雅书生Intern", "confidence": 0.99},
                "书生Intern",
            )
        )
        self.assertFalse(
            rpa._qwen_profile_header_confirmed(
                {"matched": True, "name": "书生Intern", "confidence": 0.52},
                "书生Intern",
            )
        )

    def test_qwen_search_target_requires_exact_account_name_and_valid_coordinates(self) -> None:
        """Qwen 只能在精确名称和合法坐标同时存在时返回可点击卡片。"""
        client = Mock()
        client.detect_search_account.return_value = {
            "found": True,
            "name": "ComfyUi中文",
            "center_x_1000": 428,
            "center_y_1000": 304,
            "avatar_x_1000": 118,
            "avatar_y_1000": 304,
            "confidence": 0.98,
        }

        target = rpa._qwen_search_target(
            client, Image.new("RGB", (800, 600), "white"), "ComfyUI中文"
        )

        self.assertTrue(target["found"])
        self.assertTrue(target["is_official_account"])
        self.assertEqual(target["matched_name"], "ComfyUi中文")
        self.assertEqual(target["avatar_x_1000"], 118)

    def test_qwen_search_target_rejects_similar_but_different_account(self) -> None:
        """不能因模型返回相似名称就点击，避免误采同类账号。"""
        client = Mock()
        client.detect_search_account.return_value = {
            "found": True,
            "name": "游戏圈内那些事",
            "center_x_1000": 428,
            "center_y_1000": 304,
        }

        with self.assertRaisesRegex(ValueError, "名称不匹配"):
            rpa._qwen_search_target(
                client, Image.new("RGB", (800, 600), "white"), "游戏那些事Gamez"
            )

    def test_profile_feed_uses_local_ocr_without_calling_qwen(self) -> None:
        """正常页面必须直接使用本地 OCR，不产生额外模型调用。"""
        client = Mock()
        screenshot = Image.new("RGB", (800, 600), "white")
        local_feed = {
            "time_labels": [{"text": "今天 11:35", "center_y_1000": 210}],
            "articles": [{"title": "测试文章", "center_x_1000": 500, "center_y_1000": 420}],
            "recognition_method": "rapidocr-profile-feed",
        }
        window = rpa.WindowInfo(1, "公众号", "test", rpa.Rect(100, 80, 900, 680))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa.PROFILE_OCR, "inspect_profile_feed", return_value=local_feed),
        ):
            feed = rpa.analyze_profile_window(window, Path(directory), client=client)

        self.assertEqual(feed["recognition_method"], "rapidocr-profile-feed")
        self.assertEqual(feed["articles"][0]["screen_point"], (500, 332))
        client.inspect_profile_feed.assert_not_called()

    def test_profile_feed_calls_qwen_once_after_two_local_failures(self) -> None:
        """本地连续两次缺少分组或卡片后，才调用一次 Qwen-VL 复核。"""
        client = Mock()
        client.inspect_profile_feed.return_value = {
            "time_labels": [{"text": "昨天 18:20", "center_y_1000": 180}],
            "articles": [{"title": "Qwen 复核文章", "center_x_1000": 510, "center_y_1000": 410}],
        }
        screenshot = Image.new("RGB", (800, 600), "white")
        window = rpa.WindowInfo(1, "公众号", "test", rpa.Rect(100, 80, 900, 680))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa, "time") as mocked_time,
            patch.object(rpa.PROFILE_OCR, "inspect_profile_feed", return_value={"time_labels": [], "articles": []}) as local_ocr,
        ):
            feed = rpa.analyze_profile_window(window, Path(directory), client=client)

        self.assertEqual(local_ocr.call_count, 2)
        client.inspect_profile_feed.assert_called_once_with(screenshot)
        self.assertEqual(feed["recognition_method"], "qwen-vl-profile-feed-fallback")
        self.assertIn("本地资料页识别结果不完整", feed["fallback_reason"])
        mocked_time.sleep.assert_called_once_with(0.5)

    def test_profile_feed_local_only_does_not_call_qwen(self) -> None:
        """--local-only 依然严格禁止 Qwen-VL，不能因新增兼容逻辑而穿透。"""
        client = Mock()
        screenshot = Image.new("RGB", (800, 600), "white")
        window = rpa.WindowInfo(1, "公众号", "test", rpa.Rect(100, 80, 900, 680))

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(rpa, "activate_window"),
            patch.object(rpa, "capture_window", return_value=screenshot),
            patch.object(rpa, "time") as mocked_time,
            patch.object(rpa.PROFILE_OCR, "inspect_profile_feed", return_value={"time_labels": [], "articles": []}),
        ):
            with self.assertRaisesRegex(RuntimeError, "已禁用VL"):
                rpa.analyze_profile_window(
                    window, Path(directory), client=client, allow_vl=False
                )

        client.inspect_profile_feed.assert_not_called()
        mocked_time.sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
