"""公众号管理只读查询的回归测试，不连接真实 MongoDB。"""

from __future__ import annotations

import unittest
from datetime import datetime

import rpa_control_panel as panel


class FakeCursor:
    def __init__(self, documents: list[dict]) -> None:
        self.documents = documents

    def sort(self, _field: str, _direction: int):
        return self

    def skip(self, _offset: int):
        return self

    def limit(self, _limit: int):
        return self

    def __iter__(self):
        return iter(self.documents)


class FakeTargetCollection:
    def __init__(self, documents: list[dict]) -> None:
        self.documents = documents

    def find(self, _match: dict, _projection: dict):
        return FakeCursor(self.documents)

    def count_documents(self, _match: dict) -> int:
        return len(self.documents)


class FakeArticleCollection:
    def aggregate(self, _pipeline: list[dict]):
        return iter([{"_id": "量子位", "article_count": 12, "latest_publish": datetime(2026, 8, 2, 9, 30)}])

    def count_documents(self, _match: dict) -> int:
        return 0


class WritableFakeTargetCollection(FakeTargetCollection):
    """模拟配置集合的最小写入接口，保证测试不会连接真实 MongoDB。"""

    def update_one(self, query: dict, update: dict) -> None:
        for document in self.documents:
            if document.get("_id") == query.get("_id"):
                document.update(update["$set"])
                return
        raise AssertionError("测试账号不存在")

    def insert_one(self, document: dict):
        document = {"_id": f"new-{len(self.documents) + 1}", **document}
        self.documents.append(document)

        class Result:
            inserted_id = document["_id"]

        return Result()


class AccountManagementTests(unittest.TestCase):
    def test_account_list_combines_target_and_article_coverage(self) -> None:
        result = panel.list_accounts(
            target_collection=FakeTargetCollection([{"_id": "target-1", "name": "量子位", "id": "qbitai", "category": "AI", "type": "公众号"}]),
            article_collection=FakeArticleCollection(),
            aliases={"量子位": "量子位公众号"},
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["alias_count"], 1)
        self.assertEqual(result["items"][0]["search_name"], "量子位公众号")
        self.assertTrue(result["items"][0]["alias_configured"])
        self.assertEqual(result["items"][0]["article_count"], 12)
        self.assertEqual(result["items"][0]["latest_publish"], "2026-08-02 09:30")
        self.assertEqual(result["items"][0]["coverage_status"], "covered")
        self.assertEqual(result["summary"], {"total": 1, "covered": 1, "missing": 0, "alias": 1})

    def test_account_list_can_filter_missing_coverage(self) -> None:
        result = panel.list_accounts(
            status="missing",
            target_collection=FakeTargetCollection([
                {"_id": "target-1", "name": "量子位"},
                {"_id": "target-2", "name": "未入库账号"},
            ]),
            article_collection=FakeArticleCollection(),
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["name"], "未入库账号")

    def test_account_list_can_filter_category_and_returns_categories(self) -> None:
        result = panel.list_accounts(
            category="游戏",
            target_collection=FakeTargetCollection([
                {"_id": "target-1", "name": "量子位", "category": "AI"},
                {"_id": "target-2", "name": "游戏葡萄", "category": "游戏"},
            ]),
            article_collection=FakeArticleCollection(),
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["name"], "游戏葡萄")
        self.assertEqual(result["categories"], ["AI", "游戏"])

    def test_account_list_rejects_invalid_page_size(self) -> None:
        with self.assertRaises(ValueError):
            panel.list_accounts(limit=0, target_collection=FakeTargetCollection([]), article_collection=FakeArticleCollection())

    def test_export_rows_include_alias_and_csv_chinese_headers(self) -> None:
        rows = panel.account_export_rows(
            target_collection=FakeTargetCollection([{"_id": "target-1", "name": "量子位", "id": "qbitai", "category": "AI", "type": "公众号"}]),
            aliases={"量子位": "量子位公众号"},
        )
        csv_content = panel.serialize_account_export_csv(rows)

        self.assertEqual(rows[0]["search_name"], "量子位公众号")
        self.assertTrue(csv_content.startswith("\ufeff记录ID,公众号名称,原始ID"))
        self.assertIn("量子位公众号", csv_content)

    def test_import_preview_rejects_duplicate_and_existing_name_without_record_id(self) -> None:
        result = panel.preview_account_import(
            [
                {"公众号名称": "量子位"},
                {"公众号名称": "新账号", "采集搜索名": "新账号搜索"},
                {"公众号名称": "新账号"},
            ],
            target_collection=FakeTargetCollection([{"_id": "target-1", "name": "量子位"}]),
            article_collection=FakeArticleCollection(),
        )

        self.assertEqual(result["summary"], {"total": 3, "create": 1, "update": 0, "error": 2})
        self.assertIn("记录ID", result["items"][0]["message"])
        self.assertIn("重复", result["items"][2]["message"])

    def test_upsert_updates_config_and_keeps_alias_out_of_mongo_document(self) -> None:
        target = WritableFakeTargetCollection([{"_id": "target-1", "name": "量子位", "id": "old", "category": "旧分类", "type": "公众号"}])
        aliases = {"量子位": "旧搜索名"}
        result = panel.upsert_account_config(
            {"record_id": "target-1", "name": "量子位", "source_id": "qbitai", "category": "AI", "account_type": "微信公众号", "search_name": "量子位公众号"},
            target_collection=target,
            article_collection=FakeArticleCollection(),
            aliases=aliases,
            persist_aliases=False,
        )

        self.assertEqual(result["action"], "updated")
        self.assertEqual(target.documents[0]["id"], "qbitai")
        self.assertEqual(target.documents[0]["category"], "AI")
        self.assertNotIn("search_name", target.documents[0])
        self.assertEqual(aliases, {"量子位": "量子位公众号"})


if __name__ == "__main__":
    unittest.main()
