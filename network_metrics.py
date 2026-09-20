"""脱敏的网络转发数缓存；无法证明文章身份时拒绝使用。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def article_identity(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "mp.weixin.qq.com" or parts.port not in (None, 443):
            return None
        if parts.username or parts.password:
            return None
        if re.fullmatch(r"/s/[A-Za-z0-9_-]+", parts.path):
            return "short:" + parts.path[3:]
        query = parse_qs(parts.query, keep_blank_values=True)
        keys = ("__biz", "mid", "idx")
        if parts.path != "/s" or any(len(query.get(k, [])) != 1 or not query[k][0] for k in keys):
            return None
        if not query["mid"][0].isdigit() or not query["idx"][0].isdigit():
            return None
        # 仅由公开文章标识计算哈希，丢弃 key、uin、pass_ticket 等参数。
        return "article:" + hashlib.sha256(json.dumps([query[k][0] for k in keys]).encode()).hexdigest()
    except (ValueError, TypeError):
        return None


def build_snapshot(url: str, document: str, captured_at: float) -> dict:
    from article_ingest import parse_page_html
    from bs4 import BeautifulSoup

    identity = article_identity(url)
    if not identity:
        raise ValueError("unsupported_article_url")
    page = parse_page_html(document)
    if not page["account_name"] or not page["publish_time"]:
        raise ValueError("missing_article_identity")
    # 响应中同一统计可能出现多次。禁止取最后一个值，避免关联推荐内容的数据混入。
    # 正文中展示的代码不是页面统计，必须排除文章内容区域。
    soup = BeautifulSoup(document, "html.parser")
    # 微信桌面请求长链接，但“复制链接”通常返回 og:url 中的短链接。
    aliases = {article_identity(node.get("content", "")) for node in soup.select('meta[property="og:url"]')}
    aliases.discard(None)
    if len(aliases) > 1:
        raise ValueError("conflicting_article_alias")
    aliases = sorted(value for value in aliases if value.startswith("short:") and value != identity)
    for content in soup.select("#js_content"):
        content.decompose()
    scripts = "\n".join(node.get_text() for node in soup.find_all("script"))
    tokens = re.findall(r'''(?<![\w])['"]?share_count['"]?\s*:\s*([^,;}\s]+)''', scripts)
    if not tokens or any(not re.fullmatch(r'''(?:\d+|'\d+'|"\d+")''', t) for t in tokens):
        raise ValueError("missing_or_invalid_share_count")
    values = {int(t.strip("\"'")) for t in tokens}
    if len(values) != 1:
        raise ValueError("conflicting_share_count")
    return {"schema": 1, "valid": True, "article_id": identity, "alias_ids": aliases, "captured_at": captured_at,
            "title": page["title"], "account_name": page["account_name"],
            "publish_time": page["publish_time"], "share_count": values.pop()}


def page_article_identity(url: str, document: str) -> str | None:
    """短链接不一定发生 HTTP 重定向，从页面明确的公开标识构造长链接身份。"""
    from bs4 import BeautifulSoup
    from urllib.parse import urlencode

    original = article_identity(url)
    if not original or not original.startswith("short:"):
        return original
    soup = BeautifulSoup(document, "html.parser")
    for node in soup.select("#js_content"):
        node.decompose()
    scripts = "\n".join(node.get_text() for node in soup.find_all("script"))
    identifiers = {}
    for key in ("biz", "mid", "idx"):
        expressions = re.findall(r"\bwindow\." + key + r"\s*=\s*([^;]+);", scripts)
        window_assignment = bool(expressions)
        if window_assignment and len(expressions) != 1:
            return original
        if not expressions:
            expressions = re.findall(r"\bvar\s+" + key + r"\s*=\s*([^;]+);", scripts)
        resolved_values = set()
        for expression in expressions:
            # 只解析字符串常量组成的 || 链，绝不执行页面 JavaScript。
            parts = re.split(r"\s*\|\|\s*", expression.strip())
            if any(not re.fullmatch(r'''(?:'[A-Za-z0-9_+/=-]*'|"[A-Za-z0-9_+/=-]*")''', part) for part in parts):
                if window_assignment:
                    return original
                continue  # 普通文章打包函数中的同名局部变量不属于文章常量。
            values = [part[1:-1] for part in parts]
            resolved_values.add(next((v for v in values if v), ""))
        if len(resolved_values) != 1:
            return original
        identifiers["__biz" if key == "biz" else key] = resolved_values.pop()
    return article_identity("https://mp.weixin.qq.com/s?" + urlencode(identifiers)) or original


def snapshot_path(directory: Path, identity: str) -> Path:
    return directory / (hashlib.sha256(identity.encode()).hexdigest() + ".json")


def save_snapshot(directory: Path, snapshot: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    aliases = set(snapshot.get("alias_ids", []))
    try:
        previous = json.loads(snapshot_path(directory, snapshot["article_id"]).read_text(encoding="utf-8"))
        aliases.update(previous.get("alias_ids", []))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    aliases = sorted(a for a in aliases if isinstance(a, str) and re.fullmatch(r"short:[A-Za-z0-9_-]+", a))
    snapshot = {**snapshot, "alias_ids": aliases}
    _save_one(directory, snapshot)
    # 请求失败时同步使已知短链接失效，避免短链接缓存仍然命中旧统计。
    for alias in aliases:
        _save_one(directory, {**snapshot, "article_id": alias})


def _save_one(directory: Path, snapshot: dict) -> None:
    path = snapshot_path(directory, snapshot["article_id"])
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous.get("captured_at", 0) > snapshot["captured_at"]:
            return  # 较早请求的迟到响应不能覆盖较新的统计或失效标记。
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_snapshot(directory: str, page: dict, *, now: float | None = None, max_age: float = 120) -> tuple[dict | None, str]:
    identity = page.get("network_article_id")
    if not identity:
        return None, "missing_article_identity"
    try:
        path = snapshot_path(Path(directory), identity)
        if path.stat().st_size > 65536:
            return None, "invalid_cache"
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("schema") != 1 or record.get("valid") is not True or record.get("article_id") != identity:
            return None, "invalid_cache"
        timestamp = record.get("captured_at")
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
            return None, "invalid_timestamp"
        age = (time.time() if now is None else now) - timestamp
        if age < 0 or age > max_age:
            return None, "expired_cache"
        if any(not page.get(k) or record.get(k) != page[k] for k in ("title", "account_name", "publish_time")):
            return None, "identity_mismatch"
        if type(record.get("share_count")) is not int or record["share_count"] < 0:
            return None, "invalid_share_count"
        return record, "hit"
    except (OSError, ValueError, TypeError, AttributeError):
        return None, "unavailable_cache"

