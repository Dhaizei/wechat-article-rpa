"""文章管理只读查询的回归测试，不连接真实 MongoDB。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId

import rpa_control_panel as panel


class FakeArticleCollection:
    def __init__(self, documents: list[dict]) -> None:
        self.documents = documents
        self.match = None
        self.pipeline = None

    def count_documents(self, match: dict) -> int:
        self.match = match
        return len(self.documents)

    def aggregate(self, pipeline: list[dict]):
        self.pipeline = pipeline
        if pipeline[-1] == {"$count": "total"}:
            return iter([{"total": len(self.documents)}])
        return iter(self.documents)

    def find_one(self, query: dict):
        target = query["_id"]
        return next((item for item in self.documents if item["_id"] == target), None)


class FakeTargetCollection:
    """日报测试用的公众号分类数据，不连接真实 MongoDB。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def find(self, _query: dict, _projection: dict):
        return iter(self.rows)


class FakeDailyReportCollection:
    """日报归档测试替身，确认团队阅读页只读取已有日报。"""

    def __init__(self, document: dict | None = None, documents: list[dict] | None = None) -> None:
        self.documents = documents if documents is not None else ([document] if document else [])
        self.query = None
        self.sort = None

    def find_one(self, query: dict, sort: list[tuple[str, int]] | None = None):
        self.query = query
        self.sort = sort
        return next((item for item in self.documents if item.get("reportDate") == query.get("reportDate")), None)

    def find(self, _query: dict, _projection: dict):
        return iter(self.documents)


def sample_article() -> dict:
    return {
        "_id": ObjectId(),
        "account": {"name": "量子位"},
        "article": {
            "title": "测试文章标题",
            "publishDate": datetime(2026, 8, 2, 8, 30),
            "url": "https://mp.weixin.qq.com/s/test",
            "content": {"text": "这是一段正文。"},
        },
        "latestInteraction": {"shareCount": 321, "recognitionMethod": "template-ocr-share-only"},
        "interactionHistory": [{"shareCount": 321}],
        "lastUpdatedAt": datetime(2026, 8, 2, 9, 0),
    }


class ArticleManagementTests(unittest.TestCase):
    def test_list_articles_builds_read_only_filters_and_cards(self) -> None:
        collection = FakeArticleCollection([sample_article()])
        result = panel.list_articles(
            date_filter="today", account="量子", query="标题", sort="share_desc", minimum_share=200, collection=collection
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "测试文章标题")
        self.assertEqual(result["items"][0]["share_count"], 321)
        assert collection.pipeline is not None
        self.assertIn("article.publishDate", collection.pipeline[0]["$match"])
        self.assertIn("account.name", collection.pipeline[0]["$match"])
        self.assertEqual(collection.pipeline[2], {"$match": {"latestInteraction.shareCount": {"$gte": 200}}})
        self.assertEqual(collection.pipeline[3]["$sort"], {"latestInteraction.shareCount": -1, "_id": -1})

    def test_article_detail_returns_text_only_when_opened(self) -> None:
        document = sample_article()
        collection = FakeArticleCollection([document])
        item = panel.get_article_detail(str(document["_id"]), collection)

        assert item is not None
        self.assertEqual(item["content"], "这是一段正文。")
        self.assertEqual(item["interaction"]["shareCount"], 321)

    def test_article_export_includes_content_and_supports_multiple_accounts(self) -> None:
        """导出不能只带列表摘要，且多个明确账号必须转换成 $in 条件。"""
        document = sample_article()
        document["latestInteraction"] = {
            "readCount": 1000,
            "likeCount": 21,
            "shareCount": 321,
            "favoriteCount": 8,
            "commentCount": 3,
            "recognitionMethod": "template-ocr-share-only",
        }
        collection = FakeArticleCollection([document])

        rows = panel.article_export_rows(
            date_filter="all", account="量子位，机器之心", collection=collection
        )

        self.assertEqual(rows[0]["content"], "这是一段正文。")
        self.assertEqual(rows[0]["share_count"], 321)
        self.assertEqual(collection.pipeline[0]["$match"]["account.name"], {"$in": ["量子位", "机器之心"]})
        csv_content = panel.serialize_article_export_csv(rows)
        self.assertTrue(csv_content.startswith("\ufeff"))
        self.assertIn("纯文本正文", csv_content)

    def test_invalid_article_filter_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            panel.article_date_window("last_week")
        with self.assertRaises(ValueError):
            panel.article_date_window("2026-13-40")
        with self.assertRaises(ValueError):
            panel.list_articles(minimum_share=-1, collection=FakeArticleCollection([]))

    def test_specific_publish_date_uses_one_local_day(self) -> None:
        start, end = panel.article_date_window("2026-08-01")
        self.assertEqual(start, datetime(2026, 8, 1))
        self.assertEqual(end, datetime(2026, 8, 2))

    def test_today_uses_beijing_calendar_day(self) -> None:
        # UTC 的 17:00 已经是北京时间次日，不能误判成部署机器的本地日期。
        utc_evening = datetime(2026, 8, 1, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(panel.beijing_today(utc_evening).isoformat(), "2026-08-02")

    def test_detail_datetime_is_json_serializable(self) -> None:
        """详情中的互动采集时间不能再让 HTTP 接口中断。"""
        payload = {
            "article_id": ObjectId("6a6e8c8e3a805995e79f7d3f"),
            "interaction": {"collectedAt": datetime(2026, 8, 2, 8, 10)},
        }
        encoded = json.dumps(payload, ensure_ascii=False, default=panel.json_serialization_default)
        self.assertEqual(
            json.loads(encoded),
            {"article_id": "6a6e8c8e3a805995e79f7d3f", "interaction": {"collectedAt": "2026-08-02 08:10"}},
        )

    def test_daily_report_uses_account_categories_and_only_reads_needed_fields(self) -> None:
        """团队日报按目标账号分类展示，不应依赖采集任务或向 MongoDB 写入数据。"""
        first = sample_article()
        first["latestInteraction"] = {"shareCount": 101}
        second = sample_article()
        second["account"] = {"name": "机器之心"}
        second["article"]["title"] = "第二篇文章"
        second["article"]["publishDate"] = datetime(2026, 8, 2, 9, 0)
        second["latestInteraction"] = {"shareCount": 220}
        recruitment = sample_article()
        recruitment["article"]["title"] = "量子位编辑作者招聘"
        recruitment["latestInteraction"] = {"shareCount": 1}
        articles = FakeArticleCollection([first, second, recruitment])
        targets = FakeTargetCollection([
            {"name": "量子位", "category": "AI"},
            {"name": "机器之心", "category": "技术"},
        ])
        report = panel.daily_report(
            report_date="2026-08-02",
            category="AI",
            article_collection=articles,
            target_collection=targets,
        )

        self.assertEqual(report["summary"]["article_count"], 2)
        self.assertEqual(report["summary"]["visible_count"], 1)
        self.assertEqual(report["summary"]["excluded_count"], 1)
        self.assertEqual(report["lead"]["account_name"], "量子位")
        self.assertEqual(report["categories"][1], {"key": "AI", "label": "AI", "count": 1})
        self.assertEqual(report["feed_items"][0]["share_count"], 101)
        assert articles.pipeline is not None
        self.assertEqual(
            articles.pipeline[0],
            {"$match": {"article.publishDate": {"$gte": datetime(2026, 8, 2), "$lt": datetime(2026, 8, 3)}}},
        )

    def test_daily_report_feed_orders_by_share_then_time(self) -> None:
        """团队文章流按转发量排序，避免热点与正文列表排序口径不一致。"""
        higher_share = sample_article()
        higher_share["latestInteraction"] = {"shareCount": 300}
        lower_share = sample_article()
        lower_share["article"]["title"] = "较低转发文章"
        lower_share["article"]["publishDate"] = datetime(2026, 8, 2, 10, 0)
        lower_share["latestInteraction"] = {"shareCount": 80}
        report = panel.daily_report(
            report_date="2026-08-02",
            article_collection=FakeArticleCollection([lower_share, higher_share]),
            target_collection=FakeTargetCollection([]),
        )
        self.assertEqual([item["share_count"] for item in report["feed_items"]], [300, 80])

    def test_daily_report_can_scope_team_view_to_one_account(self) -> None:
        """公众号目录跳转只读取指定账号，不能退回到控制台文章页。"""
        article = sample_article()
        article["account"] = {"name": "Account A"}
        articles = FakeArticleCollection([article])
        targets = FakeTargetCollection([{"name": "Account A", "category": "AI"}])

        report = panel.daily_report(
            report_date="2026-08-02",
            account="Account A",
            article_collection=articles,
            target_collection=targets,
        )

        self.assertEqual(report["selected_account"], "Account A")
        assert articles.pipeline is not None
        self.assertEqual(articles.pipeline[0]["$match"]["account.name"], "Account A")

    def test_team_paths_do_not_require_control_auth(self) -> None:
        """日报与目录可公开阅读，控制台、任务和导出接口必须进入管理员区。"""
        self.assertFalse(panel.requires_control_auth("/briefing.html"))
        self.assertFalse(panel.requires_control_auth("/api/daily-report"))
        self.assertFalse(panel.requires_control_auth("/api/articles"))
        self.assertTrue(panel.requires_control_auth("/"))
        self.assertTrue(panel.requires_control_auth("/accounts.html"))
        self.assertTrue(panel.requires_control_auth("/api/articles/export"))

    def test_daily_briefing_distinguishes_issue_date_from_real_time_range(self) -> None:
        """编辑日报必须显示归档记录的真实汇总范围，不能用“昨天”推测。"""
        reports = FakeDailyReportCollection(
            documents=[
                {
                    "reportDate": "2026-08-02",
                    "generatedAt": datetime(2026, 8, 2, 9, 57),
                    "timeRange": {"start": datetime(2026, 8, 1), "end": datetime(2026, 8, 2, 9, 57)},
                    "reportContent": "【今日要闻】\n编辑后的日报正文。",
                    "articleCount": 2,
                    "sendStatus": "sent",
                    "articles": [
                        {"title": "热门文章", "accountName": "量子位", "category": "AI", "shareCount": 20},
                        {"title": "技术文章", "accountName": "机器之心", "category": "技术", "shareCount": 10},
                    ],
                },
                {
                    "reportDate": "2026-08-01",
                    "generatedAt": datetime(2026, 8, 1, 8, 0),
                    "timeRange": {"start": datetime(2026, 7, 31), "end": datetime(2026, 8, 1, 8, 0)},
                    "reportContent": "历史日报正文。",
                },
            ]
        )

        briefing = panel.daily_briefing(issue_date="2026-08-02", report_collection=reports)

        self.assertTrue(briefing["available"])
        self.assertEqual(briefing["issue_label"], "2026年8月2日早报")
        self.assertEqual(briefing["coverage"]["start"], "2026-08-01 00:00")
        self.assertIn("2026-08-02 09:57", briefing["coverage"]["label"])
        self.assertEqual(briefing["account_count"], 2)
        self.assertEqual(briefing["highlights"][0]["title"], "热门文章")
        self.assertEqual(briefing["archive"][0]["issue_date"], "2026-08-02")

    def test_daily_briefing_accepts_mixed_generated_time_types(self) -> None:
        """历史日报混用 ISO 字符串与 datetime 时，仍应选择同日最新版本。"""
        reports = FakeDailyReportCollection(
            documents=[
                {
                    "reportDate": "2026-08-02",
                    "generatedAt": datetime(2026, 8, 2, 9, 0),
                    "reportContent": "旧版本日报",
                },
                {
                    "reportDate": "2026-08-02",
                    "generatedAt": "2026-08-02T10:00:00+08:00",
                    "reportContent": "最新版本日报",
                },
            ]
        )

        briefing = panel.daily_briefing(issue_date="2026-08-02", report_collection=reports)

        self.assertTrue(briefing["available"])
        self.assertEqual(briefing["content"], "最新版本日报")

    def test_daily_page_cancels_stale_date_requests_and_keeps_date_in_url(self) -> None:
        """日期连点时仅保留最新请求，并把选中的日期保留在可分享的页面地址中。"""
        daily_script = Path(__file__).resolve().parents[1] / "web" / "daily.js"
        content = daily_script.read_text(encoding="utf-8")

        self.assertIn("activeRequest: null", content)
        self.assertIn("new AbortController()", content)
        self.assertIn("state.activeRequest.abort()", content)
        self.assertIn('params.set("date", elements.date.value)', content)
        self.assertIn("window.history.replaceState", content)
        self.assertIn('`${data.date_label} 文章`', content)
        self.assertIn('`${data.date_label} · ${accountName} 的文章`', content)


    def test_briefing_archive_keeps_heading_visible_while_scrolling(self) -> None:
        """期次较多时，仅归档列表滚动，避免标题随列表一起被挤出可视区域。"""
        briefing_css = Path(__file__).resolve().parents[1] / "web" / "briefing.css"
        content = briefing_css.read_text(encoding="utf-8")

        self.assertIn("@media (min-width: 1101px)", content)
        self.assertIn(".issue-archive {\n    display: flex;", content)
        self.assertIn("overflow-y: auto;\n    overflow-x: hidden;\n    overscroll-behavior: contain;", content)

    def test_briefing_exposes_full_archive_and_expands_editorial_report(self) -> None:
        """团队日报应返回全部期次，并且首次打开就展示完整编辑报告。"""
        reports = FakeDailyReportCollection(
            documents=[
                {
                    "reportDate": f"2026-07-{day:02d}",
                    "generatedAt": datetime(2026, 7, day, 9, 57),
                    "reportContent": "历史日报正文。",
                }
                for day in range(1, 15)
            ]
        )
        briefing = panel.daily_briefing(issue_date="2026-07-14", report_collection=reports)
        briefing_script = Path(__file__).resolve().parents[1] / "web" / "briefing.js"
        briefing_html = Path(__file__).resolve().parents[1] / "web" / "briefing.html"

        self.assertEqual(len(briefing["archive"]), 14)
        self.assertIn("details.open = true", briefing_script.read_text(encoding="utf-8"))
        briefing_markup = briefing_html.read_text(encoding="utf-8")
        self.assertIn("archiveCount", briefing_markup)
        self.assertIn('id="archiveTitle"', briefing_markup)
        self.assertIn("日报档案", briefing_markup)
        script_text = briefing_script.read_text(encoding="utf-8")
        self.assertIn("const grouped = new Map()", script_text)
        self.assertIn('className = "archive-month"', script_text)
        self.assertIn("function buildExecutiveSources(items)", script_text)
        self.assertIn('href = "#briefingFullReport"', script_text)


if __name__ == "__main__":
    unittest.main()
