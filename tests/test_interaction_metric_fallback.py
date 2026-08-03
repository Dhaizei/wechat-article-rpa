"""互动指标局部降级的离线回归测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

import wechat_visual_rpa as rpa


class InteractionMetricFallbackTests(unittest.TestCase):
    def test_all_metrics_keeps_verified_share_when_auxiliary_icons_fail(self) -> None:
        """收藏/评论模板失效时，不应丢弃已经独立确认的转发数。"""
        screenshot = Image.new("RGB", (1200, 800), "white")
        share_only = {
            "share_count": 106,
            "details": {"share": {"template_confidence": 0.99, "ocr_text": ["106"]}},
        }

        with (
            patch.object(rpa.INTERACTION_OCR, "extract", side_effect=ValueError("收藏图标不稳定")),
            patch.object(rpa.INTERACTION_OCR, "extract_share", return_value=share_only),
        ):
            metrics, source, reason = rpa.extract_local_interaction_metrics(screenshot, "all")

        self.assertEqual(source, "template-ocr-partial-share")
        self.assertEqual(metrics["share_count"], 106)
        self.assertIsNone(metrics["favorite_count"])
        self.assertIsNone(metrics["comment_count"])
        self.assertIn("收藏图标不稳定", reason or "")

    def test_all_metrics_still_fails_when_share_cannot_be_confirmed(self) -> None:
        """转发数本身不可确认时，仍必须失败，不能写入猜测数据。"""
        screenshot = Image.new("RGB", (1200, 800), "white")
        with (
            patch.object(rpa.INTERACTION_OCR, "extract", side_effect=ValueError("完整指标失败")),
            patch.object(rpa.INTERACTION_OCR, "extract_share", side_effect=ValueError("转发图标失败")),
        ):
            with self.assertRaisesRegex(ValueError, "完整指标失败"):
                rpa.extract_local_interaction_metrics(screenshot, "all")

    def test_all_metrics_keeps_vl_fallback_path_when_enabled(self) -> None:
        """允许 VL 时应继续抛出本地异常，让上层补齐全部互动指标。"""
        screenshot = Image.new("RGB", (1200, 800), "white")
        with patch.object(rpa.INTERACTION_OCR, "extract", side_effect=ValueError("完整指标失败")):
            with self.assertRaisesRegex(ValueError, "完整指标失败"):
                rpa.extract_local_interaction_metrics(
                    screenshot, "all", allow_partial=False
                )


if __name__ == "__main__":
    unittest.main()
