"""文章管理与导出的只读回归测试，不连接真实 MongoDB。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from bson import ObjectId

import rpa_control_panel as panel


class FakeArticleCollection:
    """记录查询管道并返回固定文章，便于确认接口不会写入数据库。"""

    def __init__(self, documents: list[dict]) -> None:
        self.documents = documents
        self.match: dict | None = None
        self.pipeline: list[dict] | None = None

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
        "latestInteraction": {
            "shareCount": 321,
            "recognitionMethod": "template-ocr-share-only",
        },
        "interactionHistory": [{"shareCount": 321}],
        "lastUpdatedAt": datetime(2026, 8, 2, 9, 0),
    }


class ArticleManagementTests(unittest.TestCase):
    def test_list_articles_builds_read_only_filters_and_cards(self) -> None:
        collection = FakeArticleCollection([sample_article()])
        result = panel.list_articles(
            date_filter="today",
            account="量子",
            query="标题",
            sort="share_desc",
            minimum_share=200,
            collection=collection,
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "测试文章标题")
        self.assertEqual(result["items"][0]["share_count"], 321)
        assert collection.pipeline is not None
        self.assertIn("article.publishDate", collection.pipeline[0]["$match"])
        self.assertIn("account.name", collection.pipeline[0]["$match"])
        self.assertEqual(
            collection.pipeline[2],
            {"$match": {"latestInteraction.shareCount": {"$gte": 200}}},
        )
        self.assertEqual(
            collection.pipeline[3]["$sort"],
            {"latestInteraction.shareCount": -1, "_id": -1},
        )

    def test_article_detail_returns_text_only_when_opened(self) -> None:
        document = sample_article()
        item = panel.get_article_detail(
            str(document["_id"]), FakeArticleCollection([document])
        )

        assert item is not None
        self.assertEqual(item["content"], "这是一段正文。")
        self.assertEqual(item["interaction"]["shareCount"], 321)

    def test_article_export_includes_content_and_multiple_accounts(self) -> None:
        document = sample_article()
        document["latestInteraction"].update(
            {
                "readCount": 1000,
                "likeCount": 21,
                "favoriteCount": 8,
                "commentCount": 3,
            }
        )
        collection = FakeArticleCollection([document])

        rows = panel.article_export_rows(
            date_filter="all",
            account="量子位，机器之心",
            collection=collection,
        )

        self.assertEqual(rows[0]["content"], "这是一段正文。")
        self.assertEqual(rows[0]["share_count"], 321)
        assert collection.pipeline is not None
        self.assertEqual(
            collection.pipeline[0]["$match"]["account.name"],
            {"$in": ["量子位", "机器之心"]},
        )
        csv_content = panel.serialize_article_export_csv(rows)
        self.assertTrue(csv_content.startswith("\ufeff"))
        self.assertIn("纯文本正文", csv_content)

    def test_invalid_article_filters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            panel.article_date_window("last_week")
        with self.assertRaises(ValueError):
            panel.article_date_window("2026-13-40")
        with self.assertRaises(ValueError):
            panel.list_articles(
                minimum_share=-1, collection=FakeArticleCollection([])
            )

    def test_specific_publish_date_uses_one_local_day(self) -> None:
        start, end = panel.article_date_window("2026-08-01")
        self.assertEqual(start, datetime(2026, 8, 1))
        self.assertEqual(end, datetime(2026, 8, 2))

    def test_today_uses_beijing_calendar_day(self) -> None:
        # UTC 17:00 已是北京时间次日，不能依赖部署机器的本地时区。
        utc_evening = datetime(2026, 8, 1, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(
            panel.beijing_today(utc_evening).isoformat(), "2026-08-02"
        )

    def test_detail_datetime_is_json_serializable(self) -> None:
        payload = {
            "article_id": ObjectId("6a6e8c8e3a805995e79f7d3f"),
            "interaction": {"collectedAt": datetime(2026, 8, 2, 8, 10)},
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, default=panel.json_serialization_default
        )
        self.assertEqual(
            json.loads(encoded),
            {
                "article_id": "6a6e8c8e3a805995e79f7d3f",
                "interaction": {"collectedAt": "2026-08-02 08:10"},
            },
        )

    def test_all_management_pages_and_apis_require_auth(self) -> None:
        for path in (
            "/",
            "/accounts.html",
            "/articles.html",
            "/api/articles",
            "/api/articles/export",
            "/api/accounts",
        ):
            self.assertTrue(panel.requires_control_auth(path), path)


if __name__ == "__main__":
    unittest.main()
