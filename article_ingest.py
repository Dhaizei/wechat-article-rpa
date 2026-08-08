"""解析公众号文章链接，并导出本地文件或按需写入 MongoDB。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

_INDEX_READY: set[str] = set()


def shanghai_timezone():
    """返回东八区时区；Windows 未安装 IANA 时区库时使用固定 UTC+8 兜底。"""
    try:
        return ZoneInfo("Asia/Shanghai")
    except (KeyError, ZoneInfoNotFoundError):
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


def normalize_article_url(url: str) -> str:
    """规范化公众号文章 URL，避免参数顺序或片段差异造成重复记录。"""
    value = str(url or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def ensure_article_indexes(collection: Any) -> bool:
    """创建幂等索引；历史数据存在重复时降级为普通索引，不阻断采集。"""
    namespace = f"{collection.database.name}.{collection.name}"
    if namespace in _INDEX_READY:
        return True
    try:
        collection.create_index(
            [("article.urlNormalized", 1)],
            name="article_url_normalized_unique",
            unique=True,
            sparse=True,
        )
        _INDEX_READY.add(namespace)
        return True
    except DuplicateKeyError:
        collection.create_index(
            [("article.urlNormalized", 1)],
            name="article_url_normalized_lookup",
            unique=False,
            sparse=True,
        )
        _INDEX_READY.add(namespace)
        return False


def load_cached_page(
    url: str,
    mongo_uri: str,
    database_name: str,
    collection_name: str,
) -> dict[str, str] | None:
    """读取已有文章的完整字段；返回 None 表示仍需访问网页补齐。"""
    normalized_url = normalize_article_url(url)
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        collection = client[database_name][collection_name]
        ensure_article_indexes(collection)
        existing = collection.find_one(
            {"$or": [{"article.urlNormalized": normalized_url}, {"article.url": url}]},
            {"account": 1, "article": 1},
        )
        if not existing:
            return None
        account = existing.get("account") or {}
        article = existing.get("article") or {}
        content = article.get("content") or {}
        page = {
            "title": str(article.get("title") or "").strip(),
            "account_name": str(account.get("name") or "").strip(),
            "publish_time": article.get("publishDate") or "",
            "content": str(content.get("text") or "").strip(),
        }
        return page if all(page.values()) else None
    finally:
        client.close()


def clean_text(element: Any) -> str:
    if element is None:
        return ""
    for tag in element.select("script,style,svg,iframe,video,audio,img"):
        tag.decompose()
    text = element.get_text("\n", strip=True)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_publish_time(html: str, soup: BeautifulSoup) -> str:
    """从微信文章 HTML 中提取发布时间，优先读取明确的页面变量。"""
    publish_node = soup.select_one("#publish_time")
    publish_text = clean_text(publish_node)
    if publish_text:
        return publish_text

    # 微信会通过脚本把 createTime 写入空的 #publish_time 节点。
    text_patterns = (
        r"\bvar\s+createTime\s*=\s*['\"](\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})['\"]",
        r"\bcreate_time\s*:\s*['\"](\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})['\"]",
    )
    for pattern in text_patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)

    # 部分页面只保留秒级时间戳；按上海时区格式化为页面展示时间。
    timestamp_patterns = (
        r"\bvar\s+create_time\s*=\s*['\"]?(\d{10})['\"]?\s*\*?\s*1",
        r"\bori_create_time\s*:\s*['\"]?(\d{10})['\"]?",
        r"\bvar\s+oriCreateTime\s*=\s*['\"]?(\d{10})['\"]?",
    )
    for pattern in timestamp_patterns:
        match = re.search(pattern, html)
        if match:
            value = datetime.fromtimestamp(int(match.group(1)), shanghai_timezone())
            # 数据库沿用项目原有约定：publishDate 保存页面显示的北京时间，
            # 不再转换成 UTC，便于按公众号发布时间直接筛选。
            return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M")
    return ""


def parse_page(url: str) -> dict[str, str]:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    # 微信页面实际使用 UTF-8，但响应头有时缺少 charset，requests 会误判为 ISO-8859-1。
    response.encoding = "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    title_node = soup.select_one("#activity-name")
    content_node = soup.select_one("#js_content")
    account_node = soup.select_one("#js_name") or soup.select_one(".profile_nickname")
    title = clean_text(title_node)
    content = clean_text(content_node)
    publish_time = extract_publish_time(response.text, soup)

    # 页面脚本通常保留公众号昵称，作为 DOM 缺失时的后备来源。
    account_name = clean_text(account_node)
    if not account_name:
        match = re.search(r'var\s+nickname\s*=\s*htmlDecode\("(.*?)"\)', response.text)
        account_name = match.group(1) if match else ""

    if not title:
        raise ValueError("文章页面没有标题")
    if not content:
        raise ValueError("文章正文为空，拒绝写入 MongoDB")
    return {
        "title": title,
        "account_name": account_name,
        "publish_time": publish_time,
        "content": content,
    }


def parse_publish_time(value: str | None) -> datetime:
    if not value:
        raise ValueError("缺少文章发布时间")
    normalized = value.strip().replace("年", "-").replace("月", "-").replace("日", "")
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            local_value = datetime.strptime(normalized, pattern)
            # 页面显示的是北京时间，按项目现有数据约定保留这个墙上时间，
            # 不做 UTC-8 转换，确保 MongoDB 中看到的时间与公众号一致。
            return local_value
        except ValueError:
            continue
    raise ValueError(f"无法解析发布时间：{value}")


def stable_account_id(account_name: str) -> str:
    digest = hashlib.sha1(account_name.encode("utf-8")).hexdigest()[:16].upper()
    return f"RPA_{digest}"


def build_interaction(metrics: dict[str, Any], collected_at: datetime) -> dict[str, Any]:
    interaction = {
        "recognitionMethod": metrics.get("metric_source"),
        "collectedAt": collected_at,
        "source": "wechat-desktop-rpa",
    }
    # 只写入本次实际采集的指标；share-only 模式不会产生其他字段的空值。
    field_map = {
        "read_count": "readCount",
        "like_count": "likeCount",
        "share_count": "shareCount",
        "favorite_count": "favoriteCount",
        "comment_count": "commentCount",
    }
    for source_name, target_name in field_map.items():
        value = metrics.get(source_name)
        if value is not None:
            interaction[target_name] = value
    return interaction


def ingest(
    url: str,
    metrics: dict[str, Any],
    mongo_uri: str,
    database_name: str,
    collection_name: str,
    dry_run: bool = False,
    page: dict[str, str] | None = None,
    target_collection_name: str = "collection_target",
    expected_account_name: str | None = None,
    mongo_client: MongoClient | None = None,
) -> dict[str, Any]:
    normalized_url = normalize_article_url(url)
    client = mongo_client
    owns_client = False
    collection = None
    database = None
    existing = None
    if not dry_run:
        if client is None:
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
            owns_client = True
        database = client[database_name]
        collection = database[collection_name]
        ensure_article_indexes(collection)
        existing = collection.find_one(
            {"$or": [{"article.urlNormalized": normalized_url}, {"article.url": url}]},
            {"_id": 1, "account": 1, "article": 1, "source": 1},
        )
        # 已有完整正文时直接复用，避免每次重复下载和解析网页。
        if page is None and existing:
            old_account = existing.get("account") or {}
            old_article = existing.get("article") or {}
            old_content = old_article.get("content") or {}
            cached_page = {
                "title": str(old_article.get("title") or "").strip(),
                "account_name": str(old_account.get("name") or expected_account_name or "").strip(),
                "publish_time": old_article.get("publishDate") or "",
                "content": str(old_content.get("text") or "").strip(),
            }
            if all(cached_page.values()):
                page = cached_page
    try:
        page = page or parse_page(url)
    except Exception:
        if owns_client and client is not None:
            client.close()
        raise
    account_name = page["account_name"].strip()
    publish_time = page["publish_time"] or metrics.get("publish_time")
    publish_date = publish_time if isinstance(publish_time, datetime) else parse_publish_time(publish_time)
    now = datetime.now(timezone.utc)
    interaction = build_interaction(metrics, now)
    result = {
        "url": url,
        "title": page["title"],
        "account_name": account_name,
        "publish_time": publish_time,
        "content_chars": len(page["content"]),
        "content": page["content"],
        "interaction": interaction,
        "status": "dry_run" if dry_run else "pending",
    }
    if dry_run:
        return result

    try:
        assert database is not None and collection is not None and client is not None
        target = database[target_collection_name].find_one(
            {"name": account_name}, {"id": 1}
        )
        # 优先沿用 collection_target 中的账号 ID；历史配置未设置 ID 时稳定生成兜底值。
        account_id = str((target or {}).get("id") or "").strip() or stable_account_id(account_name)
        if existing is None:
            existing = collection.find_one(
                {"$or": [{"article.urlNormalized": normalized_url}, {"article.url": url}]},
                {
                    "account.id": 1,
                    "account.name": 1,
                    "article.title": 1,
                    "article.publishDate": 1,
                    "article.content.text": 1,
                    "source.type": 1,
                },
            )
        if existing is None:
            collection.update_one(
                {"article.urlNormalized": normalized_url},
                {
                    "$setOnInsert": {
                        "account.id": account_id,
                        "account.name": account_name,
                        "article.title": page["title"],
                        "article.url": url,
                        "article.publishDate": publish_date,
                        "article.content.text": page["content"],
                        "source.type": "wechat-desktop-rpa",
                        "firstCollectedAt": now,
                    },
                    "$set": {
                        "article.urlNormalized": normalized_url,
                        "source.syncedAt": now,
                        "lastUpdatedAt": now,
                    },
                    "$push": {"interactionHistory": {"$each": [interaction], "$slice": -90}},
                },
                upsert=True,
            )
            result["status"] = "inserted"
            result["baseFieldsUpdated"] = ["all"]
            return result

        # 已存在 URL 时保护原始文章，只补齐缺失字段；互动数据始终追加为历史快照。
        account = existing.get("account") or {}
        article = existing.get("article") or {}
        content = article.get("content") or {}
        source = existing.get("source") or {}
        missing_fields: dict[str, Any] = {}
        if not str(account.get("id") or "").strip():
            missing_fields["account.id"] = account_id
        if not str(account.get("name") or "").strip():
            missing_fields["account.name"] = account_name
        if not str(article.get("title") or "").strip():
            missing_fields["article.title"] = page["title"]
        if article.get("publishDate") is None:
            missing_fields["article.publishDate"] = publish_date
        if not str(content.get("text") or "").strip():
            missing_fields["article.content.text"] = page["content"]
        if not str(source.get("type") or "").strip():
            missing_fields["source.type"] = "wechat-desktop-rpa"

        collection.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    **missing_fields,
                    "article.urlNormalized": normalized_url,
                    "source.syncedAt": now,
                    "lastUpdatedAt": now,
                },
                "$push": {"interactionHistory": {"$each": [interaction], "$slice": -90}},
            },
        )
        result["status"] = "updated"
        result["baseFieldsUpdated"] = sorted(missing_fields)
        return result
    finally:
        if owns_client and client is not None:
            client.close()


def append_local_exports(
    record: dict[str, Any], jsonl_path: str | None, csv_path: str | None
) -> None:
    """逐篇落盘，避免长任务中途退出时丢失已经采集的数据。"""
    if jsonl_path:
        path = os.path.abspath(jsonl_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    if csv_path:
        path = os.path.abspath(csv_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fields = [
            "url", "title", "account_name", "publish_time", "content_chars", "content",
            "read_count", "like_count", "share_count", "favorite_count", "comment_count",
            "collected_at", "status",
        ]
        interaction = record.get("interaction", {})
        row = {
            "url": record.get("url"),
            "title": record.get("title"),
            "account_name": record.get("account_name"),
            "publish_time": record.get("publish_time"),
            "content_chars": record.get("content_chars"),
            "content": record.get("content"),
            "read_count": interaction.get("readCount"),
            "like_count": interaction.get("likeCount"),
            "share_count": interaction.get("shareCount"),
            "favorite_count": interaction.get("favoriteCount"),
            "comment_count": interaction.get("commentCount"),
            "collected_at": interaction.get("collectedAt"),
            "status": record.get("status"),
        }
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        with open(path, "a", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            if not exists:
                writer.writeheader()
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--metrics-json", required=True)
    parser.add_argument("--mongo-uri", default=os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"))
    parser.add_argument("--database", default=os.getenv("MONGO_DATABASE", "weixin"))
    parser.add_argument("--collection", default=os.getenv("MONGO_ARTICLE_COLLECTION", "article"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = ingest(
        args.url,
        json.loads(args.metrics_json),
        args.mongo_uri,
        args.database,
        args.collection,
        args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
