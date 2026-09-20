"""mitmdump 插件：只保存公众号文章的脱敏转发统计，不管理系统代理/证书。"""
import os
import json
import time
from pathlib import Path

from network_metrics import article_identity, build_snapshot, save_snapshot


def diagnostic(event, **fields):
    directory = os.getenv("WECHAT_CAPTURE_CACHE_DIR", "").strip()
    if not directory:
        return
    try:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        log = path / "capture-events.jsonl"
        # 只保留事件和公开文章标识，不记录 URL、请求头、Cookie 或异常原文。
        if log.exists() and log.stat().st_size > 2_000_000:
            log.replace(path / "capture-events.previous.jsonl")
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": time.time(), "event": event, **fields}) + "\n")
    except OSError:
        pass


def running():
    diagnostic("capture_started")


def request(flow):
    identity = article_identity(flow.request.url)
    if identity:
        diagnostic("article_request", article_id=identity)


def tls_failed_client(data):
    if data.conn.sni == "mp.weixin.qq.com":
        diagnostic("article_tls_failed")


def _capture(flow, *, failed=False):
    directory = os.getenv("WECHAT_CAPTURE_CACHE_DIR")
    identity = article_identity(flow.request.url)
    if not directory or not identity:
        return
    stamp = flow.request.timestamp_start
    # 非成功响应也写失效标记，避免当前请求失败后继续使用旧成功结果。
    record = {"schema": 1, "valid": False, "article_id": identity, "captured_at": stamp}
    try:
        if not failed and flow.response.status_code == 200 and "text/html" in flow.response.headers.get("content-type", "").lower():
            record = build_snapshot(flow.request.url, flow.response.get_text(strict=False), stamp)
    except (ValueError, TypeError, KeyError):
        pass
    try:
        save_snapshot(Path(directory), record)
        diagnostic("article_error" if failed else "article_response", article_id=identity,
                   valid=record["valid"], status=None if failed else flow.response.status_code)
    except OSError:
        pass  # 缓存故障不影响微信正常联网，采集器会自行回退。


def response(flow):
    _capture(flow)


def error(flow):
    # 连接中断同样使本次文章缓存失效，不能继续读取上一轮成功结果。
    _capture(flow, failed=True)

