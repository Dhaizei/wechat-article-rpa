"""公众号采集 RPA 本地控制台：手动执行、定时调度和实时日志。"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import ctypes
import hmac
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from ctypes import wintypes
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4

from bson import ObjectId
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from env_config import load_project_env


# 必须在读取 MongoDB、管理员密码等模块级配置前加载本机 .env。
load_project_env()


RPA_DIR = Path(__file__).resolve().parent
WEB_DIR = RPA_DIR / "web"
CONFIG_PATH = RPA_DIR / "config" / "control_panel.json"
RUN_HISTORY_PATH = RPA_DIR / "config" / "run_history.json"
PANEL_LOG_PATH = RPA_DIR / "output" / "control-panel.log"
PANEL_LOG_MAX_BYTES = 10 * 1024 * 1024
RUN_HISTORY_LIMIT = 50
ARTICLE_MONGO_URI = os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/")
ARTICLE_MONGO_DATABASE = os.getenv("ARTICLE_MONGO_DATABASE", "weixin")
ARTICLE_MONGO_COLLECTION = os.getenv("ARTICLE_MONGO_COLLECTION", "article")
TARGET_MONGO_COLLECTION = os.getenv("MONGO_TARGET_COLLECTION", "collection_target")
# daily_news_send 生成后的编辑日报归档；团队日报只读展示，不改变其发送状态。
DAILY_REPORT_MONGO_COLLECTION = os.getenv("DAILY_REPORT_MONGO_COLLECTION", "daily_reports")
ACCOUNT_ALIASES_PATH = RPA_DIR / "config" / "account_aliases.json"
ARTICLE_EXPORT_LIMIT = 10_000
# MongoDB 中的 publishDate 沿用北京时间的无时区存储，因此筛选边界也必须固定为 UTC+8。
BEIJING_TIMEZONE = timezone(timedelta(hours=8))
# 团队资讯页只提供只读浏览能力；采集控制台、配置和导出必须由管理员访问。
# 可通过环境变量覆盖默认凭据，便于部署时接入自己的密钥管理方式。
CONTROL_PANEL_USERNAME = os.getenv("CONTROL_PANEL_USERNAME", "admin")
CONTROL_PANEL_PASSWORD = os.getenv("CONTROL_PANEL_PASSWORD", "admin-123")
PUBLIC_TEAM_PATHS = frozenset(
    {
        "/briefing.html",
        "/briefing.css",
        "/briefing.js",
        "/daily.html",
        "/daily.css",
        "/daily.js",
        "/directory.html",
        "/directory.css",
        "/directory.js",
    }
)
PUBLIC_TEAM_API_PATHS = frozenset(
    {"/api/accounts", "/api/articles", "/api/daily-report", "/api/daily-briefing"}
)
DEFAULT_CONFIG = {
    "enabled": False,
    "times": ["08:00", "22:00"],
    "max_articles": 20,
    "scan_range": "today_yesterday",
    # 定时任务按时段决定范围：早上补前一天，晚上仅采集当天新增。
    "schedule_ranges": {"08:00": "today_yesterday", "22:00": "today"},
    "metrics": "share",
}
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SCAN_RANGE_VALUES = {"today", "yesterday", "today_yesterday"}
METRIC_VALUES = {"share", "all"}
# 只有这些事件表示当前文章或账号已没有恢复机会；重试过程保持为信息日志。
TERMINAL_WARNING_EVENTS = frozenset(
    {
        "article_collect_failed",
        "account_collection_failed",
        "article_tab_cleanup_failed",
    }
)

# 采集器只传递稳定的错误分类；控制台在此转换为用户可以立即执行的恢复步骤。
# 该映射同时写入任务历史，避免日后分类文案调整导致旧任务无法解释。
FAILURE_RECOVERY_HINTS = {
    "account_filter": "确认“搜一搜”已选中“账号”和二级“公众号”，保持窗口可见后重试。",
    "account_not_found": "确认公众号当前名称；如名称已变更，请更新账号别名后重试。",
    "profile_validation": "关闭残留的公众号资料页，确认搜索结果名称后重试。",
    "interaction_ocr": "确认微信缩放和页面完整显示，再重试补采互动数据。",
    "copy_link": "确认文章页已完全加载且微信可访问剪贴板后重试。",
    "window": "将微信“搜一搜”窗口置前并保持可见后重试。",
    "network": "检查网络连接和微信页面加载状态后重试。",
    "mongo": "检查入库服务连接与唯一索引状态后重试。",
}
DEFAULT_FAILURE_RECOVERY_HINT = "请保留本次输出目录的 run.log，并确认微信页面状态后重试。"


def requires_control_auth(path: str) -> bool:
    """判断请求是否属于私有采集控制台。

    团队阅读页仅包含日报、文章动态、公众号目录及只读查询接口；控制台页面、导出、
    任务日志和所有写入接口均要求管理员认证，避免“知道地址就能启动采集”。
    """
    if path in PUBLIC_TEAM_PATHS or path in PUBLIC_TEAM_API_PATHS:
        return False
    return path in {"/", "/index.html", "/accounts.html", "/articles.html"} or path.startswith("/api/")


def build_collector_command(output_dir: Path, options: dict[str, Any]) -> list[str]:
    """Build the exact collector invocation from one immutable run snapshot.

    Keeping this separate from process creation makes the most important user
    choice (scan range) easy to regression-test.  In particular, a manual
    "today" run must never silently fall back to the saved schedule's range.
    """
    return [
        sys.executable,
        "-u",
        str(RPA_DIR / "wechat_visual_rpa.py"),
        "--run-search-accounts",
        "--live",
        "--accounts-from-mongo",
        "--write-mongo",
        "--metrics",
        str(options["metrics"]),
        "--scan-range",
        str(options["scan_range"]),
        "--window-layout",
        "auto",
        "--max-articles",
        str(options["max_articles"]),
        "--output-dir",
        str(output_dir),
        "--export-jsonl",
        str(output_dir / "articles.jsonl"),
        "--export-csv",
        str(output_dir / "articles.csv"),
    ]


def beijing_today(now: datetime | None = None) -> date:
    """返回北京时间日期，避免部署到其他时区时“今天”发生偏移。"""
    return (now or datetime.now(BEIJING_TIMEZONE)).astimezone(BEIJING_TIMEZONE).date()


def article_date_window(date_filter: str) -> tuple[datetime, datetime] | None:
    """Return local (Beijing) day bounds for article.publishDate.

    Existing article data deliberately stores a human-readable Beijing local
    time as a naive MongoDB datetime, so this page must not apply an extra UTC
    conversion when filtering today or yesterday.
    """
    if date_filter == "all":
        return None
    today = beijing_today()
    if date_filter == "today":
        day = today
    elif date_filter == "yesterday":
        day = today - timedelta(days=1)
    else:
        try:
            # 控制台只按北京时间的自然日筛选，和现有 MongoDB 存储口径保持一致。
            day = datetime.strptime(date_filter, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("发布日期必须是 YYYY-MM-DD、today、yesterday 或 all") from exc
    start = datetime.combine(day, datetime.min.time())
    return start, start + timedelta(days=1)


def serialize_article_item(document: dict[str, Any]) -> dict[str, Any]:
    """Convert a Mongo article document into the small list-card payload."""
    account = document.get("account") or {}
    article = document.get("article") or {}
    latest = document.get("latestInteraction") or {}
    publish_date = article.get("publishDate")
    if isinstance(publish_date, datetime):
        publish_text = publish_date.strftime("%Y-%m-%d %H:%M")
    else:
        publish_text = str(publish_date or "")
    return {
        "id": str(document.get("_id") or ""),
        "account_name": str(account.get("name") or "未识别公众号"),
        "title": str(article.get("title") or "未命名文章"),
        "publish_time": publish_text,
        "url": str(article.get("url") or ""),
        "share_count": latest.get("shareCount"),
        "recognition_method": str(latest.get("recognitionMethod") or ""),
        "content_available": bool((article.get("content") or {}).get("text")),
        "last_updated_at": _format_datetime(document.get("lastUpdatedAt")),
    }


ARTICLE_EXPORT_FIELDS = (
    "account_name", "title", "publish_time", "url", "content",
    "read_count", "like_count", "share_count", "favorite_count", "comment_count",
    "collected_at", "recognition_method",
)
ARTICLE_EXPORT_LABELS = {
    "account_name": "公众号",
    "title": "文章标题",
    "publish_time": "发布时间",
    "url": "文章链接",
    "content": "纯文本正文",
    "read_count": "阅读数",
    "like_count": "点赞数",
    "share_count": "转发数",
    "favorite_count": "收藏数",
    "comment_count": "评论数",
    "collected_at": "互动采集时间",
    "recognition_method": "互动识别方式",
}


def _article_match(date_filter: str, account: str, query: str) -> dict[str, Any]:
    """构造文章查询条件；多个公众号以逗号或换行分隔并做精确匹配。"""
    match: dict[str, Any] = {}
    date_window = article_date_window(date_filter)
    if date_window:
        match["article.publishDate"] = {"$gte": date_window[0], "$lt": date_window[1]}
    account_names = [
        item.strip() for item in re.split(r"[,，;；\n]+", account)
        if item.strip()
    ]
    if len(account_names) == 1:
        # 单账号仍保持模糊查询，兼容现有“输入部分名称即可筛选”的体验。
        match["account.name"] = {"$regex": re.escape(account_names[0]), "$options": "i"}
    elif account_names:
        # 多账号是明确选择，避免“量子位、机器之心”被当成一个完整关键字。
        match["account.name"] = {"$in": list(dict.fromkeys(account_names))}
    if query.strip():
        match["article.title"] = {"$regex": re.escape(query.strip()), "$options": "i"}
    return match


def serialize_article_export_row(document: dict[str, Any]) -> dict[str, Any]:
    """将数据库文档转为便于表格分析的扁平导出行，正文与最新互动只取一次。"""
    account = document.get("account") or {}
    article = document.get("article") or {}
    interaction = document.get("latestInteraction") or {}
    return {
        "account_name": str(account.get("name") or "未识别公众号"),
        "title": str(article.get("title") or "未命名文章"),
        "publish_time": _format_datetime(article.get("publishDate")),
        "url": str(article.get("url") or ""),
        "content": str((article.get("content") or {}).get("text") or ""),
        "read_count": interaction.get("readCount"),
        "like_count": interaction.get("likeCount"),
        "share_count": interaction.get("shareCount"),
        "favorite_count": interaction.get("favoriteCount"),
        "comment_count": interaction.get("commentCount"),
        "collected_at": _format_datetime(interaction.get("collectedAt")),
        "recognition_method": str(interaction.get("recognitionMethod") or ""),
    }


def serialize_article_export_csv(rows: list[dict[str, Any]]) -> str:
    """以带 BOM 的 CSV 导出文章正文，保证 Excel 可直接识别中文。"""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=[ARTICLE_EXPORT_LABELS[field] for field in ARTICLE_EXPORT_FIELDS])
    writer.writeheader()
    for row in rows:
        writer.writerow({ARTICLE_EXPORT_LABELS[field]: row.get(field, "") for field in ARTICLE_EXPORT_FIELDS})
    return "\ufeff" + stream.getvalue()


def _format_datetime(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else str(value or "")


def json_serialization_default(value: Any) -> str:
    """将 MongoDB 详情里的时间和 ObjectId 转为浏览器可读取的字符串。

    文章列表只展示了少量字段，而文章详情会返回完整互动记录；其中的
    collectedAt 是 datetime。这里统一处理，避免详情接口在编码 JSON 时中断。
    """
    if isinstance(value, datetime):
        return _format_datetime(value)
    if isinstance(value, ObjectId):
        return str(value)
    raise TypeError(f"不支持 JSON 序列化的类型：{type(value).__name__}")


def load_account_aliases(path: Path = ACCOUNT_ALIASES_PATH) -> dict[str, str]:
    """读取本地搜索别名；配置异常不能阻塞公众号管理页。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(name): alias.strip()
        for name, alias in payload.items()
        if isinstance(alias, str) and alias.strip()
    }


def list_accounts(
    *,
    query: str = "",
    status: str = "all",
    category: str = "",
    limit: int = 50,
    offset: int = 0,
    target_collection: Any | None = None,
    article_collection: Any | None = None,
    aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """读取采集账号清单及文章覆盖情况；不修改 MongoDB 或别名配置。"""
    if not 1 <= limit <= 100:
        raise ValueError("limit 必须是 1 到 100 的整数")
    if offset < 0:
        raise ValueError("offset 必须是非负整数")
    if status not in {"all", "covered", "missing", "alias"}:
        raise ValueError("账号状态必须是 all、covered、missing 或 alias")

    client = None
    if target_collection is None or article_collection is None:
        client = MongoClient(ARTICLE_MONGO_URI, serverSelectionTimeoutMS=5000)
        database = client[ARTICLE_MONGO_DATABASE]
        target_collection = database[TARGET_MONGO_COLLECTION]
        article_collection = database[ARTICLE_MONGO_COLLECTION]
    try:
        coverage = {
            str(item.get("_id") or ""): item
            for item in article_collection.aggregate(
                [
                    {"$match": {"account.name": {"$type": "string"}}},
                    {
                        "$group": {
                            "_id": "$account.name",
                            "article_count": {"$sum": 1},
                            "latest_publish": {"$max": "$article.publishDate"},
                        }
                    },
                ]
            )
        }
        # 账号量目前很小，先完整读取后再做筛选，能保证概览和分页口径一致。
        rows = list(
            target_collection.find(
                {}, {"name": 1, "id": 1, "category": 1, "type": 1}
            ).sort("name", 1)
        )
        alias_map = aliases if aliases is not None else load_account_aliases()
        all_items = []
        for row in rows:
            name = str(row.get("name") or "未命名公众号")
            summary = coverage.get(name, {})
            alias = alias_map.get(name, "")
            article_count = int(summary.get("article_count") or 0)
            all_items.append(
                {
                    "id": str(row.get("_id") or ""),
                    "name": name,
                    "source_id": str(row.get("id") or ""),
                    "category": str(row.get("category") or "未分类"),
                    "account_type": str(row.get("type") or "公众号"),
                    "search_name": alias or name,
                    "alias_configured": bool(alias),
                    "article_count": article_count,
                    "latest_publish": _format_datetime(summary.get("latest_publish")),
                    # 这是文章覆盖状态，不对采集任务是否“成功”做推断。
                    "coverage_status": "covered" if article_count else "missing",
                }
            )
        normalized_query = query.strip().lower()
        normalized_category = category.strip()
        categories = sorted({item["category"] for item in all_items})
        items = [
            item
            for item in all_items
            if (
                not normalized_query
                or normalized_query in item["name"].lower()
                or normalized_query in item["search_name"].lower()
                or normalized_query in item["source_id"].lower()
            )
            and (
                status == "all"
                or item["coverage_status"] == status
                or (status == "alias" and item["alias_configured"])
            )
            and (not normalized_category or item["category"] == normalized_category)
        ]
        page_items = items[offset : offset + limit]
        return {
            "items": page_items,
            "total": len(items),
            "limit": limit,
            "offset": offset,
            # 返回真实分类，前端不维护一份可能过期的分类列表。
            "categories": categories,
            # 保留旧字段，兼容已经使用该接口的本地页面或脚本。
            "alias_count": sum(1 for item in all_items if item["alias_configured"]),
            "summary": {
                "total": len(all_items),
                "covered": sum(1 for item in all_items if item["coverage_status"] == "covered"),
                "missing": sum(1 for item in all_items if item["coverage_status"] == "missing"),
                "alias": sum(1 for item in all_items if item["alias_configured"]),
            },
        }
    finally:
        if client is not None:
            client.close()


def list_articles(
    *,
    date_filter: str = "today",
    account: str = "",
    query: str = "",
    sort: str = "publish_desc",
    minimum_share: int | None = None,
    limit: int = 30,
    offset: int = 0,
    collection: Any | None = None,
) -> dict[str, Any]:
    """Read paginated articles only; this function never writes MongoDB."""
    if not 1 <= limit <= 100:
        raise ValueError("limit 必须是 1 到 100 的整数")
    if offset < 0:
        raise ValueError("offset 必须是非负整数")
    if sort not in {"publish_desc", "share_desc"}:
        raise ValueError("排序方式必须是 publish_desc 或 share_desc")
    if minimum_share is not None and minimum_share < 0:
        raise ValueError("最低转发数必须是非负整数")
    match = _article_match(date_filter, account, query)

    client = None
    if collection is None:
        client = MongoClient(ARTICLE_MONGO_URI, serverSelectionTimeoutMS=5000)
        collection = client[ARTICLE_MONGO_DATABASE][ARTICLE_MONGO_COLLECTION]
    try:
        # 转发数来自最新一次互动记录，必须在提取 latestInteraction 后再筛选和计数。
        # 否则总数会包含当前页实际不会展示的文章，分页也会失真。
        filter_pipeline = [
            {"$match": match},
            {"$addFields": {"latestInteraction": {"$arrayElemAt": ["$interactionHistory", -1]}}},
        ]
        if minimum_share is not None:
            filter_pipeline.append({"$match": {"latestInteraction.shareCount": {"$gte": minimum_share}}})

        count_result = list(collection.aggregate([*filter_pipeline, {"$count": "total"}]))
        total = int(count_result[0]["total"]) if count_result else 0
        pipeline = [
            *filter_pipeline,
            {"$sort": {"latestInteraction.shareCount" if sort == "share_desc" else "article.publishDate": -1, "_id": -1}},
            {"$skip": offset},
            {"$limit": limit},
            {"$project": {"account": 1, "article.title": 1, "article.publishDate": 1, "article.url": 1, "article.content.text": 1, "latestInteraction": 1, "lastUpdatedAt": 1}},
        ]
        items = [serialize_article_item(item) for item in collection.aggregate(pipeline)]
        return {"items": items, "total": total, "limit": limit, "offset": offset}
    finally:
        if client is not None:
            client.close()


def _daily_report_date(value: str) -> tuple[date, datetime, datetime]:
    """验证日报日期，并返回与 MongoDB 一致的北京时间自然日边界。"""
    try:
        report_day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("日报日期必须是 YYYY-MM-DD") from exc
    start = datetime.combine(report_day, datetime.min.time())
    return report_day, start, start + timedelta(days=1)


def _daily_excerpt(value: Any, limit: int = 118) -> str:
    """日报只展示短摘要，避免把整篇正文一次传给所有浏览者。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else f"{text[:limit].rstrip()}…"


def _daily_number(value: Any) -> int:
    """互动数据可能缺失或被识别为字符串，日报统一按非负整数处理。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _is_daily_reportable_title(value: Any) -> bool:
    """团队日报只保留资讯内容，过滤招聘、招募等账号运营信息。"""
    title = re.sub(r"\s+", "", str(value or ""))
    # 这是展示层规则：原始文章仍完整保存在 MongoDB，控制台也仍可查询。
    return bool(title) and not any(keyword in title for keyword in ("招聘", "招募", "诚聘", "加入我们"))


def _daily_briefing_time_range(report: dict[str, Any]) -> dict[str, str]:
    """将日报归档的原始统计区间转成可直接展示的文案。

    ``reportDate`` 是日报生成/发送日期，不等于资讯覆盖日期。页面必须展示
    归档中真实记录的 ``timeRange``，不能凭“昨天”猜测，避免再次造成日期歧义。
    """
    time_range = report.get("timeRange") or {}
    start = _format_datetime(time_range.get("start"))
    end = _format_datetime(time_range.get("end"))
    if start and end:
        label = f"汇总范围：{start} — {end}"
    elif start:
        label = f"汇总起点：{start}"
    else:
        label = "汇总范围暂未记录"
    return {"start": start, "end": end, "label": label}


def _daily_briefing_sort_time(value: Any) -> float:
    """把日报归档的生成时间标准化为可比较的时间戳。

    历史归档可能同时存在 MongoDB ``datetime`` 和 ISO 字符串。直接排序会因
    两种类型不可比较而让团队阅读页报错；这里统一按北京时间解释无时区值。
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return float("-inf")
    else:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
    return parsed.timestamp()


def daily_briefing(
    *, issue_date: str, report_collection: Any | None = None
) -> dict[str, Any]:
    """读取一份已生成的编辑日报，供团队阅读页展示；全程只读。"""
    selected_day, _start, _end = _daily_report_date(issue_date)
    client = None
    if report_collection is None:
        client = MongoClient(ARTICLE_MONGO_URI, serverSelectionTimeoutMS=5000)
        report_collection = client[ARTICLE_MONGO_DATABASE][DAILY_REPORT_MONGO_COLLECTION]
    try:
        # 日报数量远小于文章库，读取最近归档后在内存排序能兼容不同 Mongo 驱动版本，
        # 也便于保障“同日多次生成时取最新一份”的展示口径。
        reports = list(
            report_collection.find(
                {},
                {
                    "reportDate": 1,
                    "generatedAt": 1,
                    "createdAt": 1,
                    "timeRange": 1,
                    "articleCount": 1,
                    "articles": 1,
                    "reportContent": 1,
                    "sendStatus": 1,
                },
            )
        )
        report_key = selected_day.isoformat()
        same_day = [report for report in reports if str(report.get("reportDate") or "") == report_key]
        same_day.sort(
            key=lambda report: _daily_briefing_sort_time(
                report.get("generatedAt") or report.get("createdAt")
            ),
            reverse=True,
        )
        report = same_day[0] if same_day else None
        archive: list[dict[str, str]] = []
        for candidate in sorted(
            reports,
            key=lambda item: (
                str(item.get("reportDate") or ""),
                _daily_briefing_sort_time(item.get("generatedAt") or item.get("createdAt")),
            ),
            reverse=True,
        ):
            date_text = str(candidate.get("reportDate") or "")
            if not date_text or any(item["issue_date"] == date_text for item in archive):
                continue
            archive.append(
                {
                    "issue_date": date_text,
                    "generated_at": _format_datetime(candidate.get("generatedAt") or candidate.get("createdAt")),
                    "coverage_label": _daily_briefing_time_range(candidate)["label"],
                }
            )
        if not report or not str(report.get("reportContent") or "").strip():
            return {
                "issue_date": report_key,
                "issue_label": f"{selected_day.year}年{selected_day.month}月{selected_day.day}日早报",
                "available": False,
                "message": "该期每日新闻尚未生成。可在“文章动态”查看已采集的原始文章。",
                "archive": archive,
            }

        articles = report.get("articles") if isinstance(report.get("articles"), list) else []
        category_counts: dict[str, int] = {}
        account_names: set[str] = set()
        highlights: list[dict[str, Any]] = []
        for article in articles:
            if not isinstance(article, dict):
                continue
            category = str(article.get("category") or "未分类")
            category_counts[category] = category_counts.get(category, 0) + 1
            account_name = str(article.get("accountName") or "").strip()
            if account_name:
                account_names.add(account_name)
            title = str(article.get("title") or "").strip()
            if title:
                highlights.append(
                    {
                        "title": title,
                        "account_name": account_name or "未识别公众号",
                        "url": str(article.get("url") or ""),
                        "share_count": _daily_number(article.get("shareCount")),
                    }
                )
        highlights.sort(key=lambda item: (item["share_count"], item["title"]), reverse=True)
        return {
            "issue_date": report_key,
            "issue_label": f"{selected_day.year}年{selected_day.month}月{selected_day.day}日早报",
            "available": True,
            "generated_at": _format_datetime(report.get("generatedAt") or report.get("createdAt")),
            "coverage": _daily_briefing_time_range(report),
            "article_count": _daily_number(report.get("articleCount")) or len(articles),
            "account_count": len(account_names),
            "send_status": str(report.get("sendStatus") or "unknown"),
            "content": str(report.get("reportContent") or "").strip(),
            "categories": [
                {"name": name, "count": count}
                for name, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
            ],
            "highlights": highlights[:5],
            "archive": archive,
        }
    finally:
        if client is not None:
            client.close()


def daily_report(
    *,
    report_date: str,
    category: str = "all",
    account: str = "",
    article_collection: Any | None = None,
    target_collection: Any | None = None,
) -> dict[str, Any]:
    """构建同事可读的日报数据；全程只读 MongoDB，不接触采集状态和配置。"""
    selected_day, start, end = _daily_report_date(report_date)
    requested_category = category.strip() or "all"
    requested_account = account.strip()
    client = None
    if article_collection is None or target_collection is None:
        client = MongoClient(ARTICLE_MONGO_URI, serverSelectionTimeoutMS=5000)
        database = client[ARTICLE_MONGO_DATABASE]
        article_collection = article_collection or database[ARTICLE_MONGO_COLLECTION]
        target_collection = target_collection or database[TARGET_MONGO_COLLECTION]

    try:
        category_by_account = {
            str(row.get("name") or "").strip(): str(row.get("category") or "未分类").strip() or "未分类"
            for row in target_collection.find({}, {"_id": 0, "name": 1, "category": 1})
            if str(row.get("name") or "").strip()
        }
        # 仅取日报真正需要的字段，正文只截取短摘要，降低局域网访问时的传输压力。
        # 目录页跳转时按账号过滤，仍复用同一份只读日报查询，避免团队端落回控制台。
        article_match: dict[str, Any] = {"article.publishDate": {"$gte": start, "$lt": end}}
        if requested_account:
            article_match["account.name"] = requested_account
        documents = article_collection.aggregate(
            [
                {"$match": article_match},
                {"$addFields": {"latestInteraction": {"$arrayElemAt": ["$interactionHistory", -1]}}},
                {
                    "$project": {
                        "account.name": 1,
                        "article.title": 1,
                        "article.publishDate": 1,
                        "article.url": 1,
                        "article.content.text": 1,
                        "latestInteraction.shareCount": 1,
                    }
                },
            ]
        )

        rows: list[dict[str, Any]] = []
        excluded_count = 0
        for document in documents:
            account = document.get("account") or {}
            article = document.get("article") or {}
            interaction = document.get("latestInteraction") or {}
            title = str(article.get("title") or "未命名文章")
            if not _is_daily_reportable_title(title):
                excluded_count += 1
                continue
            account_name = str(account.get("name") or "未识别公众号")
            publish_date = article.get("publishDate")
            rows.append(
                {
                    "account_name": account_name,
                    "category": category_by_account.get(account_name, "未分类"),
                    "title": title,
                    "url": str(article.get("url") or ""),
                    "publish_time": _format_datetime(publish_date),
                    "publish_order": publish_date if isinstance(publish_date, datetime) else datetime.min,
                    "excerpt": _daily_excerpt((article.get("content") or {}).get("text")),
                    "share_count": _daily_number(interaction.get("shareCount")),
                }
            )

        category_counts: dict[str, int] = {}
        for row in rows:
            category_counts[row["category"]] = category_counts.get(row["category"], 0) + 1
        categories = [{"key": "all", "label": "全部", "count": len(rows)}]
        categories.extend(
            {"key": name, "label": name, "count": count}
            for name, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))
        )
        if requested_category != "all" and requested_category not in category_counts:
            raise ValueError("该分类在当前日报日期中不存在")
        visible_rows = rows if requested_category == "all" else [row for row in rows if row["category"] == requested_category]
        # 热点先按转发数排序；同分时优先展示发布时间更晚的文章。
        hot_items = sorted(
            visible_rows,
            key=lambda row: (row["share_count"], row["publish_order"], row["title"]),
            reverse=True,
        )[:5]
        # 团队文章流与“今日热点”保持同一排序口径：优先展示转发更高的内容，
        # 同分时再按发布时间和标题稳定排序，方便同事快速发现传播度更高的文章。
        feed_items = sorted(
            visible_rows,
            key=lambda row: (row["share_count"], row["publish_order"], row["title"]),
            reverse=True,
        )[:30]

        activity: dict[str, dict[str, Any]] = {}
        for row in visible_rows:
            item = activity.setdefault(
                row["account_name"], {"account_name": row["account_name"], "count": 0, "latest": datetime.min, "articles": []}
            )
            item["count"] += 1
            item["latest"] = max(item["latest"], row["publish_order"])
            item["articles"].append(row)
        account_activity = []
        for item in sorted(activity.values(), key=lambda value: (-value["count"], value["account_name"]))[:6]:
            latest_articles = sorted(item["articles"], key=lambda row: row["publish_order"], reverse=True)[:3]
            account_activity.append(
                {
                    "account_name": item["account_name"],
                    "count": item["count"],
                    "articles": [
                        {"title": row["title"], "publish_time": row["publish_time"], "url": row["url"]}
                        for row in latest_articles
                    ],
                }
            )
        return {
            "date": selected_day.isoformat(),
            "date_label": f"{selected_day.year}年{selected_day.month}月{selected_day.day}日",
            "summary": {
                "article_count": len(rows),
                "visible_count": len(visible_rows),
                "account_count": len({row["account_name"] for row in rows}),
                "share_count": sum(row["share_count"] for row in rows),
                "excluded_count": excluded_count,
            },
            "selected_category": requested_category,
            "selected_account": requested_account,
            "categories": categories,
            "lead": hot_items[0] if hot_items else None,
            "hot_items": hot_items,
            "feed_items": feed_items,
            "account_activity": account_activity,
        }
    finally:
        if client is not None:
            client.close()


def article_export_rows(
    *,
    date_filter: str = "all",
    account: str = "",
    query: str = "",
    sort: str = "publish_desc",
    minimum_share: int | None = None,
    collection: Any | None = None,
) -> list[dict[str, Any]]:
    """按当前筛选条件导出文章正文和最新互动指标，不修改任何文章数据。"""
    if sort not in {"publish_desc", "share_desc"}:
        raise ValueError("排序方式必须是 publish_desc 或 share_desc")
    if minimum_share is not None and minimum_share < 0:
        raise ValueError("最低转发数必须是非负整数")
    match = _article_match(date_filter, account, query)
    client = None
    if collection is None:
        client = MongoClient(ARTICLE_MONGO_URI, serverSelectionTimeoutMS=5000)
        collection = client[ARTICLE_MONGO_DATABASE][ARTICLE_MONGO_COLLECTION]
    try:
        pipeline = [
            {"$match": match},
            {"$addFields": {"latestInteraction": {"$arrayElemAt": ["$interactionHistory", -1]}}},
        ]
        if minimum_share is not None:
            pipeline.append({"$match": {"latestInteraction.shareCount": {"$gte": minimum_share}}})
        count_result = list(collection.aggregate([*pipeline, {"$count": "total"}]))
        total = int(count_result[0]["total"]) if count_result else 0
        if total > ARTICLE_EXPORT_LIMIT:
            raise ValueError(f"当前条件下有 {total} 篇文章；一次最多导出 {ARTICLE_EXPORT_LIMIT} 篇，请缩小筛选范围")
        documents = collection.aggregate(
            [
                *pipeline,
                {"$sort": {"latestInteraction.shareCount" if sort == "share_desc" else "article.publishDate": -1, "_id": -1}},
                {"$project": {"account": 1, "article": 1, "latestInteraction": 1}},
            ]
        )
        return [serialize_article_export_row(document) for document in documents]
    finally:
        if client is not None:
            client.close()


def get_article_detail(article_id: str, collection: Any | None = None) -> dict[str, Any] | None:
    """Read a single article body for the detail drawer, without returning it in list calls."""
    try:
        object_id = ObjectId(article_id)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("文章 ID 格式错误") from exc
    client = None
    if collection is None:
        client = MongoClient(ARTICLE_MONGO_URI, serverSelectionTimeoutMS=5000)
        collection = client[ARTICLE_MONGO_DATABASE][ARTICLE_MONGO_COLLECTION]
    try:
        document = collection.find_one({"_id": object_id})
        if document is None:
            return None
        item = serialize_article_item(document)
        item["content"] = str(((document.get("article") or {}).get("content") or {}).get("text") or "")
        item["interaction"] = document.get("interactionHistory", [])[-1] if document.get("interactionHistory") else {}
        return item
    finally:
        if client is not None:
            client.close()


def _write_json_atomically(path: Path, payload: Any) -> None:
    """用临时文件替换保存配置/运行记录，避免断电留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary_path, path)


# 导入导出只管理 collection_target 中的采集配置，绝不修改文章正文或互动历史。
ACCOUNT_IMPORT_FIELDS = (
    "record_id",
    "name",
    "source_id",
    "category",
    "account_type",
    "search_name",
)
ACCOUNT_IMPORT_LABELS = {
    "record_id": "记录ID",
    "name": "公众号名称",
    "source_id": "原始ID",
    "category": "分类",
    "account_type": "账号类型",
    "search_name": "采集搜索名",
}
ACCOUNT_IMPORT_ALIASES = {
    "record_id": ("record_id", "记录ID"),
    "name": ("name", "公众号名称"),
    "source_id": ("source_id", "原始ID", "账号ID"),
    "category": ("category", "分类"),
    "account_type": ("account_type", "账号类型", "type", "类型"),
    "search_name": ("search_name", "采集搜索名", "搜索名称"),
}


def _account_value(row: dict[str, Any], field: str) -> str:
    """兼容导出文件的中文列名与 API 的英文键名。"""
    for key in ACCOUNT_IMPORT_ALIASES[field]:
        value = row.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def normalize_account_payload(row: dict[str, Any]) -> dict[str, str]:
    """规范化单条账号配置，并在写入前拒绝空名称和异常长文本。"""
    if not isinstance(row, dict):
        raise ValueError("账号数据必须是对象")
    normalized = {field: _account_value(row, field) for field in ACCOUNT_IMPORT_FIELDS}
    if not normalized["name"]:
        raise ValueError("公众号名称不能为空")
    limits = {
        "record_id": 80,
        "name": 100,
        "source_id": 120,
        "category": 60,
        "account_type": 60,
        "search_name": 100,
    }
    for field, limit in limits.items():
        if len(normalized[field]) > limit:
            raise ValueError(f"{ACCOUNT_IMPORT_LABELS[field]}不能超过 {limit} 个字符")
    normalized["category"] = normalized["category"] or "未分类"
    normalized["account_type"] = normalized["account_type"] or "公众号"
    normalized["search_name"] = normalized["search_name"] or normalized["name"]
    return normalized


def _account_collections() -> tuple[Any, Any, MongoClient]:
    """打开账号配置和文章集合；调用方必须在 finally 中关闭 client。"""
    client = MongoClient(ARTICLE_MONGO_URI, serverSelectionTimeoutMS=5000)
    database = client[ARTICLE_MONGO_DATABASE]
    return database[TARGET_MONGO_COLLECTION], database[ARTICLE_MONGO_COLLECTION], client


def account_export_rows(
    *, target_collection: Any | None = None, aliases: dict[str, str] | None = None
) -> list[dict[str, str]]:
    """导出可回导的账号配置行，记录 ID 用来避免同名账号被误更新。"""
    client = None
    if target_collection is None:
        target_collection, _article_collection, client = _account_collections()
    try:
        alias_map = aliases if aliases is not None else load_account_aliases()
        rows = target_collection.find({}, {"name": 1, "id": 1, "category": 1, "type": 1}).sort("name", 1)
        return [
            {
                "record_id": str(row.get("_id") or ""),
                "name": str(row.get("name") or ""),
                "source_id": str(row.get("id") or ""),
                "category": str(row.get("category") or "未分类"),
                "account_type": str(row.get("type") or "公众号"),
                "search_name": alias_map.get(str(row.get("name") or ""), str(row.get("name") or "")),
            }
            for row in rows
        ]
    finally:
        if client is not None:
            client.close()


def serialize_account_export_csv(rows: list[dict[str, str]]) -> str:
    """使用 UTF-8 BOM，保证 Excel 直接打开中文 CSV 时不乱码。"""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=[ACCOUNT_IMPORT_LABELS[field] for field in ACCOUNT_IMPORT_FIELDS])
    writer.writeheader()
    for row in rows:
        writer.writerow({ACCOUNT_IMPORT_LABELS[field]: row.get(field, "") for field in ACCOUNT_IMPORT_FIELDS})
    return "\ufeff" + stream.getvalue()


def parse_account_import(content: str, import_format: str) -> list[dict[str, Any]]:
    """解析导入文件。这里只解析，不产生任何数据库写入。"""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("导入文件为空")
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("导入文件不能超过 2MB")
    if import_format == "csv":
        rows = list(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))))
    elif import_format == "json":
        payload = json.loads(content)
        rows = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("JSON 导入文件必须是账号数组，或包含 items 数组")
    else:
        raise ValueError("仅支持 CSV 或 JSON 格式")
    if not rows:
        raise ValueError("导入文件没有可用账号")
    if len(rows) > 500:
        raise ValueError("一次最多导入 500 个账号")
    return rows


def _existing_account_rows(target_collection: Any) -> dict[str, dict[str, Any]]:
    """用字符串化的 ObjectId 建立索引，导入预览与写入共用同一识别规则。"""
    return {
        str(row.get("_id") or ""): row
        for row in target_collection.find({}, {"name": 1, "id": 1, "category": 1, "type": 1})
    }


def preview_account_import(
    rows: list[dict[str, Any]], *, target_collection: Any | None = None, article_collection: Any | None = None
) -> dict[str, Any]:
    """给导入提供确定的新增/更新预览；有错误时不允许批量应用。"""
    client = None
    if target_collection is None or article_collection is None:
        target_collection, article_collection, client = _account_collections()
    try:
        existing = _existing_account_rows(target_collection)
        preview: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for index, row in enumerate(rows, start=2):
            try:
                item = normalize_account_payload(row)
                record_id = item["record_id"]
                if record_id and record_id in seen_ids:
                    raise ValueError("记录ID重复")
                seen_ids.add(record_id)
                if item["name"] in seen_names:
                    raise ValueError("公众号名称重复")
                seen_names.add(item["name"])
                current = existing.get(record_id) if record_id else None
                if record_id and current is None:
                    raise ValueError("记录ID不存在，不能更新未知账号")
                same_name = next(
                    (candidate for candidate in existing.values() if str(candidate.get("name") or "") == item["name"]),
                    None,
                )
                if same_name and not current:
                    raise ValueError("已存在同名公众号，请使用导出文件中的记录ID更新")
                if same_name and current and same_name is not current:
                    raise ValueError("公众号名称已被其他账号使用")
                if current and item["name"] != str(current.get("name") or ""):
                    # 改名会切断历史文章 account.name 的归属，因此必须走专门的数据迁移流程。
                    if article_collection.count_documents({"account.name": current.get("name")}) > 0:
                        raise ValueError("已有入库文章的公众号不能直接改名，请保留公众号名称并修改采集搜索名")
                action = "更新" if current else "新增"
                preview.append({"line": index, "status": "valid", "action": action, "item": item})
            except ValueError as exc:
                preview.append({"line": index, "status": "error", "message": str(exc)})
        return {
            "items": preview,
            "summary": {
                "total": len(preview),
                "create": sum(1 for item in preview if item.get("action") == "新增"),
                "update": sum(1 for item in preview if item.get("action") == "更新"),
                "error": sum(1 for item in preview if item["status"] == "error"),
            },
        }
    finally:
        if client is not None:
            client.close()


def upsert_account_config(
    row: dict[str, Any],
    *,
    target_collection: Any | None = None,
    article_collection: Any | None = None,
    aliases: dict[str, str] | None = None,
    persist_aliases: bool = True,
) -> dict[str, Any]:
    """新增或编辑一条采集账号，且只写 collection_target 与本地别名文件。"""
    item = normalize_account_payload(row)
    client = None
    if target_collection is None or article_collection is None:
        target_collection, article_collection, client = _account_collections()
    try:
        existing = _existing_account_rows(target_collection)
        current = existing.get(item["record_id"]) if item["record_id"] else None
        if item["record_id"] and current is None:
            raise ValueError("记录ID不存在，无法保存")
        if current and item["name"] != str(current.get("name") or ""):
            if article_collection.count_documents({"account.name": current.get("name")}) > 0:
                raise ValueError("已有入库文章的公众号不能直接改名，请仅修改采集搜索名")
        document = {"name": item["name"], "id": item["source_id"], "category": item["category"], "type": item["account_type"]}
        if current:
            target_collection.update_one({"_id": current["_id"]}, {"$set": document})
            record_id = str(current["_id"])
            old_name = str(current.get("name") or "")
        else:
            duplicate = next((row for row in existing.values() if str(row.get("name") or "") == item["name"]), None)
            if duplicate:
                raise ValueError("已存在同名公众号，请使用编辑操作")
            result = target_collection.insert_one(document)
            record_id = str(result.inserted_id)
            old_name = ""

        alias_map = aliases if aliases is not None else load_account_aliases()
        if old_name and old_name != item["name"]:
            alias_map.pop(old_name, None)
        if item["search_name"] == item["name"]:
            alias_map.pop(item["name"], None)
        else:
            alias_map[item["name"]] = item["search_name"]
        if persist_aliases:
            _write_json_atomically(ACCOUNT_ALIASES_PATH, alias_map)
        return {"record_id": record_id, "action": "updated" if current else "created", "item": {**item, "record_id": record_id}}
    finally:
        if client is not None:
            client.close()


class RunHistory:
    """控制台的持久化任务历史。

    采集器仍是单机桌面自动化任务，因此先将轻量的运行清单保存在本机；
    无论控制台是否重启，运营者都能知道上次任务实际参数、环境检查和结束原因。
    """

    def __init__(self, path: Path = RUN_HISTORY_PATH) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.records = self._load()
        self._mark_unfinished_runs_interrupted()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return payload if isinstance(payload, list) else []

    def _save(self) -> None:
        # 仅保留最近任务，避免控制台长期运行时配置文件无限增长。
        self.records = self.records[-RUN_HISTORY_LIMIT:]
        _write_json_atomically(self.path, self.records)

    def _mark_unfinished_runs_interrupted(self) -> None:
        changed = False
        now = datetime.now().isoformat(timespec="seconds")
        for record in self.records:
            if record.get("status") == "running":
                # 控制台重启后无法安全接管旧子进程，明确标为中断，供用户后续补采。
                record["status"] = "interrupted"
                record["finished_at"] = now
                record["result_message"] = "控制台重启，无法继续跟踪原采集进程"
                changed = True
        if changed:
            self._save()

    def create(self, record: dict[str, Any]) -> None:
        with self.lock:
            self.records.append(record)
            self._save()

    def update(self, run_id: str, **changes: Any) -> None:
        with self.lock:
            for record in reversed(self.records):
                if record.get("run_id") == run_id:
                    record.update(changes)
                    self._save()
                    return

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(item) for item in reversed(self.records[-max(1, limit):])]

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.lock:
            for record in reversed(self.records):
                if record.get("run_id") == run_id:
                    return dict(record)
        return None


def _visible_windows() -> list[dict[str, str]]:
    """读取可见窗口的最小信息，仅用于启动前检查，不采集窗口内容。"""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    windows: list[dict[str, str]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def enumerate_window(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title_length = user32.GetWindowTextLengthW(hwnd)
        if title_length <= 0:
            return True
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
        user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        windows.append({"title": title_buffer.value.strip(), "class_name": class_buffer.value})
        return True

    user32.EnumWindows(enumerate_window, 0)
    return windows


def collect_desktop_environment() -> dict[str, Any]:
    """读取屏幕与工作区尺寸，供页面说明当前机器会使用哪种自适应布局。"""
    if os.name != "nt":
        return {
            "ok": True,
            "tier": "unknown",
            "message": "非 Windows 环境：将由采集端使用默认窗口布局",
        }
    user32 = ctypes.windll.user32
    screen_width = user32.GetSystemMetrics(0)  # SM_CXSCREEN
    screen_height = user32.GetSystemMetrics(1)  # SM_CYSCREEN
    work_area = wintypes.RECT()
    has_work_area = bool(user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0))
    work_width = work_area.right - work_area.left if has_work_area else screen_width
    work_height = work_area.bottom - work_area.top if has_work_area else screen_height
    try:
        dpi = int(user32.GetDpiForSystem())
    except (AttributeError, OSError):
        dpi = 96
    scale = round(dpi / 96 * 100)
    if work_width >= 1440 and work_height >= 800:
        tier = "recommended"
        prefix = "推荐"
    elif work_width >= 1280 and work_height >= 720:
        tier = "compatible"
        prefix = "可兼容"
    else:
        tier = "compact"
        prefix = "紧凑适配"
    return {
        "ok": True,
        "tier": tier,
        "screen": {"width": screen_width, "height": screen_height},
        "work_area": {"width": work_width, "height": work_height},
        "dpi_scale": scale,
        # 采集端会按工作区宽高比例重新排布，并对截图坐标使用 DPI 感知。
        "message": f"{prefix}：屏幕 {screen_width}×{screen_height}，可用区域 {work_width}×{work_height}，缩放 {scale}%",
    }


def collect_preflight() -> dict[str, Any]:
    """检查运行前必需的微信主窗口和搜一搜窗口，避免任务盲目启动。"""
    windows = _visible_windows()
    main_windows = [
        item
        for item in windows
        if item["title"] == "微信" and item["class_name"].startswith("Qt")
    ]
    search_windows = [
        item
        for item in windows
        if item["class_name"].startswith("Chrome_WidgetWin")
        and (item["title"] == "微信" or "搜一搜" in item["title"])
    ]
    wechat_ok = bool(main_windows)
    search_ok = bool(search_windows)
    desktop = collect_desktop_environment()
    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "ready": wechat_ok and search_ok,
        "wechat": {
            "ok": wechat_ok,
            "message": "已检测到微信主窗口" if wechat_ok else "未检测到微信主窗口：请启动微信并完成登录",
        },
        "search": {
            "ok": search_ok,
            "message": "已检测到“搜一搜”窗口" if search_ok else "未检测到“搜一搜”窗口：请在微信内搜索并打开它",
        },
        "automation": {
            "ok": True,
            "message": "公众号资料与文章窗口会在采集时自动打开和关闭",
        },
        "desktop": desktop,
    }


PREFLIGHT_RECOVERY_LOCK = threading.Lock()


def recover_sogou_preflight() -> dict[str, Any]:
    """在用户主动点击重新检测时，尝试从微信主窗口恢复“搜一搜”。"""
    before = collect_preflight()
    recovery = {
        "attempted": False,
        "succeeded": bool(before["search"]["ok"]),
        "message": "“搜一搜”窗口已就绪，无需重新打开。",
    }
    if before["search"]["ok"]:
        return {**before, "recovery": recovery}
    if STATE.running():
        recovery["message"] = "当前正在采集，为避免抢占微信窗口，暂不自动打开“搜一搜”。"
        return {**before, "recovery": recovery}
    if not before["wechat"]["ok"]:
        recovery["message"] = "未检测到已登录的微信主窗口，无法自动打开“搜一搜”。"
        return {**before, "recovery": recovery}
    if not PREFLIGHT_RECOVERY_LOCK.acquire(blocking=False):
        recovery["message"] = "正在尝试打开“搜一搜”，请稍候再检查。"
        return {**before, "recovery": recovery}

    try:
        # 复用正式采集流程的恢复逻辑，避免控制台和采集端使用两套点击规则。
        from wechat_visual_rpa import open_sogou_from_wechat_main

        STATE.add_log("info", "未检测到“搜一搜”窗口，正在从微信主窗口自动打开。")
        opened = open_sogou_from_wechat_main("控制台预检")
        time.sleep(0.8)
        after = collect_preflight()
        recovered = bool(after["search"]["ok"])
        message = "已自动打开“搜一搜”窗口并完成复检。" if recovered else "已提交打开“搜一搜”的操作，但窗口尚未被检测到。"
        STATE.add_log("success" if recovered else "warning", message)
        return {
            **after,
            "recovery": {
                "attempted": True,
                "succeeded": recovered,
                "message": message,
                "window_handle": opened.hwnd,
            },
        }
    except Exception as exc:  # noqa: BLE001
        message = f"自动打开“搜一搜”失败：{exc}"
        STATE.add_log("warning", message)
        after = collect_preflight()
        return {
            **after,
            "recovery": {"attempted": True, "succeeded": False, "message": message},
        }
    finally:
        PREFLIGHT_RECOVERY_LOCK.release()


def process_line_log_level(line: str) -> str:
    """将采集子进程的一行输出映射为控制台日志等级。"""
    level = "error" if " ERROR " in f" {line} " else "info"
    if '"event"' not in line:
        return level
    try:
        payload = json.loads(line[line.index("{") :])
    except (ValueError, json.JSONDecodeError):
        return level
    event = str(payload.get("event") or "")
    return "warning" if event in TERMINAL_WARNING_EVENTS else level


def _event_text(value: Any, fallback: str = "未提供") -> str:
    """压缩子进程字段，避免异常详情或标题撑满控制台实时日志。"""
    text = " ".join(str(value or fallback).split())
    return f"{text[:157]}..." if len(text) > 160 else text


def _event_count(value: Any) -> int:
    """外部结构化日志允许缺失或非数字计数，控制台展示时安全降级为 0。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def failure_recovery_hint(category: Any) -> str:
    """按稳定错误分类返回控制台与任务历史共用的恢复建议。"""
    return FAILURE_RECOVERY_HINTS.get(str(category or "").strip(), DEFAULT_FAILURE_RECOVERY_HINT)


def format_process_event_message(payload: dict[str, Any]) -> str | None:
    """将采集器 JSON 事件收敛为适合人工浏览的一行日志。

    子进程完整 JSON 仍会写入任务目录的 ``run.log``；控制台只保留决定下一步动作的
    摘要，避免一次账号完成事件展开成数百行，同时不丢失失败类别和扫描范围诊断。
    """
    event = _event_text(payload.get("event"), "")
    account = _event_text(payload.get("account"), "未命名公众号")
    if event == "accounts_loaded":
        return f"已加载 {_event_count(payload.get('count'))} 个公众号。"
    if event == "account_collection_started":
        return f"开始采集公众号：{account}（每账号最多 {_event_count(payload.get('max_articles'))} 篇）。"
    if event == "account_collection_finished":
        detected = _event_count(payload.get("detected_articles"))
        scan = payload.get("scan") if isinstance(payload.get("scan"), dict) else {}
        if detected:
            return f"公众号采集完成：{account}，识别到 {detected} 篇文章。"
        return (
            f"公众号无更新：{account}；已检查 {_event_count(scan.get('observed_cards'))} 张卡片，"
            f"范围外 {_event_count(scan.get('outside_range_cards'))} 张；"
            f"原因：{_event_text(payload.get('stop_reason'))}。"
        )
    if event == "account_collection_failed":
        category = _event_text(payload.get("category"), "unknown")
        return (
            f"公众号采集失败：{account}；类别={category}；"
            f"原因：{_event_text(payload.get('error'))}。建议：{failure_recovery_hint(category)}"
        )
    if event == "article_ingest_finished":
        status_label = {"inserted": "已新增", "updated": "已更新", "unchanged": "无变化"}.get(
            _event_text(payload.get("status"), ""), "已处理"
        )
        return f"文章{status_label}：{_event_text(payload.get('title'), '未识别标题')}。"
    if event == "article_collect_failed":
        return f"文章采集失败：{account}；原因：{_event_text(payload.get('error'))}。"
    if event == "article_tab_cleanup_failed":
        return (
            f"浏览器标签待确认：{account}；为保护“搜一搜”页，系统未继续关闭标签；"
            f"原因：{_event_text(payload.get('error'))}。任务结束后请确认微信回到“搜一搜”页，再重试受影响账号。"
        )
    if event == "article_metrics_partial":
        return "文章互动指标待补齐：已保留可识别的指标，后续可重试补采。"
    if event == "article_title_evidence_warning":
        return f"文章标题证据待复核：{_event_text(payload.get('title'), '未识别标题')}。"
    return None


def determine_final_run_status(
    *,
    exit_code: int,
    manually_stopped: bool,
    summary: dict[str, Any],
) -> tuple[str, str]:
    """结合退出码与业务失败数确定状态，避免“失败 69 个”仍显示已完成。"""
    if manually_stopped:
        return "cancelled", "已手动停止"
    if exit_code != 0:
        return "failed", "任务异常退出"
    failed_accounts = int(summary.get("accounts_failed") or 0)
    successful_accounts = int(summary.get("accounts_succeeded") or 0) + int(
        summary.get("accounts_no_updates") or 0
    )
    if failed_accounts and not successful_accounts:
        return "failed", "任务执行结束，但全部公众号采集失败"
    if failed_accounts:
        return "partial", "任务执行结束，部分公众号采集失败"
    return "completed", "任务执行完成"


class ControlState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.history = RunHistory()
        self.process: subprocess.Popen[str] | None = None
        self.active_run_id = ""
        self.run_source = ""
        self.started_at = ""
        self.finished_at = ""
        self.exit_code: int | None = None
        self.stop_requested = False
        # 定时计划配置与手动任务的临时选择分开保存，避免状态页产生误导。
        self.last_run_options: dict[str, Any] = {}
        # 进度完全由采集子进程的结构化事件驱动，不额外读取 MongoDB，避免影响采集速度。
        self.progress = self._empty_progress()
        self.output_dir = ""
        self.latest_event = "等待任务"
        self.log_sequence = 0
        self.logs: deque[dict[str, Any]] = deque(maxlen=600)
        self.last_schedule_key = ""
        self.config = self._load_config()
        self.add_log("info", "控制台已启动，等待执行任务。")

    @staticmethod
    def _empty_progress() -> dict[str, Any]:
        """返回一份新的采集进度，供每次任务启动时重置。"""
        return {
            "total_accounts": 0,
            "accounts_started": 0,
            "accounts_finished": 0,
            "current_account": "",
            "current_account_index": 0,
            "current_account_articles": 0,
            "articles_collected": 0,
            "accounts_succeeded": 0,
            "accounts_no_updates": 0,
            "accounts_failed": 0,
            "articles_inserted": 0,
            "articles_updated": 0,
            "articles_unchanged": 0,
            # 标题证据 OCR 仅作辅助校验，单独计数便于任务结束后复核。
            "articles_title_evidence_warnings": 0,
            # 本地模板只能确认转发数时，文章仍会写入；单独统计便于复核数据完整性。
            "articles_partial_metrics": 0,
            # 标签清理告警并非文章采集失败，但后续账号可能因未回到搜一搜页而受影响，必须独立留痕。
            "article_tab_cleanup_warnings": 0,
            "failure_samples": [],
            "no_update_samples": [],
            "tab_cleanup_samples": [],
            "phase": "等待任务",
        }

    def _record_progress_event(self, payload: dict[str, Any]) -> None:
        """将 RPA 的 JSON 日志事件汇总成适合控制台展示的进度数据。"""
        event = str(payload.get("event") or "")
        progress = self.progress
        if event == "accounts_loaded":
            progress["total_accounts"] = max(0, int(payload.get("count") or 0))
            progress["phase"] = "账号列表已加载"
        elif event == "account_collection_started":
            progress["accounts_started"] += 1
            progress["current_account_index"] = progress["accounts_started"]
            progress["current_account"] = str(payload.get("account") or "")
            progress["current_account_articles"] = 0
            progress["phase"] = "正在采集公众号"
        elif event == "article_collect_succeeded":
            progress["articles_collected"] += 1
            progress["current_account_articles"] = max(
                progress["current_account_articles"],
                int(payload.get("processed_count") or 0),
            )
            progress["phase"] = "正在采集文章"
        elif event in {"account_collection_finished", "account_collection_failed"}:
            progress["accounts_finished"] += 1
            if event == "account_collection_finished":
                if int(payload.get("detected_articles") or 0) > 0:
                    progress["accounts_succeeded"] += 1
                else:
                    progress["accounts_no_updates"] += 1
                    samples = progress["no_update_samples"]
                    if len(samples) < 12:
                        scan = payload.get("scan") or {}
                        samples.append(
                            {
                                "account": str(payload.get("account") or ""),
                                "range": str(scan.get("range") or ""),
                                "stop_reason": str(payload.get("stop_reason") or "未提供原因"),
                                "observed_cards": int(scan.get("observed_cards") or 0),
                                "outside_range_cards": int(scan.get("outside_range_cards") or 0),
                                "ungrouped_cards": int(scan.get("ungrouped_cards") or 0),
                                # 推广卡不是采集失败，保留数量以便控制台解释“无更新”。
                                "promotion_cards": int(scan.get("promotion_cards") or 0),
                            }
                        )
                progress["phase"] = "公众号采集完成"
            else:
                progress["accounts_failed"] += 1
                progress["phase"] = "公众号采集失败，继续下一个"
                failure_samples = progress["failure_samples"]
                if len(failure_samples) < 20:
                    failure_samples.append(
                        {
                            "account": str(payload.get("account") or ""),
                            "error": str(payload.get("error") or "未提供错误详情"),
                            "category": str(payload.get("category") or "unknown"),
                            # 将建议随记录持久化，历史任务无需依赖当时的前端版本也能给出下一步。
                            "recovery_hint": failure_recovery_hint(payload.get("category")),
                        }
                    )
        elif event == "article_ingest_finished":
            status = str(payload.get("status") or "")
            field = {
                "inserted": "articles_inserted",
                "updated": "articles_updated",
                "unchanged": "articles_unchanged",
            }.get(status)
            if field:
                progress[field] += 1
        elif event == "article_title_evidence_warning":
            progress["articles_title_evidence_warnings"] += 1
        elif event == "article_metrics_partial":
            progress["articles_partial_metrics"] += 1
            progress["phase"] = "已保留转发数，部分互动指标待补齐"
        elif event == "article_tab_cleanup_failed":
            progress["article_tab_cleanup_warnings"] += 1
            progress["phase"] = "浏览器标签待确认，后续账号可能受影响"
            samples = progress["tab_cleanup_samples"]
            if len(samples) < 12:
                # 仅保存人工恢复所需的最小上下文，避免把冗长异常或文章内容写进任务历史。
                samples.append(
                    {
                        "account": str(payload.get("account") or ""),
                        "title": str(payload.get("title") or ""),
                        "error": str(payload.get("error") or "未提供错误详情"),
                    }
                )

    def _load_config(self) -> dict[str, Any]:
        if not CONFIG_PATH.exists():
            return dict(DEFAULT_CONFIG)
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return dict(DEFAULT_CONFIG)
        times = validate_times(saved.get("times", DEFAULT_CONFIG["times"]))
        fallback_range = validate_scan_range(saved.get("scan_range", DEFAULT_CONFIG["scan_range"]))
        return {
            "enabled": bool(saved.get("enabled", False)),
            "times": times,
            "max_articles": validate_max_articles(saved.get("max_articles", 20)),
            "scan_range": fallback_range,
            "schedule_ranges": validate_schedule_ranges(
                saved.get("schedule_ranges"), times, fallback_range
            ),
            "metrics": validate_metrics(saved.get("metrics", DEFAULT_CONFIG["metrics"])),
        }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = bool(payload.get("enabled", False))
        times = validate_times(payload.get("times", []))
        if enabled and not times:
            raise ValueError("启用定时任务时至少需要设置一个执行时间")
        fallback_range = validate_scan_range(payload.get("scan_range", DEFAULT_CONFIG["scan_range"]))
        config = {
            "enabled": enabled,
            "times": times,
            "max_articles": validate_max_articles(payload.get("max_articles", 20)),
            "scan_range": fallback_range,
            "schedule_ranges": validate_schedule_ranges(
                payload.get("schedule_ranges"), times, fallback_range
            ),
            "metrics": validate_metrics(payload.get("metrics", DEFAULT_CONFIG["metrics"])),
        }
        _write_json_atomically(CONFIG_PATH, config)
        with self.lock:
            self.config = config
        self.add_log(
            "success",
            f"定时计划已保存：{', '.join(config['times']) or '未设置时间'}；"
            f"范围={schedule_ranges_label(config['schedule_ranges'])}，"
            f"指标={metrics_label(config['metrics'])}。",
        )
        return config

    def add_log(self, level: str, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            self.log_sequence += 1
            item = {
                "id": self.log_sequence,
                "time": timestamp,
                "level": level,
                "message": message.strip(),
            }
            self.logs.append(item)
        PANEL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 控制台日志长期运行时自动轮转，避免 panel.log 无限增长。
        try:
            if PANEL_LOG_PATH.exists() and PANEL_LOG_PATH.stat().st_size >= PANEL_LOG_MAX_BYTES:
                backup = PANEL_LOG_PATH.with_suffix(".log.1")
                if backup.exists():
                    backup.unlink()
                PANEL_LOG_PATH.replace(backup)
        except OSError:
            pass
        with PANEL_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    def running(self) -> bool:
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def _run_summary_locked(self) -> dict[str, Any]:
        """把结构化进度收敛为任务历史和结果页可直接展示的摘要。"""
        progress = self.progress
        return {
            "total_accounts": int(progress.get("total_accounts") or 0),
            "accounts_finished": int(progress.get("accounts_finished") or 0),
            "accounts_succeeded": int(progress.get("accounts_succeeded") or 0),
            "accounts_no_updates": int(progress.get("accounts_no_updates") or 0),
            "accounts_failed": int(progress.get("accounts_failed") or 0),
            "articles_collected": int(progress.get("articles_collected") or 0),
            "articles_inserted": int(progress.get("articles_inserted") or 0),
            "articles_updated": int(progress.get("articles_updated") or 0),
            "articles_unchanged": int(progress.get("articles_unchanged") or 0),
            "articles_title_evidence_warnings": int(progress.get("articles_title_evidence_warnings") or 0),
            "articles_partial_metrics": int(progress.get("articles_partial_metrics") or 0),
            "article_tab_cleanup_warnings": int(progress.get("article_tab_cleanup_warnings") or 0),
            "failure_samples": list(progress.get("failure_samples") or []),
            "no_update_samples": list(progress.get("no_update_samples") or []),
            "tab_cleanup_samples": list(progress.get("tab_cleanup_samples") or []),
        }

    def _persist_active_run_locked(self, **changes: Any) -> None:
        """保存当前任务快照；旧 reader 永远不能覆盖新任务的状态。"""
        if not self.active_run_id:
            return
        snapshot = {
            "progress": dict(self.progress),
            "summary": self._run_summary_locked(),
            "latest_event": self.latest_event,
            **changes,
        }
        self.history.update(self.active_run_id, **snapshot)

    def _create_run_record(
        self,
        *,
        run_id: str,
        source: str,
        options: dict[str, Any],
        preflight: dict[str, Any],
        status: str,
        output_dir: Path | None = None,
        result_message: str = "",
    ) -> None:
        """建立不可变的任务起始快照，便于事后复盘实际执行参数。"""
        now = datetime.now().isoformat(timespec="seconds")
        record = {
            "run_id": run_id,
            "status": status,
            "source": source,
            "requested_at": now,
            "started_at": now if status == "running" else "",
            "finished_at": now if status != "running" else "",
            "parameters": dict(options),
            "preflight": preflight,
            "desktop": preflight.get("desktop", {}),
            "output_dir": str(output_dir) if output_dir else "",
            "progress": self._empty_progress(),
            "summary": self._run_summary_locked(),
            "latest_event": result_message or "任务已创建",
            "result_message": result_message,
            "exit_code": None,
        }
        self.history.create(record)

    def start_job(
        self,
        source: str,
        max_articles: int | None = None,
        scan_range: str | None = None,
        metrics: str | None = None,
    ) -> tuple[bool, str]:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return False, "已有采集任务正在运行"
            count = validate_max_articles(
                self.config["max_articles"] if max_articles is None else max_articles
            )
            selected_scan_range = validate_scan_range(
                scan_range or self.config["scan_range"]
            )
            selected_metrics = validate_metrics(metrics or self.config["metrics"])
            options = {
                "source": source,
                "max_articles": count,
                "scan_range": selected_scan_range,
                "metrics": selected_metrics,
            }
            preflight = collect_preflight()
            run_id = uuid4().hex
            if not preflight["ready"]:
                message = "启动前检查未通过：" + "；".join(
                    item["message"]
                    for item in (preflight["wechat"], preflight["search"])
                    if not item["ok"]
                )
                self._create_run_record(
                    run_id=run_id,
                    source=source,
                    options=options,
                    preflight=preflight,
                    status="blocked",
                    result_message=message,
                )
                self.latest_event = "任务未启动：前置条件不足"
                self.last_run_options = options
                self.add_log("warning", message)
                return False, message
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            output_dir = RPA_DIR / "output" / f"mongo-{stamp}"
            output_dir.mkdir(parents=True, exist_ok=True)
            command = build_collector_command(output_dir, options)
            environment = os.environ.copy()
            environment.setdefault("MONGO_URI", "mongodb://192.168.28.70:27019/")
            # 子进程日志统一使用 UTF-8，避免中文账号在管理页面中显示为乱码。
            environment["PYTHONIOENCODING"] = "utf-8"
            creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=RPA_DIR,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creation_flags,
                )
            except OSError as exc:
                return False, f"启动采集进程失败：{exc}"
            self.process = process
            self.active_run_id = run_id
            self.run_source = source
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self.finished_at = ""
            self.exit_code = None
            self.stop_requested = False
            self.last_run_options = options
            self.progress = self._empty_progress()
            self.progress["phase"] = "正在启动采集任务"
            self.output_dir = str(output_dir)
            self.latest_event = "任务启动中"
            self._create_run_record(
                run_id=run_id,
                source=source,
                options=options,
                preflight=preflight,
                status="running",
                output_dir=output_dir,
            )
        label = "手动任务（当前页面选择）" if source == "manual" else "定时任务（已保存计划）"
        self.add_log(
            "info",
            f"{label}已启动：范围={scan_range_label(selected_scan_range)}，"
            f"指标={metrics_label(selected_metrics)}，每账号最多 {count} 篇。",
        )
        threading.Thread(target=self._read_process, args=(process, run_id), daemon=True).start()
        return True, "任务已启动"

    def _read_process(self, process: subprocess.Popen[str], run_id: str) -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            level = process_line_log_level(line)
            display_message = line
            if '"event"' in line:
                try:
                    payload = json.loads(line[line.index("{") :])
                    event = str(payload.get("event") or "采集处理中")
                    # 完整事件由子进程写入 run.log；控制台只展示一行可操作的摘要。
                    display_message = format_process_event_message(payload) or line
                    with self.lock:
                        # 停止后立即启动下一任务时，旧 reader 只能写自己的日志，
                        # 不能回写新任务的进度和状态。
                        if self.process is process and self.active_run_id == run_id:
                            self.latest_event = event
                            self._record_progress_event(payload)
                            if event in {
                                "accounts_loaded",
                                "account_collection_finished",
                                "account_collection_failed",
                                "article_ingest_finished",
                                "article_metrics_partial",
                                "article_tab_cleanup_failed",
                            }:
                                self._persist_active_run_locked()
                except (ValueError, json.JSONDecodeError):
                    pass
            self.add_log(level, display_message)
        exit_code = process.wait()
        with self.lock:
            # 若当前活跃任务已更换，旧进程只更新其历史条目，不污染新任务页面。
            active_process = self.process is process and self.active_run_id == run_id
            manually_stopped = self.stop_requested if active_process else False
            finished_at = datetime.now().isoformat(timespec="seconds")
            if active_process:
                final_summary = self._run_summary_locked()
            else:
                # 旧 reader 晚于新任务退出时，只能使用自己的历史快照，不能借用新任务进度。
                previous_record = self.history.get(run_id) or {}
                final_summary = dict(previous_record.get("summary") or {})
            final_status, final_event = determine_final_run_status(
                exit_code=exit_code,
                manually_stopped=manually_stopped,
                summary=final_summary,
            )
            if active_process:
                self.exit_code = exit_code
                self.finished_at = finished_at
                self.latest_event = final_event
                self.progress["phase"] = final_event
                self._persist_active_run_locked(
                    status=final_status,
                    finished_at=finished_at,
                    exit_code=exit_code,
                    result_message=final_event,
                )
                self.process = None
                self.active_run_id = ""
            else:
                self.history.update(
                    run_id,
                    status=final_status,
                    finished_at=finished_at,
                    exit_code=exit_code,
                    result_message=final_event,
                )
        if manually_stopped:
            self.add_log("warning", f"采集任务已手动停止，返回码：{exit_code}。")
            return
        if final_status == "completed":
            self.add_log("success", "采集任务执行完成。")
        elif final_status == "partial":
            self.add_log("warning", "采集任务已结束，但存在公众号采集失败，请查看任务诊断。")
        elif final_status == "failed" and exit_code == 0:
            self.add_log("error", "采集任务已结束，但全部公众号采集失败，请先恢复微信和搜一搜窗口。")
        else:
            self.add_log("error", f"采集任务退出，返回码：{exit_code}。")

    def stop_job(self) -> tuple[bool, str]:
        with self.lock:
            process = self.process
            if process is None or process.poll() is not None:
                return False, "当前没有正在运行的任务"
            self.stop_requested = True
            process.terminate()
        self.add_log("warning", "正在停止采集任务……")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            self.add_log("warning", "任务未及时退出，已强制停止。")
        return True, "停止指令已发送"

    def status(self) -> dict[str, Any]:
        with self.lock:
            config = dict(self.config)
            running = self.process is not None and self.process.poll() is None
            return {
                "ready": True,
                "running": running,
                "run_source": self.run_source,
                "run_id": self.active_run_id,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "exit_code": self.exit_code,
                "output_dir": self.output_dir,
                "latest_event": self.latest_event,
                # config 是定时计划的持久化默认值；last_run_options 是本次实际启动参数。
                "last_run_options": dict(self.last_run_options),
                "progress": dict(self.progress),
                "config": config,
                "next_run": next_run_text(config),
                "recent_runs": self.history.recent(5),
            }


def validate_times(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("执行时间必须是数组")
    cleaned = [str(value).strip() for value in values if str(value).strip()]
    invalid = [value for value in cleaned if not TIME_RE.fullmatch(value)]
    if invalid:
        raise ValueError(f"执行时间格式错误：{', '.join(invalid)}，请使用 HH:MM")
    result = sorted(set(cleaned))
    if len(result) > 8:
        raise ValueError("每天最多设置 8 个执行时间")
    return result


def validate_max_articles(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("每个账号最大文章数必须是整数") from exc
    if not 1 <= number <= 100:
        raise ValueError("每个账号最大文章数必须在 1 到 100 之间")
    return number


def validate_scan_range(value: Any) -> str:
    selected = str(value or DEFAULT_CONFIG["scan_range"]).strip()
    if selected not in SCAN_RANGE_VALUES:
        raise ValueError("扫描范围必须是 today、yesterday 或 today_yesterday")
    return selected


def default_schedule_range_for_time(value: str, fallback: str) -> str:
    """为常用的早晚任务提供安全默认值，其他时间沿用原来的全局范围。"""
    return {"08:00": "today_yesterday", "22:00": "today"}.get(value, fallback)


def validate_schedule_ranges(value: Any, times: list[str], fallback: str) -> dict[str, str]:
    """校验每个定时时间的扫描范围，并兼容旧版单一扫描范围配置。"""
    raw = value if isinstance(value, dict) else {}
    ranges: dict[str, str] = {}
    for scheduled_time in times:
        selected = raw.get(scheduled_time, default_schedule_range_for_time(scheduled_time, fallback))
        ranges[scheduled_time] = validate_scan_range(selected)
    return ranges


def validate_metrics(value: Any) -> str:
    selected = str(value or DEFAULT_CONFIG["metrics"]).strip()
    if selected not in METRIC_VALUES:
        raise ValueError("采集指标必须是 share 或 all")
    return selected


def scan_range_label(value: str) -> str:
    return {
        "today": "今天",
        "yesterday": "昨天",
        "today_yesterday": "今天和昨天",
    }[value]


def schedule_ranges_label(ranges: dict[str, str]) -> str:
    """按时间顺序展示定时范围，日志中能直接看出早晚任务的差别。"""
    return "；".join(
        f"{scheduled_time}={scan_range_label(scan_range)}"
        for scheduled_time, scan_range in sorted(ranges.items())
    ) or "未设置"


def metrics_label(value: str) -> str:
    return {"share": "仅转发数", "all": "全部互动数"}[value]


def next_run_text(config: dict[str, Any]) -> str:
    if not config.get("enabled") or not config.get("times"):
        return "未启用"
    now = datetime.now()
    candidates: list[tuple[datetime, str]] = []
    for day_offset in (0, 1):
        day = now.date() + timedelta(days=day_offset)
        for value in config["times"]:
            hour, minute = map(int, value.split(":"))
            candidate = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
            if candidate > now:
                candidates.append((candidate, value))
    if not candidates:
        return "未设置"
    next_time, scheduled_time = min(candidates, key=lambda item: item[0])
    scheduled_range = config.get("schedule_ranges", {}).get(
        scheduled_time, config.get("scan_range", DEFAULT_CONFIG["scan_range"])
    )
    # 下次执行同时展示范围，避免用户只看到时间却不知道会不会补采昨天。
    return f"{next_time:%Y-%m-%d %H:%M} · {scan_range_label(scheduled_range)}"


STATE = ControlState()


def scheduler_loop() -> None:
    while True:
        now = datetime.now()
        minute = now.strftime("%H:%M")
        key = now.strftime("%Y-%m-%d %H:%M")
        with STATE.lock:
            config = dict(STATE.config)
            already_triggered = STATE.last_schedule_key == key
        if config.get("enabled") and minute in config.get("times", []) and not already_triggered:
            with STATE.lock:
                STATE.last_schedule_key = key
            selected_range = config.get("schedule_ranges", {}).get(
                minute, config.get("scan_range", DEFAULT_CONFIG["scan_range"])
            )
            # 定时任务必须传入当前时间对应的范围，禁止再退回为一个全局默认值。
            started, message = STATE.start_job("scheduled", scan_range=selected_range)
            if not started:
                STATE.add_log("warning", f"定时任务未启动：{message}。")
        time.sleep(5)


class ControlHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def end_headers(self) -> None:
        """让本地控制台在刷新后读取最新的 HTML、JS 和样式。

        采集控制台常在同一端口迭代运行；静态资源若被浏览器长期缓存，
        用户会看到旧界面却以为新功能没有生效。API 与下载接口已经各自
        设置了 no-store，因此这里只处理静态文件。
        """
        if not urlparse(self.path).path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache, max-age=0, must-revalidate")
        super().end_headers()

    def _has_control_access(self) -> bool:
        """校验控制台的 HTTP Basic 凭据；团队阅读页不会触发此校验。"""
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return False
        return hmac.compare_digest(username, CONTROL_PANEL_USERNAME) and hmac.compare_digest(password, CONTROL_PANEL_PASSWORD)

    def _request_control_auth(self) -> None:
        """向访问私有控制台的未认证请求发起浏览器登录挑战。"""
        body = "Administrator authentication required.".encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        # Basic realm 按 HTTP 头规范使用 ASCII，避免中文值导致服务端直接断开连接。
        self.send_header("WWW-Authenticate", 'Basic realm="wechat-rpa-control", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, default=json_serialization_default
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # 浏览器主动取消等待时无需记录为控制台故障，后台恢复动作仍会继续完成。
            return

    def _download(self, content: str, filename: str, content_type: str) -> None:
        """返回下载文件；导出接口不会对 MongoDB 或本地配置产生任何写入。"""
        body = content.encode("utf-8")
        # HTTP 响应头不能直接放中文。ASCII 回退名兼容旧客户端，RFC 5987 名称保证下载后仍显示中文。
        fallback_name = "account-config.csv" if filename.endswith(".csv") else "account-config.json"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename={fallback_name}; filename*=UTF-8''{quote(filename)}",
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if requires_control_auth(parsed.path) and not self._has_control_access():
            self._request_control_auth()
            return
        if parsed.path == "/api/accounts/export":
            query = parse_qs(parsed.query)
            export_format = str(query.get("format", ["csv"])[0]).lower()
            try:
                rows = account_export_rows()
                if export_format == "csv":
                    self._download(
                        serialize_account_export_csv(rows),
                        "公众号配置.csv",
                        "text/csv",
                    )
                elif export_format == "json":
                    self._download(
                        json.dumps({"items": rows}, ensure_ascii=False, indent=2),
                        "公众号配置.json",
                        "application/json",
                    )
                else:
                    raise ValueError("导出格式仅支持 csv 或 json")
            except PyMongoError as exc:
                # 数据库连接/认证异常也必须返回 JSON，前端才能展示可处理的原因。
                self._json({"ok": False, "message": f"MongoDB 暂时不可用：{exc}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            except (ValueError, OSError) as exc:
                self._json({"ok": False, "message": f"导出公众号失败：{exc}"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/accounts":
            query = parse_qs(parsed.query)
            try:
                result = list_accounts(
                    query=str(query.get("q", [""])[0]),
                    status=str(query.get("status", ["all"])[0]),
                    category=str(query.get("category", [""])[0]),
                    limit=min(100, max(1, int(query.get("limit", ["50"])[0]))),
                    offset=max(0, int(query.get("offset", ["0"])[0])),
                )
                self._json({"ok": True, **result})
            except PyMongoError as exc:
                self._json({"ok": False, "message": f"MongoDB 暂时不可用：{exc}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            except (ValueError, OSError) as exc:
                self._json({"ok": False, "message": f"读取公众号失败：{exc}"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/daily-report":
            query = parse_qs(parsed.query)
            try:
                report_date = str(query.get("date", [beijing_today().isoformat()])[0])
                result = daily_report(
                    report_date=report_date,
                    category=str(query.get("category", ["all"])[0]),
                    account=str(query.get("account", [""])[0]),
                )
                self._json({"ok": True, **result})
            except PyMongoError as exc:
                self._json({"ok": False, "message": f"MongoDB 暂时不可用：{exc}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            except (ValueError, OSError) as exc:
                self._json({"ok": False, "message": f"读取日报失败：{exc}"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/daily-briefing":
            query = parse_qs(parsed.query)
            try:
                issue_date = str(query.get("date", [beijing_today().isoformat()])[0])
                self._json({"ok": True, **daily_briefing(issue_date=issue_date)})
            except PyMongoError as exc:
                self._json({"ok": False, "message": f"MongoDB 暂时不可用：{exc}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            except (ValueError, OSError) as exc:
                self._json({"ok": False, "message": f"读取每日新闻失败：{exc}"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/articles":
            query = parse_qs(parsed.query)
            try:
                result = list_articles(
                    date_filter=str(query.get("date", ["today"])[0]),
                    account=str(query.get("account", [""])[0]),
                    query=str(query.get("q", [""])[0]),
                    sort=str(query.get("sort", ["publish_desc"])[0]),
                    minimum_share=(
                        int(query["min_share"][0])
                        if query.get("min_share", [""])[0].strip()
                        else None
                    ),
                    limit=min(100, max(1, int(query.get("limit", ["30"])[0]))),
                    offset=max(0, int(query.get("offset", ["0"])[0])),
                )
                self._json({"ok": True, **result})
            except PyMongoError as exc:
                self._json({"ok": False, "message": f"MongoDB 暂时不可用：{exc}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            except (ValueError, OSError) as exc:
                self._json({"ok": False, "message": f"读取文章失败：{exc}"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/articles/export":
            query = parse_qs(parsed.query)
            try:
                export_format = str(query.get("format", ["csv"])[0]).lower()
                minimum_share_text = str(query.get("min_share", [""])[0]).strip()
                rows = article_export_rows(
                    date_filter=str(query.get("date", ["all"])[0]),
                    account=str(query.get("account", [""])[0]),
                    query=str(query.get("q", [""])[0]),
                    sort=str(query.get("sort", ["publish_desc"])[0]),
                    minimum_share=int(minimum_share_text) if minimum_share_text else None,
                )
                if export_format == "csv":
                    self._download(serialize_article_export_csv(rows), "公众号文章.csv", "text/csv")
                elif export_format == "json":
                    self._download(
                        json.dumps({"count": len(rows), "items": rows}, ensure_ascii=False, indent=2),
                        "公众号文章.json",
                        "application/json",
                    )
                else:
                    raise ValueError("导出格式仅支持 csv 或 json")
            except PyMongoError as exc:
                self._json({"ok": False, "message": f"MongoDB 暂时不可用：{exc}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            except (ValueError, OSError) as exc:
                self._json({"ok": False, "message": f"导出文章失败：{exc}"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/api/articles/"):
            article_id = parsed.path.removeprefix("/api/articles/").strip()
            try:
                item = get_article_detail(article_id)
                if item is None:
                    self._json({"ok": False, "message": "未找到文章"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json({"ok": True, "item": item})
            except PyMongoError as exc:
                self._json({"ok": False, "message": f"MongoDB 暂时不可用：{exc}"}, HTTPStatus.SERVICE_UNAVAILABLE)
            except (ValueError, OSError) as exc:
                self._json({"ok": False, "message": f"读取文章详情失败：{exc}"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/status":
            self._json(STATE.status())
            return
        if parsed.path == "/api/preflight":
            self._json(collect_preflight())
            return
        if parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            try:
                since = max(0, int(query.get("since", ["0"])[0]))
            except ValueError:
                self._json({"ok": False, "message": "since 必须是非负整数"}, HTTPStatus.BAD_REQUEST)
                return
            with STATE.lock:
                items = [item for item in STATE.logs if item["id"] > since]
            self._json({"items": items})
            return
        if parsed.path == "/api/runs":
            query = parse_qs(parsed.query)
            try:
                limit = min(50, max(1, int(query.get("limit", ["20"])[0])))
            except ValueError:
                self._json({"ok": False, "message": "limit 必须是 1 到 50 的整数"}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"items": STATE.history.recent(limit)})
            return
        if parsed.path.startswith("/api/runs/"):
            run_id = parsed.path.removeprefix("/api/runs/").strip()
            if not run_id:
                self._json({"ok": False, "message": "缺少 run_id"}, HTTPStatus.BAD_REQUEST)
                return
            record = STATE.history.get(run_id)
            if record is None:
                self._json({"ok": False, "message": "未找到任务记录"}, HTTPStatus.NOT_FOUND)
                return
            self._json({"item": record})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        # 采集启动、停止、配置导入等写操作只允许管理员执行。
        if not self._has_control_access():
            self._request_control_auth()
            return
        try:
            payload = self._payload()
            if self.path == "/api/accounts/import/preview":
                rows = parse_account_import(
                    str(payload.get("content") or ""), str(payload.get("format") or "").lower()
                )
                self._json({"ok": True, **preview_account_import(rows)})
                return
            if self.path == "/api/accounts/import/apply":
                rows = parse_account_import(
                    str(payload.get("content") or ""), str(payload.get("format") or "").lower()
                )
                preview = preview_account_import(rows)
                if preview["summary"]["error"]:
                    raise ValueError("导入预览存在错误，请修正文件后再保存")
                saved = [upsert_account_config(item["item"]) for item in preview["items"]]
                self._json({"ok": True, "saved": len(saved), "message": f"已保存 {len(saved)} 个公众号配置"})
                return
            if self.path == "/api/accounts/upsert":
                result = upsert_account_config(payload)
                self._json({"ok": True, **result, "message": "公众号配置已保存"})
                return
            if self.path == "/api/preflight/recover":
                self._json(recover_sogou_preflight())
                return
            if self.path == "/api/run":
                source = str(payload.get("source") or "manual")
                requested_scan_range = payload.get("scan_range")
                requested_metrics = payload.get("metrics")
                # 手动和“立即测试”必须显式携带选择值，不能静默回退为定时计划默认值。
                if source in {"manual", "scheduled-test"}:
                    if requested_scan_range not in SCAN_RANGE_VALUES:
                        raise ValueError("手动任务必须明确选择扫描范围")
                    if requested_metrics not in METRIC_VALUES:
                        raise ValueError("手动任务必须明确选择采集指标")
                STATE.add_log(
                    "info",
                    f"收到{source}启动请求：范围={requested_scan_range or '使用定时默认值'}，"
                    f"指标={requested_metrics or '使用定时默认值'}，"
                    f"每账号上限={payload.get('max_articles') or '使用定时默认值'}。",
                )
                started, message = STATE.start_job(
                    source,
                    payload.get("max_articles"),
                    requested_scan_range,
                    requested_metrics,
                )
                self._json(
                    {
                        "ok": started,
                        "message": message,
                        "run_options": dict(STATE.last_run_options) if started else {},
                    },
                    200 if started else 409,
                )
                return
            if self.path == "/api/stop":
                stopped, message = STATE.stop_job()
                self._json({"ok": stopped, "message": message}, 200 if stopped else 409)
                return
            if self.path == "/api/config":
                config = STATE.save_config(payload)
                self._json({"ok": True, "config": config})
                return
            self._json({"ok": False, "message": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"ok": False, "message": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            STATE.add_log("error", f"控制台接口错误：{exc}")
            self._json({"ok": False, "message": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), ControlHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"公众号采集控制台已启动：{url}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n控制台已停止。")
    finally:
        if STATE.running():
            STATE.stop_job()
        server.server_close()


if __name__ == "__main__":
    main()
