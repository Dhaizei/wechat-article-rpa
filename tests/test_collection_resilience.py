"""采集结果保留与标题匹配的离线回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import wechat_visual_rpa as rpa


class CollectionResilienceTests(unittest.TestCase):
    def test_article_validation_uses_canonical_account_name_after_ocr_suffix(self) -> None:
        """搜索结果的 OCR 后缀不能污染文章页和 MongoDB 的账号归属。"""
        profile_window = rpa.WindowInfo(
            hwnd=0,
            title="微信",
            class_name="Chrome_WidgetWin_0",
            rect=rpa.Rect(0, 0, 100, 100),
        )
        first_page = {
            "time_labels": [{"text": "今天", "center_y_1000": 100}],
            "articles": [
                {
                    "title": "一篇测试文章",
                    "center_y_1000": 200,
                    "center_x_1000": 500,
                    "screen_point": (10, 10),
                    "list_read_count": 10,
                    "list_like_count": 2,
                }
            ],
        }
        older_page = {
            "time_labels": [{"text": "昨天", "center_y_1000": 100}],
            "articles": [],
        }
        captured_expected_accounts: list[str] = []

        def fake_collect_open_article(*args, **kwargs):
            captured_expected_accounts.append(kwargs["expected_account"])
            return {
                "url": "https://mp.weixin.qq.com/s/test",
                "title": kwargs["expected_title"],
                "account_name": kwargs["expected_account"],
                "content": "正文",
                "status": "inserted",
            }

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    rpa,
                    "search_and_open_profile",
                    return_value=(profile_window, "腾讯技术工程 媒体"),
                ),
                patch.object(
                    rpa,
                    "analyze_profile_window",
                    # 资料页会继续翻页到扫描范围结束；后续页面统一模拟为空页。
                    side_effect=[first_page, *([older_page] * 12)],
                ),
                patch.object(rpa, "collect_open_article", side_effect=fake_collect_open_article),
                patch.object(rpa, "activate_window"),
                patch.object(rpa, "click"),
                patch.object(rpa, "close_article_after_attempt"),
                patch.object(rpa, "log_event"),
                patch.object(rpa.time, "sleep"),
            ):
                summary = rpa.collect_profile_account(
                    None,
                    "腾讯技术工程",
                    Path(directory),
                    max_articles=20,
                    export_jsonl=None,
                    export_csv=None,
                    write_mongo=False,
                    scan_range="today",
                )

        self.assertEqual(captured_expected_accounts, ["腾讯技术工程"])
        self.assertEqual(summary["collected"][0]["account_name"], "腾讯技术工程")

    def test_truncated_card_title_allows_small_ocr_errors(self) -> None:
        """卡片省略标题允许 AI/Al、千/干等少量 OCR 误差。"""
        card_title = (
            "Physical Al正进入经验工程时代，Ropedia聚焦全链路数据基建，完成数干万美..."
        )
        article_title = (
            "Physical AI正进入经验工程时代，Ropedia聚焦全链路数据基建，完成数千万美元融资"
        )
        self.assertTrue(rpa.titles_match(card_title, article_title))

    def test_truncated_card_title_rejects_unrelated_article(self) -> None:
        self.assertFalse(
            rpa.titles_match(
                "Physical AI正进入经验工程时代，聚焦全链路数据基建...",
                "腾讯发布全新游戏模型，内容生产效率显著提升",
            )
        )

    def test_last_line_of_multiline_card_title_matches_article_suffix(self) -> None:
        self.assertTrue(
            rpa.titles_match(
                "热门插件被锤了",
                "一个Skill让DeepSeek V4 Pro超越Fable 5？热门插件被锤了",
            )
        )

    def test_partial_account_results_survive_later_fatal_error(self) -> None:
        """账号后续失败时，已成功写出的文章必须保留在批次摘要中。"""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            checkpoint = {
                "account": "量子位",
                "partial": True,
                "collected": [
                    {"title": "已成功文章一", "status": "inserted"},
                    {"title": "已成功文章二", "status": "updated"},
                ],
                "failures": [],
            }
            (output_dir / "partial-summary.json").write_text(
                json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8"
            )

            with patch.object(rpa, "log_event"):
                summary = rpa.recover_partial_account_summary(
                    output_dir,
                    "量子位",
                    "文章标签清理失败",
                    "window",
                )

            self.assertEqual(len(summary["collected"]), 2)
            self.assertEqual(summary["fatal_category"], "window")
            self.assertTrue(summary["partial"])
            persisted = json.loads(
                (output_dir / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(persisted["collected"]), 2)


if __name__ == "__main__":
    unittest.main()
