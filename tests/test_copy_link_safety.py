"""复制文章链接的缓存、本地 OCR 与 Qwen-VL 三层兜底测试。"""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

import wechat_visual_rpa as rpa


class CopyArticleUrlSafetyTests(unittest.TestCase):
    """确保缓存坐标经过验证，任何识别失败都不会回退到盲点坐标。"""

    def setUp(self) -> None:
        self.rect = rpa.Rect(left=100, top=200, right=1100, bottom=1400)
        self.screenshot = Image.new("RGB", (1000, 1200), "white")
        self.local_action = {
            "found": True,
            "center_x_1000": 680,
            "center_y_1000": 120,
            "method": "rapidocr-browser-copy-link",
        }

    def common_patches(self):
        return (
            patch.object(rpa, "set_clipboard_text"),
            patch.object(rpa, "press_escape"),
            patch.object(rpa, "capture_window", return_value=self.screenshot),
            patch.object(rpa, "read_clipboard_text", return_value="https://mp.weixin.qq.com/s/test"),
            patch.object(rpa, "save_copy_link_position_cache"),
            patch.object(rpa, "clear_copy_link_position_cache"),
            patch.object(rpa, "click"),
            patch.object(rpa.time, "sleep"),
            patch.object(rpa, "load_menu_button_position_cache", return_value=None),
            patch.object(rpa, "save_menu_button_position_cache"),
            patch.object(rpa, "clear_menu_button_position_cache"),
        )

    def test_validated_cache_is_used_before_full_ocr(self) -> None:
        """上次坐标附近仍可识别到“复制链接”时，直接走缓存快路径。"""
        cached = {"center_x_1000": 670, "center_y_1000": 118}
        cached_action = {**self.local_action, "method": "cached-position-roi-rapidocr"}
        client = Mock()
        patches = self.common_patches()
        with ExitStack() as stack:
            mocks = [stack.enter_context(item) for item in patches]
            stack.enter_context(patch.object(rpa, "load_copy_link_position_cache", return_value=cached))
            stack.enter_context(
                patch.object(rpa, "validate_cached_copy_link_action", return_value=cached_action)
            )
            full_ocr = stack.enter_context(
                patch.object(rpa.PROFILE_OCR, "locate_copy_link_action")
            )
            url = rpa.copy_article_url(1, self.rect, client=client, allow_vl=True)

        self.assertEqual(url, "https://mp.weixin.qq.com/s/test")
        full_ocr.assert_not_called()
        client.detect_copy_link_action.assert_not_called()
        mocks[4].assert_called_once()
        self.assertEqual(mocks[4].call_args.args[3], "cache-validated")
        mocks[9].assert_called_once()

    def test_invalid_cache_falls_back_to_local_ocr_and_replaces_cache(self) -> None:
        """缓存验证失败后删除旧值，并用完整 OCR 的成功坐标替换。"""
        patches = self.common_patches()
        with ExitStack() as stack:
            mocks = [stack.enter_context(item) for item in patches]
            stack.enter_context(
                patch.object(
                    rpa,
                    "load_copy_link_position_cache",
                    return_value={"center_x_1000": 700},
                )
            )
            stack.enter_context(
                patch.object(
                    rpa,
                    "validate_cached_copy_link_action",
                    return_value={"found": False, "reason": "缓存区域文字不符"},
                )
            )
            stack.enter_context(
                patch.object(rpa.PROFILE_OCR, "locate_copy_link_action", return_value=self.local_action)
            )
            url = rpa.copy_article_url(1, self.rect, client=Mock(), allow_vl=True)

        self.assertEqual(url, "https://mp.weixin.qq.com/s/test")
        mocks[5].assert_called_once()
        mocks[4].assert_called_once()
        self.assertEqual(mocks[4].call_args.args[3], "local-ocr")

    def test_qwen_is_used_only_after_local_ocr_failure(self) -> None:
        """只有没有缓存且本地 OCR 失败时，才允许 Qwen-VL 返回候选坐标。"""
        client = Mock()
        client.detect_copy_link_action.return_value = {
            "found": True,
            "label": "复制链接",
            "center_x_1000": 690,
            "center_y_1000": 125,
            "confidence": 0.96,
        }
        patches = self.common_patches()
        with ExitStack() as stack:
            mocks = [stack.enter_context(item) for item in patches]
            stack.enter_context(patch.object(rpa, "load_copy_link_position_cache", return_value=None))
            stack.enter_context(
                patch.object(
                    rpa.PROFILE_OCR,
                    "locate_copy_link_action",
                    return_value={"found": False, "reason": "本地未识别"},
                )
            )
            url = rpa.copy_article_url(1, self.rect, client=client, allow_vl=True)

        self.assertEqual(url, "https://mp.weixin.qq.com/s/test")
        client.detect_copy_link_action.assert_called_once_with(self.screenshot)
        self.assertEqual(mocks[4].call_args.args[3], "qwen-vl")

    def test_missing_all_detectors_never_clicks_a_guessed_menu_item(self) -> None:
        """缓存、本地 OCR 和 VL 都失败时，只允许点击打开菜单本身。"""
        client = Mock()
        client.detect_copy_link_action.return_value = {
            "found": False,
            "label": None,
            "confidence": 0,
        }
        patches = self.common_patches()
        with ExitStack() as stack:
            mocks = [stack.enter_context(item) for item in patches]
            stack.enter_context(patch.object(rpa, "load_copy_link_position_cache", return_value=None))
            stack.enter_context(
                patch.object(
                    rpa.PROFILE_OCR,
                    "locate_copy_link_action",
                    return_value={"found": False, "reason": "本地未识别"},
                )
            )
            with self.assertRaisesRegex(RuntimeError, "缓存坐标、本地 OCR 与 Qwen-VL"):
                rpa.copy_article_url(1, self.rect, client=client, allow_vl=True)

        # 唯一鼠标点击是打开右上角菜单，绝不点击猜测的操作项。
        self.assertEqual(mocks[6].call_count, 1)
        mocks[9].assert_not_called()
        mocks[10].assert_called_once()

    def test_qwen_result_requires_exact_label_and_confidence(self) -> None:
        """Qwen 不能用相似菜单名或低置信度坐标触发鼠标点击。"""
        self.assertFalse(
            rpa.normalize_qwen_copy_link_action(
                {
                    "found": True,
                    "label": "转发链接",
                    "center_x_1000": 600,
                    "center_y_1000": 100,
                    "confidence": 0.99,
                }
            )["found"]
        )
        self.assertFalse(
            rpa.normalize_qwen_copy_link_action(
                {
                    "found": True,
                    "label": "复制链接",
                    "center_x_1000": 600,
                    "center_y_1000": 100,
                    "confidence": 0.5,
                }
            )["found"]
        )

    def test_titlebar_ellipsis_is_located_without_fixed_coordinates(self) -> None:
        """标题栏三点按钮应由当前截图定位，而不是依赖旧电脑绝对坐标。"""
        screenshot = Image.new("RGB", (1000, 600), "white")
        draw = ImageDraw.Draw(screenshot)
        for center_x in (752, 764, 776):
            draw.ellipse((center_x - 3, 27, center_x + 3, 33), fill="black")

        result = rpa.PROFILE_OCR.locate_browser_menu_button(screenshot)

        self.assertTrue(result["found"], result)
        self.assertLess(abs(result["center_x_1000"] - 764), 8)
        self.assertLess(abs(result["center_y_1000"] - 50), 8)

    def test_body_ellipsis_does_not_replace_browser_menu_button(self) -> None:
        """网页内容区的三点按钮不得覆盖浏览器顶部菜单候选。"""
        screenshot = Image.new("RGB", (1000, 1200), "white")
        draw = ImageDraw.Draw(screenshot)
        for center_x in (854, 866, 878):
            draw.ellipse((center_x - 3, 17, center_x + 3, 23), fill="black")
        # 这是曾在远端日志中被误选的网页三点位置（约 y=75）。
        for center_x in (738, 750, 762):
            draw.ellipse((center_x - 3, 72, center_x + 3, 78), fill="black")

        result = rpa.PROFILE_OCR.locate_browser_menu_button(screenshot)

        self.assertTrue(result["found"], result)
        self.assertLess(abs(result["center_x_1000"] - 866), 8)
        self.assertLess(abs(result["center_y_1000"] - 17), 8)

    def test_verified_menu_cache_survives_unrelated_live_ellipsis(self) -> None:
        """实时检测到网页三点时，继续优先尝试已通过 URL 验证的菜单坐标。"""
        cached = {
            "center_x_1000": 866,
            "center_y_1000": 17,
            "confidence": 0.91,
        }
        unrelated = {
            "found": True,
            "center_x_1000": 750,
            "center_y_1000": 65,
            "confidence": 0.88,
        }
        with patch.object(
            rpa.PROFILE_OCR,
            "locate_browser_menu_button",
            return_value=unrelated,
        ):
            result = rpa.validate_cached_menu_button_action(self.screenshot, cached)

        self.assertTrue(result["found"], result)
        self.assertEqual(result["center_x_1000"], 866)
        self.assertEqual(result["center_y_1000"], 17)
        self.assertEqual(result["method"], "cached-menu-button-verified-position")

    def test_qwen_menu_button_requires_titlebar_position_and_confidence(self) -> None:
        """Qwen 返回正文省略号或低置信度坐标时不得触发点击。"""
        self.assertFalse(
            rpa.normalize_qwen_menu_button_action(
                {
                    "found": True,
                    "label": "...",
                    "center_x_1000": 700,
                    "center_y_1000": 500,
                    "confidence": 0.99,
                }
            )["found"]
        )
        self.assertFalse(
            rpa.normalize_qwen_menu_button_action(
                {
                    "found": True,
                    "label": "...",
                    "center_x_1000": 700,
                    "center_y_1000": 50,
                    "confidence": 0.50,
                }
            )["found"]
        )


if __name__ == "__main__":
    unittest.main()
