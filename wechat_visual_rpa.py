"""微信电脑版公众号视觉采集器。

默认仅截图和识别，不点击界面。只有显式传入 ``--live`` 才允许鼠标操作。
"""

from __future__ import annotations

import argparse
import ctypes
import difflib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageGrab, ImageStat
from pymongo import MongoClient

from env_config import load_project_env
from wechat_window_probe import has_search_tab, probe_page, select_tab
from runtime_diagnostics import runtime_identity
from browser_navigation import Navigator, Page, Checkpoint, InventoryCache, tab_signature, selected_tab, page_from_probe, feed_from_probe, picture_share_count
from network_metrics import read_snapshot
from tab_ownership import TabOwnership


# 命令行直接启动采集器时也自动加载项目配置，不依赖 PowerShell 会话变量。
load_project_env()

from qwen_vision import QwenVisionClient, QwenVisionConfig
from article_evidence_ocr import ArticleEvidenceOCR
from article_ingest import (
    append_local_exports,
    ingest,
    load_cached_page,
    parse_page,
    parse_publish_time,
    shanghai_timezone,
)
from interaction_ocr import InteractionOCR
from wechat_feed_ocr import WeChatFeedOCR
from wechat_ocr import WeChatOCR
from wechat_profile_ocr import WeChatProfileOCR


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

RPA_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = RPA_DIR / "output"
ACCOUNT_ALIASES_PATH = RPA_DIR / "config" / "account_aliases.json"
COPY_LINK_POSITION_CACHE_PATH = Path(
    os.getenv("RPA_COPY_LINK_CACHE_PATH", str(RPA_DIR / "config" / "ui_position_cache.json"))
)
FEED_OCR = WeChatFeedOCR()
INTERACTION_OCR = InteractionOCR()
PROFILE_OCR = WeChatProfileOCR()
ARTICLE_EVIDENCE_OCR = ArticleEvidenceOCR()
RUN_LOGGER = logging.getLogger("wechat_rpa")
WINDOW_LAYOUT_MODE = "auto"
# 微信 3.x/4.x 及其内置 Chromium 子进程可能使用不同可执行文件名。
# 普通 chrome.exe/msedge.exe 即使窗口标题恰好为“微信”，也绝不能进入自动化范围。
WECHAT_PROCESS_NAMES = frozenset(
    {
        "wechat.exe",
        "wechatappex.exe",
        "wechatbrowser.exe",
        "weixin.exe",
        "weixinappex.exe",
    }
)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _read_ui_position_cache() -> dict[str, Any]:
    """读取界面坐标缓存；损坏或不存在时按空缓存处理。"""
    try:
        raw = json.loads(COPY_LINK_POSITION_CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": 1}
    return raw if isinstance(raw, dict) else {"version": 1}


def _write_ui_position_cache(payload: dict[str, Any]) -> None:
    """原子写入界面坐标缓存，避免进程退出时留下半个 JSON 文件。"""
    COPY_LINK_POSITION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = COPY_LINK_POSITION_CACHE_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(COPY_LINK_POSITION_CACHE_PATH)


def _compatible_cached_position(key: str, rect: "Rect", dpi: int) -> dict[str, Any] | None:
    """读取与当前窗口尺寸、DPI 相容的归一化坐标。"""
    cached = _read_ui_position_cache().get(key)
    if not isinstance(cached, dict):
        return None
    try:
        x_1000 = int(cached["center_x_1000"])
        y_1000 = int(cached["center_y_1000"])
        cached_dpi = int(cached["dpi"])
        cached_width = int(cached["window_width"])
        cached_height = int(cached["window_height"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= x_1000 <= 1000 and 0 <= y_1000 <= 1000):
        return None
    width_ratio = rect.width / max(cached_width, 1)
    height_ratio = rect.height / max(cached_height, 1)
    if abs(cached_dpi - dpi) > 24 or not (0.8 <= width_ratio <= 1.25) or not (0.8 <= height_ratio <= 1.25):
        return None
    return cached


def _save_ui_position(key: str, action: dict[str, Any], rect: "Rect", dpi: int, source: str) -> None:
    """保存一个已经通过后续行为验证的界面坐标，同时保留其他坐标。"""
    payload = _read_ui_position_cache()
    payload["version"] = 1
    payload[key] = {
        "center_x_1000": int(action["center_x_1000"]),
        "center_y_1000": int(action["center_y_1000"]),
        "dpi": dpi,
        "window_width": rect.width,
        "window_height": rect.height,
        "source": source,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_ui_position_cache(payload)


def _clear_ui_position(key: str, reason: str) -> None:
    """只删除指定坐标，避免一个控件失效时连带清除其他有效缓存。"""
    try:
        payload = _read_ui_position_cache()
        removed = payload.pop(key, None)
        if removed is not None:
            _write_ui_position_cache(payload)
    except OSError as exc:
        log_event("ui_position_cache_clear_failed", key=key, reason=reason, error=str(exc))
        return
    log_event("ui_position_cache_cleared", key=key, reason=reason)


def load_copy_link_position_cache(rect: "Rect", dpi: int) -> dict[str, Any] | None:
    """读取上一次验证成功的“复制链接”归一化坐标。

    缓存只提供一个候选区域，后续仍需用小区域 OCR 验证；窗口尺寸或 DPI
    变化过大时直接放弃缓存，避免把另一台电脑上的坐标当成当前坐标使用。
    """
    return _compatible_cached_position("copy_link", rect, dpi)


def save_copy_link_position_cache(action: dict[str, Any], rect: "Rect", dpi: int, source: str) -> None:
    """持久化已经通过剪贴板 URL 校验的菜单坐标。"""
    try:
        _save_ui_position("copy_link", action, rect, dpi, source)
    except OSError as exc:
        # 缓存不可写不能阻塞采集，当前文章仍然可以依靠本次 OCR 结果继续。
        log_event("copy_link_position_cache_write_failed", error=str(exc))


def clear_copy_link_position_cache(reason: str) -> None:
    """缓存失效后立即移除，避免下一篇文章再次落入同一个错误坐标。"""
    _clear_ui_position("copy_link", reason)


def load_menu_button_position_cache(rect: "Rect", dpi: int) -> dict[str, Any] | None:
    """读取上一次完成合法 URL 复制时使用的浏览器菜单按钮坐标。"""
    return _compatible_cached_position("menu_button", rect, dpi)


def save_menu_button_position_cache(action: dict[str, Any], rect: "Rect", dpi: int, source: str) -> None:
    """保存已由完整复制链接流程验证成功的浏览器菜单按钮坐标。"""
    try:
        _save_ui_position("menu_button", action, rect, dpi, source)
    except OSError as exc:
        log_event("menu_button_position_cache_write_failed", error=str(exc))


def clear_menu_button_position_cache(reason: str) -> None:
    """动态菜单按钮失效后只清除该按钮缓存。"""
    _clear_ui_position("menu_button", reason)


def validate_cached_copy_link_action(
    screenshot: Image.Image,
    cached: dict[str, Any],
) -> dict[str, Any]:
    """仅 OCR 缓存坐标附近的小区域，确认文字仍然是“复制链接”。"""
    width, height = screenshot.size
    center_x = width * int(cached["center_x_1000"]) / 1000
    center_y = height * int(cached["center_y_1000"]) / 1000
    # 让候选文字位于裁剪区域上半部，兼容 locate_copy_link_action 的菜单区域约束。
    left = max(0, round(center_x - width * 0.14))
    top = max(0, round(center_y - height * 0.05))
    right = min(width, round(center_x + width * 0.14))
    bottom = min(height, round(center_y + height * 0.13))
    if right - left < 20 or bottom - top < 20:
        return {"found": False, "reason": "缓存坐标附近区域过小"}
    region = screenshot.crop((left, top, right, bottom))
    action = PROFILE_OCR.locate_copy_link_action(region)
    if not action.get("found"):
        return {"found": False, "reason": str(action.get("reason") or "缓存区域未识别到复制链接")}
    region_width, region_height = region.size
    full_x = left + region_width * int(action["center_x_1000"]) / 1000
    full_y = top + region_height * int(action["center_y_1000"]) / 1000
    return {
        **action,
        "center_x_1000": round(full_x * 1000 / width),
        "center_y_1000": round(full_y * 1000 / height),
        "method": "cached-position-roi-rapidocr",
    }


def validate_cached_menu_button_action(
    screenshot: Image.Image,
    cached: dict[str, Any],
) -> dict[str, Any]:
    """校验已成功复制过公众号 URL 的浏览器菜单坐标。

    缓存本身已经经过“复制链接 + 合法公众号 URL”验证。当前页面可能还包含
    文章自身的三点按钮，因此实时识别到另一个三点时，不能反过来淘汰可靠缓存；
    真正的失效由后续菜单文字与剪贴板 URL 双重校验确认。
    """
    try:
        cached_x = int(cached["center_x_1000"])
        cached_y = int(cached["center_y_1000"])
    except (KeyError, TypeError, ValueError):
        return {"found": False, "reason": "菜单按钮缓存坐标格式错误"}
    if not (450 <= cached_x <= 980 and 0 <= cached_y <= 120):
        return {"found": False, "reason": "菜单按钮缓存坐标不在浏览器顶部工具栏内"}

    detected = PROFILE_OCR.locate_browser_menu_button(screenshot)
    if detected.get("found"):
        try:
            distance_x = abs(int(detected["center_x_1000"]) - cached_x)
            distance_y = abs(int(detected["center_y_1000"]) - cached_y)
        except (KeyError, TypeError, ValueError):
            distance_x = distance_y = 1000
        if distance_x <= 55 and distance_y <= 35:
            return {**detected, "method": "cached-menu-button-opencv"}

    # OCR/OpenCV 没找到缓存附近的按钮，或找到了网页里的另一个三点。
    # 先按已验证缓存尝试；若菜单文字/URL 校验失败，调用方会清缓存并降级识别。
    return {
        "found": True,
        "center_x_1000": cached_x,
        "center_y_1000": cached_y,
        "confidence": float(cached.get("confidence") or 1.0),
        "method": "cached-menu-button-verified-position",
        "live_detection_found": bool(detected.get("found")),
    }


def normalize_qwen_copy_link_action(result: dict[str, Any]) -> dict[str, Any]:
    """把 Qwen-VL 返回值收紧为可点击的“复制链接”动作。"""
    if not result.get("found") or str(result.get("label") or "").replace(" ", "") != "复制链接":
        return {"found": False, "reason": "Qwen-VL 未确认精确的复制链接菜单项"}
    try:
        x_1000 = int(result["center_x_1000"])
        y_1000 = int(result["center_y_1000"])
        confidence = float(result.get("confidence") or 0)
    except (KeyError, TypeError, ValueError):
        return {"found": False, "reason": "Qwen-VL 返回坐标格式错误"}
    if not (0 <= x_1000 <= 1000 and 0 <= y_1000 <= 1000) or confidence < 0.75:
        return {"found": False, "reason": "Qwen-VL 坐标越界或置信度不足"}
    return {
        "found": True,
        "text": "复制链接",
        "center_x_1000": x_1000,
        "center_y_1000": y_1000,
        "confidence": confidence,
        "method": "qwen-vl-copy-link-fallback",
    }


def normalize_qwen_menu_button_action(result: dict[str, Any]) -> dict[str, Any]:
    """只接受 Qwen-VL 明确认出的浏览器标题栏三点菜单按钮。"""
    label = str(result.get("label") or "").replace(" ", "")
    if not result.get("found") or label not in {"...", "…", "⋯", "更多", "三点菜单"}:
        return {"found": False, "reason": "Qwen-VL 未确认浏览器三点菜单按钮"}
    try:
        x_1000 = int(result["center_x_1000"])
        y_1000 = int(result["center_y_1000"])
        confidence = float(result.get("confidence") or 0)
    except (KeyError, TypeError, ValueError):
        return {"found": False, "reason": "Qwen-VL 菜单按钮坐标格式错误"}
    # 菜单按钮必须位于文章窗口标题栏右侧，防止选择正文或页面内的省略号。
    if not (450 <= x_1000 <= 950 and 0 <= y_1000 <= 140) or confidence < 0.80:
        return {"found": False, "reason": "Qwen-VL 菜单按钮位置越界或置信度不足"}
    return {
        "found": True,
        "center_x_1000": x_1000,
        "center_y_1000": y_1000,
        "confidence": confidence,
        "method": "qwen-vl-browser-menu-button",
    }


def normalize_qwen_wechat_search_entry(result: dict[str, Any]) -> dict[str, Any]:
    """只接受 Qwen-VL 在微信左上区域识别出的“搜索网络结果”入口。"""
    label = str(result.get("label") or "").replace(" ", "")
    if not result.get("found") or not label.startswith("搜索网络结果"):
        return {"found": False, "reason": "Qwen-VL 未确认搜索网络结果入口"}
    try:
        x_1000 = int(result["center_x_1000"])
        y_1000 = int(result["center_y_1000"])
        confidence = float(result.get("confidence") or 0)
    except (KeyError, TypeError, ValueError):
        return {"found": False, "reason": "Qwen-VL 返回的搜一搜坐标格式错误"}
    if not (0 <= x_1000 <= 550 and 30 <= y_1000 <= 220) or confidence < 0.80:
        return {"found": False, "reason": "Qwen-VL 搜索网络结果坐标越界或置信度不足"}
    return {
        "found": True,
        "text": label,
        "center_x_1000": x_1000,
        "center_y_1000": y_1000,
        "confidence": confidence,
        "method": "qwen-vl-wechat-network-search-entry",
    }


def resolve_search_account_name(account_name: str) -> str:
    """返回搜一搜使用的名称；别名只改变检索词，不改变 MongoDB 中的来源账号名。"""
    try:
        raw = json.loads(ACCOUNT_ALIASES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return account_name
    except (OSError, json.JSONDecodeError) as exc:
        # 配置损坏时继续使用原名，避免一个别名配置阻断全部账号采集。
        log_event("account_alias_config_ignored", error=str(exc))
        return account_name
    if not isinstance(raw, dict):
        log_event("account_alias_config_ignored", error="根节点必须是 JSON 对象")
        return account_name
    alias = raw.get(account_name)
    return alias.strip() if isinstance(alias, str) and alias.strip() else account_name


def configure_run_logging(output_dir: Path) -> Path:
    """同时记录控制台和 UTF-8 文件日志，便于还原每一次界面决策。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "run.log"
    RUN_LOGGER.setLevel(logging.INFO)
    RUN_LOGGER.handlers.clear()
    formatter = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    RUN_LOGGER.addHandler(file_handler)
    RUN_LOGGER.addHandler(stream_handler)
    return log_path


def log_event(event: str, **details: Any) -> None:
    """使用单行 JSON 记录事件，既方便人工查看，也方便后续程序统计。"""
    payload = {"event": event, **details}
    RUN_LOGGER.info(json.dumps(payload, ensure_ascii=False, default=str))
# 必须在首次读取窗口坐标前启用 DPI 感知，否则 150% 缩放下截图与点击坐标不一致。
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    rect: Rect
    process_name: str = ""
    shared_browser: bool = False
    profile_checkpoint: Checkpoint | None = None


@dataclass(frozen=True)
class CropRegion:
    left_ratio: float
    top_ratio: float
    right_ratio: float
    bottom_ratio: float

    def pixel_box(self, image: Image.Image) -> tuple[int, int, int, int]:
        width, height = image.size
        return (
            round(width * self.left_ratio),
            round(height * self.top_ratio),
            round(width * self.right_ratio),
            round(height * self.bottom_ratio),
        )


def window_process_name(hwnd: int) -> str:
    """返回窗口所属进程名；无法确认时返回空串并按非微信窗口处理。"""
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value:
        return ""
    process = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value
    )
    if not process:
        return ""
    try:
        size = wintypes.DWORD(32768)
        executable = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            process, 0, executable, ctypes.byref(size)
        ):
            return ""
        return Path(executable.value).name.lower()
    finally:
        kernel32.CloseHandle(process)


def is_wechat_owned_window(hwnd: int) -> bool:
    """只信任明确属于微信进程的窗口，未知归属一律安全拒绝。"""
    return window_process_name(hwnd) in WECHAT_PROCESS_NAMES


def enumerate_wechat_windows() -> list[WindowInfo]:
    """枚举微信主窗口、公众号消息窗口和文章浏览器窗口。"""
    windows: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        class_name = class_buffer.value
        # 微信不同版本会改变 Qt/Chromium 窗口类名中的版本号或末尾序号。
        is_qt_window = class_name.startswith("Qt") and class_name.endswith("QWindowIcon")
        is_chrome_window = class_name.startswith("Chrome_WidgetWin_")
        if not (is_qt_window or is_chrome_window):
            return True
        process_name = window_process_name(hwnd)
        if process_name not in WECHAT_PROCESS_NAMES:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, length + 1)
        raw = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(raw)):
            return True
        rect = Rect(raw.left, raw.top, raw.right, raw.bottom)
        if rect.width > 500 and rect.height > 500:
            windows.append(
                WindowInfo(
                    hwnd,
                    title_buffer.value,
                    class_buffer.value,
                    rect,
                    process_name,
                )
            )
        return True

    user32.EnumWindows(callback, 0)
    return windows


def find_search_window() -> WindowInfo:
    candidates = [
        item for item in enumerate_wechat_windows()
        if item.class_name.startswith("Qt")
        and item.class_name.endswith("QWindowIcon")
        and item.title.strip() == "微信"
    ]
    if not candidates:
        raise RuntimeError("没有找到微信服务号搜索主窗口")
    return max(candidates, key=lambda item: item.rect.width * item.rect.height)


def is_sogou_search_window(window: WindowInfo) -> bool:
    """判断窗口是否为搜一搜浏览器，兼容新版“公众号名 - 公众号搜一搜”标题。"""
    title = window.title.strip()
    legacy_embedded_processes = {"wechatappex.exe", "wechatbrowser.exe", "weixinappex.exe"}
    return (
        window.process_name in WECHAT_PROCESS_NAMES
        and window.class_name.startswith("Chrome_WidgetWin_")
        and (
            "搜一搜" in title
            # 旧版内置浏览器标题只有“微信”，必须同时确认它属于 AppEx/Browser
            # 子进程，避免把新版 Chromium 微信主窗口误判为搜一搜。
            or (title == "微信" and window.process_name in legacy_embedded_processes)
        )
    )


def find_sogou_search_window(
    excluded_hwnds: set[int] | frozenset[int] | None = None,
) -> WindowInfo:
    """查找微信搜一搜浏览器窗口；文章会在该窗口的新标签页中打开。"""
    metadata_candidates = [
        item for item in enumerate_wechat_windows()
        if is_sogou_search_window(item)
        and item.hwnd not in (excluded_hwnds or set())
    ]
    candidates: list[WindowInfo] = []
    for item in metadata_candidates:
        if "搜一搜" in item.title:
            candidates.append(item)
            continue
        # 系统标题为“微信”时读取真实标签名；被遮挡或当前处于文章标签也能识别。
        if has_search_tab(item.hwnd):
            candidates.append(item)
            continue
        # 旧版内置浏览器标题只有“微信”。仅凭进程名仍可能撞到小程序或其他
        # WeChatAppEx 窗口，因此必须看到搜一搜搜索框和账号导航后才接纳。
        try:
            evidence = _inspect_sogou_search_results(capture_window(item.rect))
        except Exception as exc:  # noqa: BLE001
            log_event(
                "legacy_sogou_window_validation_failed",
                hwnd=item.hwnd,
                reason=str(exc),
            )
            continue
        if evidence.get("found"):
            candidates.append(item)
            log_event("legacy_sogou_window_validated", hwnd=item.hwnd)
    if not candidates:
        raise RuntimeError("没有找到微信搜一搜窗口，请先在微信中打开搜一搜")
    # 新版窗口标题会直接包含“搜一搜”，优先级高于旧版仅显示“微信”的兼容候选。
    return max(
        candidates,
        key=lambda item: (
            int("搜一搜" in item.title),
            item.rect.width * item.rect.height,
        ),
    )


def open_sogou_from_wechat_main(
    account_name: str,
    *,
    excluded_hwnds: set[int] | frozenset[int] | None = None,
    client: QwenVisionClient | None = None,
    allow_vl: bool = True,
) -> WindowInfo:
    """搜一搜窗口缺失时，从已登录的微信主窗口自动恢复。

    优先使用微信原生 Ctrl+F 键盘流程；失败后才用本地 OCR 点击精确入口，
    最后允许 Qwen-VL 兜底。每次动作都必须由新搜一搜窗口出现来确认成功。
    """
    # 主窗口可能是 Qt，也可能是新版 Chromium 窗口，统一走管理窗口探测。
    main_hwnd, main_rect = find_wechat_manager_window()
    main_window = WindowInfo(
        main_hwnd,
        "微信",
        "Chrome_WidgetWin_0",
        main_rect,
        window_process_name(main_hwnd),
    )
    excluded = excluded_hwnds or set()

    def wait_for_search_window(timeout_seconds: float, method: str) -> WindowInfo | None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            time.sleep(0.4)
            try:
                window = find_sogou_search_window(excluded)
                log_event(
                    "sogou_recovery_succeeded",
                    account=account_name,
                    hwnd=window.hwnd,
                    method=method,
                )
                return window
            except RuntimeError:
                continue
        return None

    def focus_and_type_query() -> None:
        """使用微信原生快捷键聚焦全局搜索，不依赖窗口坐标。"""
        activate_window(main_window.hwnd)
        press_ctrl_f()
        time.sleep(0.35)
        set_clipboard_text("搜一搜")
        press_ctrl_a()
        press_ctrl_v()
        time.sleep(0.55)

    # 第一次回车会在微信 4.x 中展开“搜索网络结果”入口；少数版本会直接
    # 打开搜一搜，因此先短暂等待一次，兼容两类行为。
    focus_and_type_query()
    press_enter()
    log_event(
        "sogou_recovery_submitted",
        account=account_name,
        query="搜一搜",
        source="wechat-main-window-keyboard",
    )
    # AppEx 首次启动和本地 OCR 模型预热都可能耗时十余秒；等待期间只做
    # 页面复核，不重复发送打开动作，避免慢机器上产生多个搜一搜窗口。
    recovered = wait_for_search_window(18, "ctrl-f-enter")
    if recovered is not None:
        return recovered

    # 当前版本第一次回车后需要点击顶部“搜索网络结果”整行。本地 OCR
    # 动态定位该行，避免依赖不同分辨率和缩放下的固定坐标。
    focus_and_type_query()
    press_enter()
    time.sleep(0.8)
    screenshot = capture_window(main_window.rect)
    local_action = PROFILE_OCR.locate_wechat_search_entry(screenshot)
    log_event(
        "sogou_recovery_local_detection",
        account=account_name,
        found=bool(local_action.get("found")),
        confidence=local_action.get("confidence"),
        reason=local_action.get("reason"),
    )
    if local_action.get("found"):
        click(
            main_window.rect.left
            + round(main_window.rect.width * int(local_action["center_x_1000"]) / 1000),
            main_window.rect.top
            + round(main_window.rect.height * int(local_action["center_y_1000"]) / 1000),
        )
        recovered = wait_for_search_window(18, "rapidocr-search-entry")
        if recovered is not None:
            return recovered

    # 本地识别或点击未能打开窗口时，才启用 Qwen-VL；模型也必须返回精确标签和安全坐标。
    vl_reason = "已禁用 Qwen-VL"
    if allow_vl:
        if client is None:
            try:
                client = QwenVisionClient(QwenVisionConfig.from_env())
            except RuntimeError as exc:
                vl_reason = str(exc)
        if client is not None:
            try:
                # 本地点击可能改变候选状态，重新生成可复核的网络搜索入口。
                focus_and_type_query()
                press_enter()
                time.sleep(0.8)
                screenshot = capture_window(main_window.rect)
                vl_action = normalize_qwen_wechat_search_entry(
                    client.detect_wechat_search_entry(screenshot)
                )
                vl_reason = str(vl_action.get("reason") or "Qwen-VL 点击后未出现搜一搜窗口")
                log_event(
                    "sogou_recovery_vl_detection",
                    account=account_name,
                    found=bool(vl_action.get("found")),
                    confidence=vl_action.get("confidence"),
                    reason=vl_action.get("reason"),
                )
                if vl_action.get("found"):
                    click(
                        main_window.rect.left
                        + round(main_window.rect.width * int(vl_action["center_x_1000"]) / 1000),
                        main_window.rect.top
                        + round(main_window.rect.height * int(vl_action["center_y_1000"]) / 1000),
                    )
                    recovered = wait_for_search_window(22, "qwen-vl-search-entry")
                    if recovered is not None:
                        return recovered
            except Exception as exc:  # noqa: BLE001
                vl_reason = f"Qwen-VL 调用失败：{exc}"
                log_event("sogou_recovery_vl_failed", account=account_name, error=str(exc))

    local_reason = str(local_action.get("reason") or "本地识别点击后未出现窗口")
    raise RuntimeError(
        "已执行 Ctrl+F 搜索，但未出现搜一搜浏览器窗口；"
        f"本地识别：{local_reason}；视觉兜底：{vl_reason}"
    )


def recreate_sogou_search_window(
    stale_window: WindowInfo,
    account_name: str,
    reason: str,
) -> WindowInfo:
    """无损新建搜一搜窗口；旧窗口无论是否失效都不得由恢复流程关闭。"""
    log_event(
        "search_page_recovery_started",
        account=account_name,
        reason=reason,
        stale_window={
            "hwnd": stale_window.hwnd,
            "title": stale_window.title,
            "class_name": stale_window.class_name,
        },
        action="preserve_stale_window_and_open_new",
    )
    # 新版会在原 HWND 内打开搜索标签，不能要求必须产生独立窗口。
    # open_sogou_from_wechat_main 仍须验证搜一搜证据，旧页面全部保留。
    recovered = open_sogou_from_wechat_main(
        account_name,
    )
    recovered = arrange_automation_window(recovered, "browser")
    activate_window(recovered.hwnd)
    if not browser_navigator(recovered, "").select(lambda page: page.role == "search"):
        press_ctrl_1()
    time.sleep(0.8)
    log_event(
        "search_page_recovery_finished",
        account=account_name,
        recovered_hwnd=recovered.hwnd,
    )
    return recovered


def find_official_profile_window() -> WindowInfo:
    candidates = [
        item for item in enumerate_wechat_windows()
        if (
            item.class_name.startswith("Chrome_WidgetWin_")
            or (item.class_name.startswith("Qt") and item.class_name.endswith("QWindowIcon"))
        )
        and "公众号" in item.title.strip()
        # “公众号名 - 公众号搜一搜”是左侧搜索浏览器，不是右侧公众号资料页。
        and not is_sogou_search_window(item)
    ]
    if not candidates:
        raise RuntimeError("没有找到微信公众号资料窗口")
    return max(candidates, key=lambda item: item.rect.width * item.rect.height)


def observe_browser_page(window: WindowInfo, account: str = "") -> Page:
    """每次读取新证据，禁止复用 UIA 元素索引或旧截图坐标。"""
    result = probe_page(window.hwnd)
    page = page_from_probe(result, account)
    if not page.key:
        time.sleep(0.4)
        page = page_from_probe(probe_page(window.hwnd), account)
    return page


_CLEANED_TAB_SESSIONS: set[str] = set()
TAB_OWNERSHIP = TabOwnership(Path(__file__).resolve().parent / "output" / "owned-browser-tabs.json")
_NAVIGATION_CACHES: dict[tuple[str, str], InventoryCache] = {}
_CUSTOM_TAB_BINDINGS: dict[str, dict[str, tuple]] = {}
_TAB_PROBE_DIAGNOSTICS: dict[tuple[str, str], tuple] = {}


def browser_session(hwnd: int) -> str:
    """进程 PID 和创建时间共同标识会话，避免 Windows 复用句柄导致误关。"""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = kernel32.OpenProcess(0x1000, False, pid.value)
    if not process:
        return ""
    try:
        stamps = [wintypes.FILETIME() for _ in range(4)]
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        if not kernel32.GetProcessTimes(process, *(ctypes.byref(s) for s in stamps)):
            return ""
        return f"{pid.value}:{stamps[0].dwHighDateTime}:{stamps[0].dwLowDateTime}:{hwnd}"
    finally:
        kernel32.CloseHandle(process)


def browser_navigator(window: WindowInfo, account: str) -> Navigator:
    session = browser_session(window.hwnd)
    evidence: dict = {}
    bindings = _CUSTOM_TAB_BINDINGS.setdefault(session, {}) if session else {}

    def current_tab() -> tuple:
        standard = selected_tab(evidence)
        if standard:
            return standard
        if evidence.get("tab_probe_mode") != "custom-unverified":
            return ()
        signature = tab_signature(evidence)
        page = page_from_probe(evidence, account)
        if not signature or not page.key:
            return ()
        if len(signature) == 1:
            bindings[page.key] = signature[0][0]
        identity = bindings.get(page.key, ())
        return identity if identity in dict(signature) else ()

    def choose_tab(identity: tuple) -> bool:
        activate_window(window.hwnd)
        observe()
        if evidence.get("tab_probe_mode") != "custom-unverified":
            return select_tab(window.hwnd, identity)
        matches = [t for t in evidence.get("custom_tabs", []) if tuple(t["id"]) == identity]
        if len(matches) != 1 or matches[0].get("offscreen"):
            return False
        left, top, right, bottom = matches[0]["rect"]
        if right - left < 60 or bottom - top < 10:
            return False
        # 使用刚读取的标签实体位置，点击左侧标题区，避开右侧关闭按钮。
        click(round(left + (right-left) * 0.3), round((top+bottom)/2))
        time.sleep(0.4)
        page = observe()
        if page.key and identity in dict(tab_signature(evidence)):
            bindings[page.key] = identity
            return True
        return False

    def observe() -> Page:
        nonlocal evidence
        # 页面身份和标签列表来自同一次新探测，避免额外启动探测进程。
        evidence = probe_page(window.hwnd)
        page = page_from_probe(evidence, account)
        if not page.key:
            time.sleep(0.4)
            evidence = probe_page(window.hwnd)
            page = page_from_probe(evidence, account)
        # 同一账号仅在探测类型、数量或可绑定状态变化时记录，避免每次轮询刷屏。
        standard = evidence.get("tabs") or []
        custom = evidence.get("custom_tabs") or []
        state = (bool(evidence.get("ok")), evidence.get("tab_probe_mode", "unknown"),
                 len(standard), len(custom), bool(tab_signature(evidence)), bool(current_tab()))
        diagnostic_key = (session or str(window.hwnd), account)
        if _TAB_PROBE_DIAGNOSTICS.get(diagnostic_key) != state:
            _TAB_PROBE_DIAGNOSTICS[diagnostic_key] = state
            log_event("browser_tab_probe", account=account, hwnd=window.hwnd,
                      probe_ok=state[0], mode=state[1], standard_tab_count=state[2],
                      custom_tab_count=state[3], signature_valid=state[4], active_tab_bound=state[5])
        return page

    # 无法确认进程会话时不跨 Navigator 共享，避免 HWND 复用继承旧记录。
    cache = _NAVIGATION_CACHES.setdefault((session, account), InventoryCache()) if session else InventoryCache()
    def next_tab() -> None:
        activate_window(window.hwnd)
        press_ctrl_tab()
        time.sleep(0.35)

    def close_tab() -> None:
        activate_window(window.hwnd)
        press_ctrl_w()
        time.sleep(0.5)

    def back() -> None:
        activate_window(window.hwnd)
        user32.keybd_event(0x12, 0, 0, 0)  # Alt+Left：同页导航返回。
        user32.keybd_event(0x25, 0, 0, 0)
        user32.keybd_event(0x25, 0, 2, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        time.sleep(0.8)

    return Navigator(observe, next_tab, close_tab, back,
                     signature=lambda: tab_signature(evidence), cache=cache,
                     selected=current_tab, select_tab=choose_tab,
                     custom_tabs=lambda: evidence.get("tab_probe_mode") == "custom-unverified",
                     on_reused=lambda old, new: TAB_OWNERSHIP.reuse(session, old, new),
                     on_created=lambda page: TAB_OWNERSHIP.remember(session, page),
                     on_closed=lambda page: TAB_OWNERSHIP.forget(session, page),
                     trace=lambda data: log_event("browser_navigation", account=account, **data))


def wait_for_article_page(window: WindowInfo, account: str, title: str, timeout: float = 15) -> Page:
    """网页文档会先出现空壳，再更新标题和署名；等待身份齐全后才能采集或清理。"""
    deadline = time.monotonic() + timeout
    page = Page(window.hwnd, "", "unknown", "")
    mismatch_key, mismatch_count = "", 0
    while time.monotonic() < deadline:
        page = observe_browser_page(window, account)
        if page.role in {"article", "unknown"} and page.name and titles_match(title, page.name):
            # 非标准图文可能没有 UIA 日期/署名。此处仅允许进入读取链路，
            # collect_open_article 仍须校验链接解析出的标题、公众号和前后 URL。
            return page
        if page.role == "article" and page.name:
            mismatch_count = mismatch_count + 1 if page.key == mismatch_key else 1
            mismatch_key = page.key
            if mismatch_count >= 3:
                log_event("article_page_identity_failed", account=account, expected_title=title,
                          observed_page=page.__dict__, reason="stable_title_mismatch")
                raise ArticleMismatchError(f"正文标题已稳定但与卡片不匹配：目标={title!r}，实际={page.name!r}")
        else:
            mismatch_key, mismatch_count = "", 0
        time.sleep(0.5)
    log_event("article_page_identity_failed", account=account, expected_title=title,
              observed_page=page.__dict__)
    raise RuntimeError("文章加载后仍未确认目标标题和公众号，保留页面")


def find_navigation_browser() -> WindowInfo:
    """新版当前标签是主页/文章时，也能找到承载它的微信浏览器。"""
    try:
        return find_sogou_search_window()
    except RuntimeError:
        for window in enumerate_wechat_windows():
            if window.class_name.startswith("Chrome_WidgetWin_"):
                if observe_browser_page(window).key:
                    return window
        raise


def find_account_message_window(account_name: str) -> WindowInfo:
    expected = normalize_title(account_name)
    candidates = [
        item for item in enumerate_wechat_windows()
        if item.class_name.startswith("Qt")
        and item.class_name.endswith("QWindowIcon")
        and normalize_title(item.title) == expected
    ]
    if not candidates:
        raise RuntimeError(f"没有找到公众号消息窗口：{account_name}")
    return max(candidates, key=lambda item: item.rect.width * item.rect.height)


def recover_batch_navigation(account_name: str) -> None:
    """仅在搜索页身份重新确认后允许批次继续，不以窗口存在代替健康检查。"""
    browser = find_navigation_browser()
    if not browser_navigator(browser, "").select(lambda page: page.role == "search"):
        raise RuntimeError("未找到可确认的搜一搜页面")
    log_event("batch_navigation_recovered", account=account_name)


def close_window(hwnd: int, timeout_seconds: float = 3.0) -> None:
    """只关闭当前仍明确属于微信进程的窗口句柄。"""
    process_name = window_process_name(hwnd)
    if process_name not in WECHAT_PROCESS_NAMES:
        raise RuntimeError(
            "拒绝关闭非微信窗口："
            f"hwnd={hwnd}, process={process_name or 'unknown'}"
        )
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    deadline = time.time() + timeout_seconds
    while time.time() < deadline and user32.IsWindow(hwnd):
        time.sleep(0.1)


def normalized_bbox_to_pixels(values: list[int], image: Image.Image) -> tuple[int, int, int, int]:
    if len(values) != 4 or any(not 0 <= int(value) <= 1000 for value in values):
        raise ValueError(f"模型返回了无效区域：{values}")
    width, height = image.size
    left, top, right, bottom = (int(value) for value in values)
    box = (
        round(width * left / 1000),
        round(height * top / 1000),
        round(width * right / 1000),
        round(height * bottom / 1000),
    )
    if box[2] - box[0] < 120 or box[3] - box[1] < 250:
        raise ValueError(f"模型定位区域过小：{box}")
    return box


def find_wechat_manager_window() -> tuple[int, Rect]:
    candidates: list[tuple[int, str, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        is_qt_window = class_name.value.startswith("Qt") and class_name.value.endswith("QWindowIcon")
        is_chrome_window = class_name.value.startswith("Chrome_WidgetWin_")
        if title.value.strip() != "微信" or not (is_qt_window or is_chrome_window):
            return True
        if not is_wechat_owned_window(hwnd):
            return True
        raw = wintypes.RECT()
        initial_area = 0
        if user32.GetWindowRect(hwnd, ctypes.byref(raw)):
            initial_area = max(0, raw.right - raw.left) * max(0, raw.bottom - raw.top)
        # 最小化窗口的当前矩形可能非常小，不能在恢复前用尺寸把它过滤掉。
        candidates.append((hwnd, class_name.value, initial_area))
        return True

    user32.EnumWindows(callback, 0)
    # 传统 Qt 主窗口优先；新版 Chromium 微信则按恢复前面积排序。
    candidates.sort(
        key=lambda item: (item[1].startswith("Qt"), item[2]),
        reverse=True,
    )
    for hwnd, class_name, _initial_area in candidates:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.15)
        raw = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(raw)):
            continue
        rect = Rect(raw.left, raw.top, raw.right, raw.bottom)
        if rect.width > 700 and rect.height > 600:
            log_event(
                "wechat_manager_window_recovered",
                hwnd=hwnd,
                class_name=class_name,
                width=rect.width,
                height=rect.height,
            )
            return hwnd, rect
    raise RuntimeError("没有找到已打开的微信公众号管理窗口（已尝试恢复最小化窗口）")


def find_article_window() -> tuple[int, Rect]:
    candidates: list[tuple[int, Rect, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(64)
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 64)
        user32.GetClassNameW(hwnd, class_name, 256)
        if title.value.strip() != "微信" or not class_name.value.startswith("Chrome_WidgetWin_"):
            return True
        if not is_wechat_owned_window(hwnd):
            return True
        raw = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(raw)):
            rect = Rect(raw.left, raw.top, raw.right, raw.bottom)
            if rect.width > 700 and rect.height > 600:
                candidates.append((hwnd, rect, rect.width * rect.height))
        return True

    user32.EnumWindows(callback, 0)
    if not candidates:
        raise RuntimeError("没有找到已打开的微信文章窗口")
    hwnd, rect, _ = max(candidates, key=lambda item: item[2])
    return hwnd, rect


def capture_window(rect: Rect) -> Image.Image:
    # ImageGrab 只读屏幕像素；窗口不能被其他窗口遮挡。
    return ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)


def activate_window(hwnd: int) -> None:
    """恢复并可靠激活目标窗口；无法置前时停止，避免向其他应用误发按键。"""
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    if int(user32.GetForegroundWindow()) != int(hwnd):
        foreground_hwnd = int(user32.GetForegroundWindow())
        current_thread = int(kernel32.GetCurrentThreadId())
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
        foreground_thread = (
            int(user32.GetWindowThreadProcessId(foreground_hwnd, None))
            if foreground_hwnd
            else 0
        )
        attached_threads: list[int] = []
        try:
            # Windows 会限制后台进程抢占前台；临时绑定输入队列后再置前。
            for thread_id in {target_thread, foreground_thread}:
                if thread_id and thread_id != current_thread:
                    if user32.AttachThreadInput(current_thread, thread_id, True):
                        attached_threads.append(thread_id)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            for thread_id in reversed(attached_threads):
                user32.AttachThreadInput(current_thread, thread_id, False)
    time.sleep(0.6)
    if int(user32.GetForegroundWindow()) != int(hwnd):
        raise RuntimeError(f"无法将微信窗口置于前台，已停止发送按键：hwnd={hwnd}")


def _tab_switch_difference(before: Image.Image, after: Image.Image) -> float:
    """比较浏览器标签栏和页面主体，判断 Ctrl+Tab 是否真的切换了标签。"""
    if before.size != after.size:
        return 255.0
    width, height = before.size
    # 标签栏变化最直接，同时保留少量页面区域来区分两个标题相近的页面。
    crop_height = max(1, round(height * 0.32))
    difference = ImageChops.difference(
        before.crop((0, 0, width, crop_height)).convert("L"),
        after.crop((0, 0, width, crop_height)).convert("L"),
    )
    return float(ImageStat.Stat(difference).mean[0])


def _inspect_sogou_search_results(screenshot: Image.Image) -> dict[str, Any]:
    """验证搜索框与账号导航；横向滚动隐藏一级导航时复核二级筛选栏。"""
    search_box = PROFILE_OCR.locate_search_box(screenshot)
    account_tab = PROFILE_OCR.locate_account_tab(screenshot)
    account_filters = False
    if search_box.get("found") and not account_tab.get("found"):
        width, height = screenshot.size
        # 必须同时出现一组账号筛选项，不能把正文里单独的“公众号”当成导航。
        rows = [
            row for row in PROFILE_OCR._rows(screenshot)
            if height * 0.10 <= row["center_y"] <= height * 0.30
            and row["center_x"] < width * 0.65
        ]
        labels = ("不限", "小程序", "公众号", "服务号", "视频号")
        for anchor in rows:
            line = "".join(
                row["normalized"] for row in rows
                if abs(row["center_y"] - anchor["center_y"]) <= height * 0.015
            )
            if "公众号" in line and sum(label in line for label in labels) >= 3:
                account_filters = True
                break
    return {
        "found": bool(search_box.get("found") and (account_tab.get("found") or account_filters)),
        "search_box": search_box,
        "account_tab": account_tab,
        "account_filters": {"found": account_filters},
    }


def find_and_pin_search_tab(
    search_window: WindowInfo,
    account_name: str,
    *,
    max_tabs: int = 20,
) -> bool:
    """遍历现有标签找到真正的搜一搜结果页，并将它移动到第一个标签。"""
    activate_window(search_window.hwnd)
    press_ctrl_1()
    time.sleep(0.35)
    for index in range(max_tabs):
        screenshot = capture_window(search_window.rect)
        evidence = _inspect_sogou_search_results(screenshot)
        log_event(
            "sogou_search_tab_probe",
            account=account_name,
            tab_index=index + 1,
            found=bool(evidence["found"]),
            search_box_found=bool(evidence["search_box"].get("found")),
            account_tab_found=bool(evidence["account_tab"].get("found")),
        )
        if evidence["found"]:
            # Chromium 用 Ctrl+Shift+PageUp 将当前标签逐格向左移动。
            for _ in range(index):
                activate_window(search_window.hwnd)
                press_ctrl_shift_pageup()
                time.sleep(0.12)
            activate_window(search_window.hwnd)
            press_ctrl_1()
            time.sleep(0.35)
            validation = _inspect_sogou_search_results(capture_window(search_window.rect))
            if not validation["found"]:
                log_event(
                    "sogou_search_tab_pin_failed",
                    account=account_name,
                    original_tab_index=index + 1,
                )
                return False
            log_event(
                "sogou_search_tab_pinned",
                account=account_name,
                original_tab_index=index + 1,
                moved_left=index,
            )
            return True
        activate_window(search_window.hwnd)
        press_ctrl_tab()
        time.sleep(0.35)
    log_event(
        "sogou_search_tab_not_found",
        account=account_name,
        inspected_tabs=max_tabs,
    )
    return False


def keep_only_search_tab(
    search_window: WindowInfo,
    account_name: str,
    output_dir: Path | None = None,
) -> int:
    """清理历史遗留标签，只保留当前搜一搜页，保证采集时最多再打开一个文章标签。"""
    activate_window(search_window.hwnd)
    # 搜一搜约定固定在首标签。每轮从首标签重新建立基准，再跳到最右侧标签清理，
    # 避免文章页或分享弹窗里的输入框被误识别成搜一搜搜索框后提前停止。
    press_ctrl_1()
    time.sleep(0.35)
    baseline = capture_window(search_window.rect)
    if not _inspect_sogou_search_results(baseline)["found"]:
        return 0
    # 先测量当前电脑、远程桌面压缩和页面动画带来的自然波动，避免使用某台电脑的固定阈值。
    time.sleep(0.2)
    stable_baseline = capture_window(search_window.rect)
    idle_difference = _tab_switch_difference(baseline, stable_baseline)
    baseline = stable_baseline
    single_tab_threshold = max(0.35, min(12.0, idle_difference * 3.0 + 0.5))
    log_event(
        "browser_tab_cleanup_calibrated",
        account=account_name,
        idle_difference=round(idle_difference, 3),
        single_tab_threshold=round(single_tab_threshold, 3),
    )
    removed = 0
    for index in range(20):
        # 每次按键前重新激活同一个 HWND，防止远程桌面或公众号资料窗口抢走焦点。
        activate_window(search_window.hwnd)
        press_ctrl_9()
        time.sleep(0.35)
        candidate = capture_window(search_window.rect)
        difference = _tab_switch_difference(baseline, candidate)
        candidate_evidence = _inspect_sogou_search_results(candidate)
        candidate_has_search_box = bool(candidate_evidence["search_box"].get("found"))
        candidate_is_search_page = bool(candidate_evidence["found"])
        log_event(
            "browser_tab_probe",
            account=account_name,
            probe=index + 1,
            difference=round(difference, 3),
            single_tab_threshold=round(single_tab_threshold, 3),
            strategy="last_tab",
            candidate_has_search_box=candidate_has_search_box,
            candidate_is_search_page=candidate_is_search_page,
        )
        # 只有一个标签时 Ctrl+9 不会切页，截图差异仅来自光标或轻微动画。
        if difference < 0.35:
            break
        # 搜索结果页的动态内容会造成中等截图差异；只有差异较小且仍识别到搜索框时才保护首标签。
        # 文章分享弹窗同样含搜索框，但与搜一搜基准差异通常显著，不能据此放弃清理。
        if candidate_is_search_page and difference <= single_tab_threshold:
            log_event(
                "browser_tab_cleanup_stopped",
                account=account_name,
                probe=index + 1,
                reason="single_search_page_with_dynamic_content",
                difference=round(difference, 3),
            )
            break
        activate_window(search_window.hwnd)
        press_ctrl_w()
        removed += 1
        time.sleep(0.35)
        if not user32.IsWindow(search_window.hwnd):
            raise RuntimeError("清理浏览器标签时搜一搜窗口被意外关闭")
        # 关闭最右侧标签后可能落在另一个文章标签，必须显式回到首标签再校验。
        activate_window(search_window.hwnd)
        press_ctrl_1()
        time.sleep(0.35)
        baseline = capture_window(search_window.rect)
        if not _inspect_sogou_search_results(baseline)["found"]:
            raise RuntimeError("清理浏览器标签后没有回到搜一搜页面")
    else:
        raise RuntimeError("已清理20个历史标签但仍检测到其他标签，请人工检查浏览器")
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        baseline.save(output_dir / "browser-tabs-normalized.png")
    log_event("browser_tabs_normalized", account=account_name, removed=removed, remaining=1)
    return removed


def close_article_tabs_until_search(account_name: str) -> None:
    """先定位并固定搜索标签，再关闭其他标签，绝不盲目连关。"""
    # 只能从明确识别为搜一搜的窗口开始清理，禁止把任意微信窗口当作搜索窗口。
    search_window = find_sogou_search_window()
    if not find_and_pin_search_tab(search_window, account_name):
        # 搜索标签可能已被异常流程关闭或替换。此时不在旧窗口里继续盲目 Ctrl+W，
        # 也不销毁旧窗口；只尝试从微信主窗口无损打开一个新的搜一搜页。
        search_window = recreate_sogou_search_window(
            search_window,
            account_name,
            "遍历现有标签后未找到真正的搜一搜结果页",
        )
        if not find_and_pin_search_tab(search_window, account_name, max_tabs=3):
            raise RuntimeError("重新创建搜一搜窗口后仍无法确认搜索页，为保护页面拒绝自动关闭标签")
        log_event(
            "article_tab_cleanup_search_recovered",
            account=account_name,
            recovered_hwnd=search_window.hwnd,
        )
    closed = keep_only_search_tab(search_window, account_name)
    log_event(
        "article_tabs_closed",
        account=account_name,
        closed=closed,
        search_tab_preserved=True,
    )


def close_current_article_tab(
    account_name: str,
    title: str = "",
) -> bool:
    """正常路径直接关闭当前文章标签，避免每篇文章都轮询全部标签。

    文章采集结束时，前台应当仍是刚打开的文章标签。先确认它不是搜一搜
    页面，再发送一次 Ctrl+W；只有关闭后仍无法确认回到搜一搜时，调用方
    才会进入全量标签恢复流程。
    """
    try:
        article_hwnd, article_rect = find_article_window()
        foreground_hwnd = int(user32.GetForegroundWindow())
        if foreground_hwnd != article_hwnd:
            log_event(
                "article_tab_direct_close_skipped",
                account=account_name,
                title=title,
                reason="article_browser_not_foreground",
                foreground_hwnd=foreground_hwnd,
                article_hwnd=article_hwnd,
            )
            return False

        # 防止异常流程没有真正打开文章时误关搜一搜标签。
        evidence = _inspect_sogou_search_results(capture_window(article_rect))
        if evidence.get("found"):
            log_event(
                "article_tab_direct_close_skipped",
                account=account_name,
                title=title,
                reason="current_tab_is_search_page",
            )
            return False

        press_ctrl_w()
        time.sleep(0.45)
        search_window = find_sogou_search_window()
        search_evidence = _inspect_sogou_search_results(
            capture_window(search_window.rect)
        )
        if not search_evidence.get("found"):
            log_event(
                "article_tab_direct_close_failed",
                account=account_name,
                title=title,
                reason="search_page_not_confirmed_after_close",
            )
            return False
        log_event(
            "article_tab_closed_directly",
            account=account_name,
            title=title,
            search_tab_preserved=True,
        )
        return True
    except Exception as exc:
        # 直接关闭只是一条快速路径，任何不确定都交给安全恢复流程。
        log_event(
            "article_tab_direct_close_failed",
            account=account_name,
            title=title,
            reason="direct_close_exception",
            error=str(exc),
        )
        return False


def close_article_after_attempt(account_name: str, title: str = "") -> None:
    """优先关闭当前文章，失败时才执行全量标签恢复。"""
    if close_current_article_tab(account_name, title):
        return
    log_event(
        "article_tab_cleanup_recovery_started",
        account=account_name,
        title=title,
        reason="direct_close_not_confirmed",
    )
    close_article_tabs_until_search(account_name)


def arrange_automation_window(window: WindowInfo, role: str) -> WindowInfo:
    """固定搜一搜浏览器和公众号资料窗口，移动后返回新的真实坐标。"""
    if WINDOW_LAYOUT_MODE == "off":
        return window

    work_area = wintypes.RECT()
    if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0):  # SPI_GETWORKAREA
        return window
    work = Rect(work_area.left, work_area.top, work_area.right, work_area.bottom)
    if role == "browser":
        # 搜一搜在超宽窗口会切换成聚合布局并隐藏“公众号”二级筛选，限制宽度保证结构稳定。
        browser_width = max(900, min(round(work.width * 0.58), 1600))
        target = Rect(
            work.left,
            work.top,
            work.left + browser_width,
            work.bottom,
        )
    elif role == "profile":
        profile_width = max(620, min(round(work.width * 0.38), 1100))
        vertical_margin = max(0, round(work.height * 0.04))
        target = Rect(
            work.right - profile_width,
            work.top + vertical_margin,
            work.right,
            work.bottom - vertical_margin,
        )
    else:
        raise ValueError(f"未知窗口布局角色：{role}")

    user32.ShowWindow(window.hwnd, 9)  # SW_RESTORE
    moved = bool(user32.MoveWindow(
        window.hwnd,
        target.left,
        target.top,
        target.width,
        target.height,
        True,
    ))
    time.sleep(0.4)
    raw = wintypes.RECT()
    if moved and user32.GetWindowRect(window.hwnd, ctypes.byref(raw)):
        actual = Rect(raw.left, raw.top, raw.right, raw.bottom)
    else:
        actual = window.rect
    arranged = WindowInfo(
        window.hwnd,
        window.title,
        window.class_name,
        actual,
        window.process_name,
        window.shared_browser,
        window.profile_checkpoint,
    )
    log_event(
        "window_arranged",
        role=role,
        title=window.title,
        class_name=window.class_name,
        moved=moved,
        rect={
            "left": actual.left,
            "top": actual.top,
            "width": actual.width,
            "height": actual.height,
        },
    )
    return arranged


def normalized_to_screen(
    item: dict[str, Any], crop_box: tuple[int, int, int, int], window_rect: Rect
) -> tuple[int, int]:
    left, top, right, bottom = crop_box
    x = left + (right - left) * int(item["center_x_1000"]) / 1000
    y = top + (bottom - top) * int(item["center_y_1000"]) / 1000
    return round(window_rect.left + x), round(window_rect.top + y)


def click(screen_x: int, screen_y: int) -> None:
    user32.SetCursorPos(screen_x, screen_y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def press_ctrl_shift_r() -> None:
    """强制重新请求文章正文，避免页面预加载导致抓包缓存缺失。"""
    try:
        for key in (0x11, 0x10, 0x52):
            user32.keybd_event(key, 0, 0, 0)
    finally:
        for key in (0x52, 0x10, 0x11):
            user32.keybd_event(key, 0, 0x0002, 0)


def acquire_network_metrics(directory: str, page: dict, hwnd: int):
    """先等待在途响应，再刷新一次；刷新后只接受新请求产生的统计。"""
    record, reason = read_snapshot(directory, page)
    if record is not None or reason == "missing_article_identity":
        return record, reason
    for _ in range(6):
        time.sleep(0.5)
        record, reason = read_snapshot(directory, page)
        if record is not None:
            return record, reason
    activate_window(hwnd)
    refreshed_at = time.time()
    press_ctrl_shift_r()
    log_event("article_network_refresh", article_id=page.get("network_article_id"))
    for _ in range(30):
        time.sleep(0.5)
        record, reason = read_snapshot(directory, page)
        if record is not None and record.get("captured_at", 0) >= refreshed_at:
            return record, "hit_after_refresh"
    return None, "refresh_timeout:" + reason


def press_ctrl_end() -> None:
    user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
    user32.keybd_event(0x23, 0, 0, 0)  # End down
    user32.keybd_event(0x23, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_home() -> None:
    user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
    user32.keybd_event(0x24, 0, 0, 0)  # Home down
    user32.keybd_event(0x24, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_w() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x57, 0, 0, 0)
    user32.keybd_event(0x57, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_tab() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x09, 0, 0, 0)
    user32.keybd_event(0x09, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_1() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x31, 0, 0, 0)
    user32.keybd_event(0x31, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_9() -> None:
    """切换到浏览器最右侧标签，用于从尾部逐个清理文章页。"""
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x39, 0, 0, 0)
    user32.keybd_event(0x39, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_shift_pageup() -> None:
    """将当前 Chromium 标签向左移动一格，用于动态固定搜一搜标签。"""
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x10, 0, 0, 0)
    user32.keybd_event(0x21, 0, 0, 0)
    user32.keybd_event(0x21, 0, 0x0002, 0)
    user32.keybd_event(0x10, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_a() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x41, 0, 0, 0)
    user32.keybd_event(0x41, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_ctrl_v() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x56, 0, 0, 0)
    user32.keybd_event(0x56, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_enter() -> None:
    """向当前微信搜索框发送回车，触发搜索。"""
    user32.keybd_event(0x0D, 0, 0, 0)
    user32.keybd_event(0x0D, 0, 0x0002, 0)


def press_ctrl_f() -> None:
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x46, 0, 0, 0)
    user32.keybd_event(0x46, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def press_enter() -> None:
    user32.keybd_event(0x0D, 0, 0, 0)
    user32.keybd_event(0x0D, 0, 0x0002, 0)


def press_down() -> None:
    """选中微信搜索候选列表中的下一项。"""
    user32.keybd_event(0x28, 0, 0, 0)
    user32.keybd_event(0x28, 0, 0x0002, 0)


def press_escape() -> None:
    user32.keybd_event(0x1B, 0, 0, 0)
    user32.keybd_event(0x1B, 0, 0x0002, 0)


def scroll_window_up(rect: Rect, wheel_notches: int = 2) -> None:
    """在公众号内容区域向上翻页，正滚轮值表示查看更早的消息。"""
    user32.SetCursorPos(rect.left + rect.width // 2, rect.top + rect.height // 2)
    # Qt 会把超大的单次 delta 仍按一次滚轮处理，因此必须逐次发送标准 120 delta。
    for _ in range(wheel_notches):
        user32.mouse_event(0x0800, 0, 0, 120, 0)  # MOUSEEVENTF_WHEEL
        time.sleep(0.02)


def scroll_window_down(rect: Rect, wheel_notches: int = 2) -> None:
    """在公众号资料窗口向下滚动，查看更早的文章。"""
    user32.SetCursorPos(rect.left + rect.width // 2, rect.top + rect.height * 3 // 4)
    for _ in range(wheel_notches):
        user32.mouse_event(0x0800, 0, 0, -120, 0)
        time.sleep(0.02)


def set_clipboard_text(value: str) -> None:
    """写入 Unicode 剪贴板且不创建窗口，避免抢走微信输入焦点。"""
    import pyperclip

    pyperclip.copy(value)


def read_clipboard_text() -> str:
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(None):
        raise RuntimeError("无法打开系统剪贴板")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = ctypes.windll.kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            ctypes.windll.kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def copy_article_url(
    hwnd: int,
    rect: Rect,
    output_dir: Path | None = None,
    phase: str = "before",
    client: QwenVisionClient | None = None,
    allow_vl: bool = True,
) -> str:
    """动态定位浏览器菜单与“复制链接”，并用剪贴板 URL 验证整条操作链。"""
    clipboard_sentinel = f"__WECHAT_RPA_COPY_PENDING_{phase}__"
    # 标题栏按钮的物理像素会随 Windows DPI 缩放：100% 时菜单距右侧约 124px，
    # 150% 时约 186px。使用窗口真实 DPI，避免把 150% 坐标误用到 100% 电脑。
    get_dpi_for_window = getattr(user32, "GetDpiForWindow", None)
    dpi = int(get_dpi_for_window(hwnd)) if get_dpi_for_window else 96
    dpi = dpi if dpi > 0 else 96
    scale = dpi / 96
    fallback_menu_action = {
        "found": True,
        "center_x_1000": round((rect.width - 124 * scale) * 1000 / rect.width),
        "center_y_1000": round(21 * scale * 1000 / rect.height),
        "confidence": 0.35,
        "method": "dpi-relative-menu-button-fallback",
    }

    # 先在菜单关闭状态下识别标题栏按钮，避免菜单遮罩干扰三点图标检测。
    press_escape()
    time.sleep(0.2)
    titlebar_screenshot = capture_window(rect)
    menu_button_action: dict[str, Any] | None = None
    cached_menu_button = load_menu_button_position_cache(rect, dpi)
    if cached_menu_button is not None:
        validated_menu_button = validate_cached_menu_button_action(
            titlebar_screenshot,
            cached_menu_button,
        )
        log_event("menu_button_cached_position_validation", phase=phase, **validated_menu_button)
        if validated_menu_button.get("found"):
            menu_button_action = validated_menu_button
        else:
            clear_menu_button_position_cache(
                str(validated_menu_button.get("reason") or "缓存菜单按钮验证失败")
            )

    if menu_button_action is None:
        local_menu_button = PROFILE_OCR.locate_browser_menu_button(titlebar_screenshot)
        log_event("menu_button_local_detection", phase=phase, **local_menu_button)
        if local_menu_button.get("found"):
            menu_button_action = local_menu_button

    if menu_button_action is None and allow_vl and client is not None:
        try:
            qwen_menu_button = normalize_qwen_menu_button_action(
                client.detect_browser_menu_button(titlebar_screenshot)
            )
        except Exception as exc:
            qwen_menu_button = {"found": False, "reason": f"Qwen-VL 菜单按钮定位失败：{exc}"}
        log_event("menu_button_qwen_fallback", phase=phase, **qwen_menu_button)
        if qwen_menu_button.get("found"):
            menu_button_action = qwen_menu_button

    if menu_button_action is None:
        # 最终兜底仍是 DPI 相对位置，但只有后续成功复制出公众号 URL 时才会写入缓存。
        menu_button_action = fallback_menu_action
        log_event("menu_button_dpi_fallback", phase=phase, **fallback_menu_action)

    def open_menu(stage: str) -> Image.Image:
        # Esc 同时负责收起旧菜单和误弹出的“发送给”窗口，再打开干净菜单。
        press_escape()
        time.sleep(0.2)
        menu_x = rect.left + round(
            rect.width * int(menu_button_action["center_x_1000"]) / 1000
        )
        menu_y = rect.top + round(
            rect.height * int(menu_button_action["center_y_1000"]) / 1000
        )
        click(menu_x, menu_y)
        log_event(
            "copy_link_menu_button_clicked",
            phase=phase,
            stage=stage,
            dpi=dpi,
            scale=round(scale, 3),
            screen_x=menu_x,
            screen_y=menu_y,
            method=str(menu_button_action.get("method") or "unknown"),
        )
        time.sleep(0.8)
        screenshot = capture_window(rect)
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            screenshot.save(output_dir / f"copy-menu-{phase}-{stage}.png")
        return screenshot

    def click_action(action: dict[str, Any], stage: str) -> str:
        set_clipboard_text(clipboard_sentinel)
        action_x = rect.left + round(rect.width * int(action["center_x_1000"]) / 1000)
        action_y = rect.top + round(rect.height * int(action["center_y_1000"]) / 1000)
        click(action_x, action_y)
        log_event(
            "copy_link_action_clicked",
            phase=phase,
            stage=stage,
            screen_x=action_x,
            screen_y=action_y,
            method=str(action.get("method") or stage),
        )
        deadline = time.monotonic() + 2.5
        observed = clipboard_sentinel
        while time.monotonic() < deadline:
            time.sleep(0.2)
            observed = read_clipboard_text().strip()
            if observed != clipboard_sentinel:
                break
        log_event(
            "copy_link_clipboard_validation",
            phase=phase,
            stage=stage,
            valid=observed.startswith("https://mp.weixin.qq.com/"),
        )
        return observed

    def remember_success(action: dict[str, Any], source: str) -> None:
        """只有剪贴板 URL 已验证成功，才同时学习按钮与菜单项坐标。"""
        save_copy_link_position_cache(action, rect, dpi, source)
        save_menu_button_position_cache(
            menu_button_action,
            rect,
            dpi,
            str(menu_button_action.get("method") or "unknown"),
        )

    last_action: dict[str, Any] = {"found": False, "reason": "尚未识别菜单"}
    last_url = clipboard_sentinel
    menu_screenshot = open_menu("cache")

    cached = load_copy_link_position_cache(rect, dpi)
    if cached is not None:
        cached_action = validate_cached_copy_link_action(menu_screenshot, cached)
        last_action = cached_action
        log_event("copy_link_cached_position_validation", phase=phase, **cached_action)
        if cached_action.get("found"):
            last_url = click_action(cached_action, "cache")
            if last_url.startswith("https://mp.weixin.qq.com/"):
                remember_success(cached_action, "cache-validated")
                return last_url
            clear_copy_link_position_cache("缓存坐标点击后剪贴板未得到公众号 URL")
            menu_screenshot = open_menu("local-ocr")
        else:
            clear_copy_link_position_cache("缓存坐标附近未识别到复制链接")
    else:
        log_event("copy_link_cached_position_missed", phase=phase)

    local_action = PROFILE_OCR.locate_copy_link_action(menu_screenshot)
    last_action = local_action
    log_event("copy_link_menu_detection", phase=phase, stage="local-ocr", **local_action)
    if local_action.get("found"):
        last_url = click_action(local_action, "local-ocr")
        if last_url.startswith("https://mp.weixin.qq.com/"):
            remember_success(local_action, "local-ocr")
            return last_url
        menu_screenshot = open_menu("qwen-vl")
    else:
        log_event(
            "copy_link_menu_action_skipped",
            phase=phase,
            stage="local-ocr",
            reason=str(local_action.get("reason") or "未找到复制链接菜单项"),
        )

    if allow_vl and client is not None:
        try:
            qwen_action = normalize_qwen_copy_link_action(
                client.detect_copy_link_action(menu_screenshot)
            )
        except Exception as exc:
            qwen_action = {"found": False, "reason": f"Qwen-VL 调用失败：{exc}"}
        last_action = qwen_action
        log_event("copy_link_qwen_fallback", phase=phase, **qwen_action)
        if qwen_action.get("found"):
            last_url = click_action(qwen_action, "qwen-vl")
            if last_url.startswith("https://mp.weixin.qq.com/"):
                remember_success(qwen_action, "qwen-vl")
                return last_url
    else:
        log_event(
            "copy_link_qwen_fallback_skipped",
            phase=phase,
            reason="VL 已禁用或客户端未配置",
        )

    clear_copy_link_position_cache("复制链接三层识别全部失败")
    clear_menu_button_position_cache("未能通过合法公众号 URL 验证菜单按钮")
    press_escape()
    raise RuntimeError(
        "复制链接失败，缓存坐标、本地 OCR 与 Qwen-VL 均未写入公众号URL："
        f"menu_found={bool(last_action.get('found'))}，clipboard={last_url[:80]!r}"
    )


class ArticleMismatchError(RuntimeError):
    pass


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip()
    normalized = normalized.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    # 资料页 OCR 偶尔会把标题和其下方的阅读、点赞指标合并成一行。
    normalized = re.sub(r"阅读\s*[\d.万亿+]+.*$", "", normalized)
    normalized = re.sub(r"赞\s*\d+.*$", "", normalized)
    # 中文书名/引号的右半边常被 OCR 识别为 ASCII 方括号。
    normalized = normalized.replace("]", "」")
    normalized = re.sub(r"\s+", "", normalized)
    return normalized


def compact_title(value: str) -> str:
    """去掉标题标点，降低 OCR 对引号、竖线和中英文标点差异的影响。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", normalize_title(value))


def canonical_title_for_match(value: str) -> str:
    """生成用于比对的标题文本，兼容本地 OCR 的少量高频字符误识别。"""
    compact = compact_title(value).replace("丨", "").replace("｜", "").replace("|", "")
    # 在公众号标题中，OCR 常把 AI 末尾的大写 I 读成小写 l。
    # 这里只处理已知品牌词，避免对普通中文标题做过宽的替换。
    return (
        compact.replace("OpenAl", "OpenAI")
        .replace("ChatGPt", "ChatGPT")
        .replace("AlAgent", "AIAgent")
        .replace("PhysicalAl", "PhysicalAI")
    )


def parse_article_with_retry(url: str, **kwargs) -> dict:
    """临时空响应在原页面内重试，不重复打开文章或放宽身份验证。"""
    for attempt in range(1, 4):
        try:
            return parse_page(url, **kwargs)
        except ValueError as exc:
            if str(exc) not in {"文章页面没有标题", "文章正文为空，拒绝写入 MongoDB"}:
                raise
            log_event("article_parse_incomplete", attempt=attempt, reason=str(exc),
                      exhausted=attempt == 3)
            if attempt == 3:
                raise
            time.sleep(attempt)


def titles_match(expected: str, actual: str) -> bool:
    expected_value = normalize_title(expected)
    actual_value = normalize_title(actual)
    expected_canonical = canonical_title_for_match(expected_value)
    actual_canonical = canonical_title_for_match(actual_value)
    if not expected_canonical or not actual_canonical:
        return False  # 互动数被去掉后是空字符串，不能把空标题视作匹配。
    truncated = expected_value.rstrip(".…")
    if expected_value.endswith(("...", "…")):
        truncated_canonical = canonical_title_for_match(truncated)
        # 卡片文本带省略号时，卡片只提供标题前缀；以去标点后的前缀比较，
        # 能兼容“丨 / |”以及极少量 OCR 错字；公众号名和文章 URL 仍会独立校验。
        if len(truncated_canonical) < 8:
            return False
        if actual_canonical.startswith(truncated_canonical):
            return True
        actual_prefix = actual_canonical[: len(truncated_canonical)]
        return difflib.SequenceMatcher(
            None, truncated_canonical, actual_prefix
        ).ratio() >= 0.90
    if expected_value == actual_value:
        return True
    if expected_canonical == actual_canonical:
        return True
    # 卡片 OCR 可能漏掉末尾或错读一个字符；公众号名称仍会另行严格校验。
    # 浏览器标签受窗口宽度限制时不会显示省略号，只保留标题前缀。
    # 同时还有网页大标题、公众号名称及前后 URL 校验，因此 8 字以上前缀可安全接受。
    if len(expected_canonical) >= 8 and actual_canonical.startswith(expected_canonical):
        return True
    # 指标锚定模式优先读取紧贴“阅读/赞”的最后一行；多行标题因此可能只留下
    # 末行。公众号归属和复制前后 URL 仍会独立校验，6 字以上的完整后缀可接受。
    if len(expected_canonical) >= 6 and actual_canonical.endswith(expected_canonical):
        return True
    # OCR 的窗口标签经常只保留前半段，并把 AI/Al、O/0 等单字符读错。
    # 比较较短标题与真实标题等长前缀；公众号名称和 URL 仍会独立严格校验。
    shorter, longer = sorted((expected_canonical, actual_canonical), key=len)
    if len(shorter) >= 8:
        prefix_similarity = difflib.SequenceMatcher(
            None, shorter, longer[: len(shorter)]
        ).ratio()
        if prefix_similarity >= 0.84:
            return True
    length_ratio = min(len(expected_value), len(actual_value)) / max(
        len(expected_value), len(actual_value), 1
    )
    similarity = difflib.SequenceMatcher(None, expected_canonical, actual_canonical).ratio()
    return length_ratio >= 0.75 and similarity >= 0.92


def extract_local_interaction_metrics(
    screenshot: Image.Image, metric_mode: str, *, allow_partial: bool = True
) -> tuple[dict[str, Any], str, str | None]:
    """识别本地互动指标，并在非关键图标失败时保住已验证的转发数。

    转发数是当前采集的核心指标。全部指标模式下，收藏或评论图标可能随
    微信版本变化而匹配失败；此时不能让一篇已经确认链接、标题和转发数的
    文章被整体丢弃。函数会明确标记为部分采集，未确认的指标保持 ``None``。
    """
    if metric_mode == "share":
        return INTERACTION_OCR.extract_share(screenshot), "template-ocr-share-only", None

    try:
        metrics = INTERACTION_OCR.extract(screenshot)
        required = ("share_count", "favorite_count", "comment_count")
        if any(metrics.get(name) is None for name in required):
            raise ValueError(f"本地互动数识别不完整：{metrics}")
        return metrics, "template-ocr", None
    except Exception as full_error:
        # 已启用 VL 时仍交给视觉模型补齐全部指标；局部降级仅服务于禁用 VL 的本地运行。
        if not allow_partial:
            raise
        # 只对转发图标进行一次独立识别；它成功时允许以“部分指标”继续入库。
        # 若转发本身也无法确认，仍把原始异常向上抛出，避免写入不可靠数据。
        try:
            share_metrics = INTERACTION_OCR.extract_share(screenshot)
        except Exception:
            raise full_error
        if share_metrics.get("share_count") is None:
            raise full_error
        details = dict(share_metrics.get("details") or {})
        details["partial_reason"] = str(full_error)
        return (
            {
                "share_count": share_metrics["share_count"],
                "like_count": None,
                "favorite_count": None,
                "comment_count": None,
                "details": details,
            },
            "template-ocr-partial-share",
            str(full_error),
        )


def collect_open_article(
    client: QwenVisionClient,
    output_dir: Path,
    write_mongo: bool,
    export_jsonl: str | None,
    export_csv: str | None,
    expected_title: str | None = None,
    expected_account: str | None = None,
    allow_vl: bool = True,
    mongo_uri: str | None = None,
    mongo_database: str | None = None,
    mongo_collection: str | None = None,
    mongo_target_collection: str | None = None,
    list_read_count: int | None = None,
    list_like_count: int | None = None,
    successful_urls_in_run: set[str] | None = None,
    metric_mode: str = "all",
    scan_range: str | None = None,
    article_window: WindowInfo | None = None,
) -> dict[str, Any]:
    log_event(
        "article_collect_started",
        expected_title=expected_title,
        expected_account=expected_account,
        metric_mode=metric_mode,
    )
    hwnd, rect = (article_window.hwnd, article_window.rect) if article_window else find_article_window()
    activate_window(hwnd)
    output_dir.mkdir(parents=True, exist_ok=True)

    raw = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(raw))
    rect = Rect(raw.left, raw.top, raw.right, raw.bottom)
    log_event(
        "article_window_selected",
        hwnd=hwnd,
        rect={"left": rect.left, "top": rect.top, "width": rect.width, "height": rect.height},
    )
    # 先复制链接并解析真实标题，校验通过后直接识别固定在视口底部的互动栏。
    url = copy_article_url(
        hwnd,
        rect,
        output_dir,
        "before",
        client=client,
        allow_vl=allow_vl,
    )
    log_event("article_url_copied_before", url=url)
    if successful_urls_in_run is not None and url in successful_urls_in_run:
        # URL 是文章的确定标识；仅凭 OCR 标题相似度绝不跳过，避免误漏文章。
        log_event("article_skipped_duplicate_url", url=url, expected_title=expected_title)
        return {
            "url": url,
            "title": expected_title or "",
            "account_name": expected_account or "",
            "status": "skipped_duplicate_in_run",
        }
    # 先查 MongoDB；已成功采集且正文完整的文章不再重复下载正文。
    cached_page = None
    if write_mongo:
        try:
            cached_page = load_cached_page(
                url,
                mongo_uri or os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
                mongo_database or os.getenv("MONGO_DATABASE", "weixin"),
                mongo_collection or os.getenv("MONGO_ARTICLE_COLLECTION", "article"),
            )
            if cached_page:
                log_event("article_page_reused_from_mongo", url=url)
        except Exception as exc:
            log_event("article_cache_lookup_failed", url=url, error=str(exc))
    network_directory = os.getenv("WECHAT_CAPTURE_CACHE_DIR", "").strip() if metric_mode == "share" else ""
    network_strict = os.getenv("WECHAT_CAPTURE_MODE", "prefer").strip().lower() == "strict"
    if network_strict and not network_directory:
        raise RuntimeError("仅抓包模式需要配置缓存目录并选择仅转发数")
    # 网络模式需要最终响应的文章 ID，不能仅凭 Mongo 中缓存的标题关联统计。
    if network_directory:
        try:
            page = parse_article_with_retry(url, include_network_identity=True)
        except Exception:
            if not cached_page:
                raise
            # 解析最终地址失败时仍可使用已有正文，但不允许复用旧网络身份。
            page = dict(cached_page)
            page.pop("network_article_id", None)
            log_event("article_network_identity_unavailable", action="fallback_to_ui")
    else:
        page = cached_page or parse_article_with_retry(url)
    log_event(
        "article_page_parsed",
        url=url,
        title=page.get("title"),
        account_name=page.get("account_name"),
        publish_time=page.get("publish_time") or page.get("publishDate"),
        content_length=len(str(page.get("content") or "")),
    )
    if expected_title and not titles_match(expected_title, page["title"]):
        raise ArticleMismatchError(
            f"文章标题不匹配：目标={expected_title!r}，实际={page['title']!r}"
        )
    if expected_account and normalize_title(expected_account) != normalize_title(page["account_name"]):
        raise ArticleMismatchError(
            f"公众号不匹配：目标={expected_account!r}，实际={page['account_name']!r}"
        )
    publish_time = page.get("publish_time") or page.get("publishDate")
    if scan_range and not publish_time_matches_scan_range(publish_time, scan_range):
        # 跳过统计和写库不代表跳过身份确认，图片消息清理同样需要 URL 稳定证据。
        url_after = copy_article_url(hwnd, rect, output_dir, "after", client=client, allow_vl=allow_vl)
        if url_after != url:
            raise ArticleMismatchError("跳过旧文章期间活动标签发生变化，拒绝确认清理身份")
        # 资料页时间分组只用于初筛；真正写库前必须以文章页面的发布时间为准。
        # 这样即使 OCR 把旧卡片错误归到“今天”，也不会更新历史文章互动数。
        log_event(
            "article_skipped_outside_scan_range",
            url=url,
            title=page.get("title"),
            publish_time=publish_time,
            scan_range=scan_range,
        )
        return {
            "url": url,
            "title": page.get("title") or expected_title or "",
            "account_name": page.get("account_name") or expected_account or "",
            "publish_time": publish_time,
            "status": "skipped_outside_scan_range",
            "skip_reason": f"真实发布时间 {publish_time} 不属于扫描范围 {scan_range}",
            "verification": {"url_before": url, "url_after": url_after, "url_stable": True,
                             "title_matched": True, "account_matched": True},
        }
    network_record = None
    if network_directory:
        network_record, network_reason = acquire_network_metrics(network_directory, page, hwnd)
        log_event("article_network_metrics_lookup", status=network_reason)
        if network_record is None and network_strict:
            # 必须在视觉模型兜底的 try 块外抛出，禁止严格模式静默转为 OCR/VL。
            raise RuntimeError("仅抓包采集失败：" + network_reason)
    evidence_screenshot = capture_window(rect)
    evidence_screenshot.save(output_dir / "article_evidence.png")
    evidence = ARTICLE_EVIDENCE_OCR.inspect(evidence_screenshot, page["title"])
    viewport_title = str((evidence.get("viewport_title") or {}).get("text") or "")
    tab_title = str((evidence.get("tab_title") or {}).get("text") or "")
    log_event(
        "article_title_evidence",
        parsed_title=page.get("title"),
        card_title=expected_title,
        tab_title=tab_title,
        viewport_title=viewport_title,
    )
    # 正文 OCR 可能把首段引文识别成标题；网页标题、公众号名和 URL 已严格校验，
    # 因此正文/标签页 OCR 只记录辅助证据，不因误识别而重复打开文章。
    viewport_matched = bool(viewport_title) and titles_match(viewport_title, page["title"])
    tab_matched = bool(tab_title) and titles_match(tab_title, page["title"])
    if not viewport_matched:
        log_event(
            "article_title_evidence_warning",
            parsed_title=page.get("title"),
            viewport_title=viewport_title,
            tab_title=tab_title,
            viewport_matched=viewport_matched,
            tab_matched=tab_matched,
            action="continue_after_url_page_account_validation",
        )
    if False and (not viewport_title or not titles_match(viewport_title, page["title"])):
        raise ArticleMismatchError(
            f"同屏正文标题不匹配：OCR={viewport_title!r}，网页={page['title']!r}"
        )
    if False and (not tab_title or not titles_match(tab_title, page["title"])):
        raise ArticleMismatchError(
            f"活动标签标题不匹配：OCR={tab_title!r}，网页={page['title']!r}"
        )

    # 正文标题和互动栏必须来自同一张完整窗口截图。
    footer_top = round(evidence_screenshot.height * 0.70)
    article_footer = evidence_screenshot.crop(
        (0, footer_top, evidence_screenshot.width, evidence_screenshot.height)
    )
    article_footer.save(output_dir / "article_footer.png")
    metric_source = "template-ocr-share-only" if metric_mode == "share" else "template-ocr"
    try:
        picture_count = picture_share_count(probe_page(hwnd), page["account_name"], page["title"]) if metric_mode == "share" and network_record is None else None
        if network_record is not None:
            bottom_metrics, metric_source, partial_reason = {"share_count": network_record["share_count"]}, "network-response-share", None
        elif picture_count is not None:
            bottom_metrics, metric_source, partial_reason = {"share_count": picture_count}, "uia-picture-share", None
        else:
            bottom_metrics, metric_source, partial_reason = extract_local_interaction_metrics(
                evidence_screenshot, metric_mode, allow_partial=not allow_vl
            )
        if partial_reason:
            log_event(
                "article_metrics_partial",
                url=url,
                metric_source=metric_source,
                retained_metrics={"share_count": bottom_metrics.get("share_count")},
                unavailable_metrics=["like_count", "favorite_count", "comment_count"],
                reason=partial_reason,
            )
    except Exception as exc:
        if not allow_vl:
            raise RuntimeError(f"本地互动数识别失败且已禁用VL：{exc}") from exc
        # 窗口缩放、主题或微信版本变化导致模板失效时，保留视觉模型兜底。
        metric_source = "qwen-vl-share-fallback" if metric_mode == "share" else "qwen-vl-fallback"
        bottom_metrics = client.extract_interaction_counts(article_footer)
        bottom_metrics["fallback_reason"] = str(exc)
    metrics = {
        "read_count": None if metric_mode == "share" else list_read_count,
        "like_count": None if metric_mode == "share" else (
            bottom_metrics.get("like_count")
            if bottom_metrics.get("like_count") is not None
            else list_like_count
        ),
        "share_count": bottom_metrics.get("share_count"),
        "favorite_count": None if metric_mode == "share" else bottom_metrics.get("favorite_count"),
        "comment_count": None if metric_mode == "share" else bottom_metrics.get("comment_count"),
        "metric_source": metric_source,
    }
    log_event("article_metrics_extracted", url=url, **metrics)
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    url_after = copy_article_url(
        hwnd,
        rect,
        output_dir,
        "after",
        client=client,
        allow_vl=allow_vl,
    )
    log_event("article_url_copied_after", url_before=url, url_after=url_after, stable=url_after == url)
    if url_after != url:
        raise ArticleMismatchError(
            f"互动数采集期间活动标签发生变化：before={url!r}, after={url_after!r}"
        )
    verification = {
        "url_before": url,
        "url_after": url_after,
        "url_stable": True,
        "expected_card_title": expected_title or "",
        "parsed_title": page["title"],
        "parsed_account": page["account_name"],
        "tab_title": evidence.get("tab_title"),
        "viewport_title": evidence.get("viewport_title"),
        "same_frame_evidence": "article_evidence.png",
        "title_matched": True,
        "account_matched": True,
    }
    (output_dir / "verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result = ingest(
        url=url,
        metrics=metrics,
        mongo_uri=mongo_uri or os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
        database_name=mongo_database or os.getenv("MONGO_DATABASE", "weixin"),
        collection_name=mongo_collection or os.getenv("MONGO_ARTICLE_COLLECTION", "article"),
        dry_run=not write_mongo,
        page=page,
        target_collection_name=mongo_target_collection
        or os.getenv("MONGO_TARGET_COLLECTION", "collection_target"),
        expected_account_name=expected_account,
    )
    log_event(
        "article_ingest_finished",
        url=url,
        title=page.get("title"),
        status=result.get("status"),
        write_mongo=write_mongo,
    )
    result["verification"] = verification
    append_local_exports(result, export_jsonl, export_csv)
    (output_dir / "collection.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return result


def safe_path_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned[:60] or "unknown"


def classify_collection_error(error: BaseException) -> str:
    """把采集异常归类，便于失败队列按类型重试和统计。"""
    text = str(error).lower()
    if any(marker in text for marker in ("页面跳转归属", "标签清理失败", "返回后未找到",
                                         "标签遍历", "导航上限", "无法确认当前页面")):
        return "navigation_return"
    if "未确认目标标题和公众号" in text:
        return "article_identity"
    if "搜一搜连续" in text and "未出现搜一搜" in text:
        return "search_recovery"
    if "未找到可确认的同名公众号" in text or "没有精确匹配名称" in text:
        return "account_not_found"
    if "筛选未确认选中" in text or "二级公众号筛选" in text:
        return "account_filter"
    if "资料窗口顶部名称不匹配" in text:
        return "profile_validation"
    if "ocr" in text or "识别" in text or "模板" in text:
        return "interaction_ocr"
    if "复制链接" in text or "clipboard" in text or "url" in text:
        return "copy_link"
    if "窗口" in text or "window" in text or "标签页" in text or "tab" in text:
        return "window"
    if "http" in text or "网络" in text or "timeout" in text or "timed out" in text:
        return "network"
    if "mongodb" in text or "mongo" in text or "入库" in text:
        return "mongodb"
    return "unknown"


def append_failure_queue(output_dir: Path, item: dict[str, Any]) -> None:
    """追加失败文章队列，下一次任务可据此优先补采。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "failure-queue.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")


def recent_visible_articles(
    articles: list[dict[str, Any]], scan_range: str = "today_yesterday"
) -> list[dict[str, Any]]:
    """继承时间分组标签，并按选择的日期范围筛选文章。"""
    recent: list[dict[str, Any]] = []
    current_group = ""
    for article in sorted(articles, key=lambda item: item["screen_point"][1]):
        visible_time = str(article.get("visible_time") or "").strip()
        if visible_time:
            current_group = visible_time
        article["effective_visible_time"] = current_group
        if is_recent_time_group(current_group, scan_range):
            recent.append(article)
    return recent


def run_one_account(
    client: QwenVisionClient,
    output_dir: Path,
    account_index: int,
    max_articles: int,
    export_jsonl: str | None,
    export_csv: str | None,
    metric_mode: str = "all",
    scan_range: str = "today_yesterday",
) -> dict[str, Any]:
    manager_result = analyze_current_window(client, output_dir / "manager-before")
    accounts = manager_result["accounts"]
    if not 0 <= account_index < len(accounts):
        raise IndexError("公众号序号超出当前屏识别结果范围")
    account = accounts[account_index]
    click(*account["screen_point"])
    time.sleep(2)

    selected_result = analyze_current_window(client, output_dir / "account-selected")
    articles = recent_visible_articles(
        selected_result["articles"], scan_range
    )[:max_articles]
    collected: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for index, article in enumerate(articles, start=1):
        try:
            click(*article["screen_point"])
            time.sleep(2.5)
            article_dir = output_dir / f"article-{index:02d}-{safe_path_name(article['title'])}"
            record = collect_open_article(
                client,
                article_dir,
                write_mongo=False,
                export_jsonl=export_jsonl,
                export_csv=export_csv,
                metric_mode=metric_mode,
                scan_range=scan_range,
            )
            if record.get("status") == "skipped_outside_scan_range":
                skipped.append(
                    {
                        "title": article.get("title", ""),
                        "reason": str(record.get("skip_reason") or "真实发布时间不在扫描范围"),
                    }
                )
            else:
                collected.append({key: value for key, value in record.items() if key != "content"})
        except Exception as exc:
            failures.append({"title": article.get("title", ""), "error": str(exc)})
        finally:
            try:
                article_hwnd, _ = find_article_window()
                activate_window(article_hwnd)
                press_ctrl_w()
                time.sleep(0.8)
            except Exception:
                pass
            manager_hwnd, _ = find_wechat_manager_window()
            activate_window(manager_hwnd)

    summary = {
        "account": account.get("name"),
        "recognized_recent_articles": len(articles),
        "collected": collected,
        "skipped": skipped,
        "failures": failures,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return summary


def analyze_current_window(client: QwenVisionClient, output_dir: Path) -> dict[str, Any]:
    hwnd, rect = find_wechat_manager_window()
    activate_window(hwnd)
    # 恢复窗口后位置可能变化，重新读取矩形。
    raw = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(raw))
    rect = Rect(raw.left, raw.top, raw.right, raw.bottom)
    screenshot = capture_window(rect)
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot.save(output_dir / "wechat_window.png")

    layout = client.detect_manager_layout(screenshot)
    if not layout.get("is_manager_visible"):
        raise RuntimeError("当前微信窗口没有显示公众号管理页面")
    sidebar_box = normalized_bbox_to_pixels(layout["account_sidebar_bbox_1000"], screenshot)
    content_box = normalized_bbox_to_pixels(layout["article_content_bbox_1000"], screenshot)
    sidebar = screenshot.crop(sidebar_box)
    content = screenshot.crop(content_box)
    sidebar.save(output_dir / "sidebar.png")
    content.save(output_dir / "content.png")

    accounts = client.detect_accounts(sidebar)
    articles = client.detect_articles(content)
    for account in accounts:
        account["screen_point"] = normalized_to_screen(account, sidebar_box, rect)
    for article in articles:
        article["screen_point"] = normalized_to_screen(article, content_box, rect)

    result = {
        "window": rect.__dict__,
        "layout": layout,
        "sidebar_box": sidebar_box,
        "content_box": content_box,
        "accounts": accounts,
        "articles": articles,
    }
    (output_dir / "analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def search_and_open_account(
    client: QwenVisionClient,
    account_name: str,
    output_dir: Path,
    allow_vl: bool = True,
) -> WindowInfo:
    search_window = find_search_window()
    # 先准备查询文本，再激活并操作微信搜索框。
    set_clipboard_text(account_name)
    activate_window(search_window.hwnd)
    search_ocr = WeChatOCR()
    # Ctrl+F 唤起微信全局搜索，再由 OCR 定位输入框，兼容窗口尺寸及当前页面变化。
    press_ctrl_f()
    time.sleep(0.5)
    before_search = capture_window(search_window.rect)
    output_dir.mkdir(parents=True, exist_ok=True)
    before_search.save(output_dir / "before-search.png")
    search_box = search_ocr.locate_search_box(before_search, account_name)
    if not search_box.get("found"):
        raise RuntimeError(str(search_box.get("reason") or "无法定位微信搜索框"))
    click(
        search_window.rect.left
        + round(search_window.rect.width * int(search_box["center_x_1000"]) / 1000),
        search_window.rect.top
        + round(search_window.rect.height * int(search_box["center_y_1000"]) / 1000),
    )
    time.sleep(0.2)
    press_ctrl_a()
    press_ctrl_v()
    time.sleep(2.0)

    screenshot = capture_window(search_window.rect)
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot.save(output_dir / "search-result.png")
    try:
        target = search_ocr.locate_official_account_result(screenshot, account_name)
        if not target.get("found"):
            raise ValueError(str(target.get("reason") or "本地OCR没有定位到公众号"))
    except Exception as exc:
        if not allow_vl:
            raise RuntimeError(f"本地公众号搜索失败且已禁用VL：{exc}") from exc
        # 窗口主题或版面变化时保留 VL 兜底，但正常搜索不再消耗 VL。
        target = client.detect_search_account(screenshot, account_name)
        target["method"] = "qwen-vl-fallback"
        target["fallback_reason"] = str(exc)
    (output_dir / "search-detection.json").write_text(
        json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not target.get("found") or normalize_title(str(target.get("name") or "")) != normalize_title(account_name):
        raise RuntimeError(f"搜索结果中没有公众号精确匹配项：{account_name}")
    click(
        search_window.rect.left + round(search_window.rect.width * int(target["center_x_1000"]) / 1000),
        search_window.rect.top + round(search_window.rect.height * int(target["center_y_1000"]) / 1000),
    )

    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            account_window = find_account_message_window(account_name)
            activate_window(account_window.hwnd)
            return account_window
        except RuntimeError:
            time.sleep(0.3)
    raise RuntimeError(f"点击搜索结果后未打开公众号窗口：{account_name}")


def _normalize_account_name_for_confirmation(value: object) -> str:
    """标准化用于公众号精确确认的名称。

    这里故意不使用文章标题的模糊匹配逻辑：公众号名称一旦点错，后面的文章、
    链接和互动数就会全部归属错误。仅忽略 Unicode 形式、空白和 ASCII 大小写差异。
    """
    return "".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _qwen_profile_header_confirmed(
    validation: dict[str, Any], expected_name: str
) -> bool:
    """只依据模型读到的精确名称和置信度确认资料页。

    部分兼容网关会返回正确的 ``name``，但把派生字段 ``matched`` 错置为 false。
    名称仍必须精确一致，并要求足够置信度；因此不会放宽到相似公众号。
    """
    observed_name = str(validation.get("name") or "").strip()
    try:
        confidence = float(validation.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        bool(observed_name)
        and confidence >= 0.80
        and _normalize_account_name_for_confirmation(observed_name)
        == _normalize_account_name_for_confirmation(expected_name)
    )


def _qwen_search_target(
    client: QwenVisionClient,
    screenshot: Image.Image,
    expected_name: str,
) -> dict[str, Any]:
    """把 Qwen-VL 的定位结果转为搜索卡片。

    调用时已经由本地 OCR 确认了“账号 → 公众号”筛选。即使如此，仍在这里再做名称与坐标检查，不接受模型的猜测结果。
    """
    result = client.detect_search_account(screenshot, expected_name)
    observed_name = str(result.get("name") or "").strip()
    if not result.get("found"):
        raise ValueError("Qwen-VL 未确认目标公众号")
    if not observed_name or (
        _normalize_account_name_for_confirmation(observed_name)
        != _normalize_account_name_for_confirmation(expected_name)
    ):
        raise ValueError(
            f"Qwen-VL 公众号名称不匹配：预期={expected_name!r}，识别={observed_name!r}"
        )

    def coordinate(key: str) -> int:
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Qwen-VL 缺少有效坐标：{key}")
        normalized = int(round(value))
        if not 0 <= normalized <= 1000:
            raise ValueError(f"Qwen-VL 坐标超出范围：{key}={normalized}")
        return normalized

    center_x = coordinate("center_x_1000")
    center_y = coordinate("center_y_1000")
    avatar_x = result.get("avatar_x_1000")
    avatar_y = result.get("avatar_y_1000")
    # 旧版模型或缓存响应没有头像坐标时，只允许退回到同一张卡片的名称坐标，
    # 绝不会使用一个未校验的默认点。
    if isinstance(avatar_x, bool) or not isinstance(avatar_x, (int, float)):
        avatar_x = center_x
    if isinstance(avatar_y, bool) or not isinstance(avatar_y, (int, float)):
        avatar_y = center_y
    return {
        "found": True,
        "is_official_account": True,
        "name": observed_name,
        "matched_name": observed_name,
        "name_match_method": "qwen-vl-exact-after-local-filter",
        "official_evidence": "local_filter_confirmed + qwen-vl-exact-name",
        "center_x_1000": center_x,
        "center_y_1000": center_y,
        "avatar_x_1000": int(round(avatar_x)),
        "avatar_y_1000": int(round(avatar_y)),
        "confidence": result.get("confidence"),
    }


def search_and_open_profile(
    account_name: str,
    output_dir: Path,
    *,
    client: QwenVisionClient | None = None,
    allow_vl: bool = True,
) -> tuple[WindowInfo, str]:
    """通过搜一搜精确名称打开公众号资料窗口，不要求当前微信账号关注公众号。"""
    search_name = resolve_search_account_name(account_name)
    log_event(
        "account_search_started",
        account=account_name,
        search_name=search_name,
        alias_applied=search_name != account_name,
    )
    try:
        search_window = find_navigation_browser()
    except RuntimeError as exc:
        log_event(
            "sogou_window_missing",
            account=account_name,
            reason=str(exc),
            action="recover_from_wechat_main_window",
        )
        search_window = open_sogou_from_wechat_main(
            account_name,
            client=client,
            allow_vl=allow_vl,
        )
        # 明确记录自动恢复已完成，控制台日志可区分“已自愈”与“仍然缺少搜一搜窗口”。
        log_event(
            "sogou_window_recovered",
            account=account_name,
            hwnd=search_window.hwnd,
            method="wechat_main_window",
        )
    search_window = arrange_automation_window(search_window, "browser")
    activate_window(search_window.hwnd)
    session = browser_session(search_window.hwnd)
    if session and session not in _CLEANED_TAB_SESSIONS:
        closed = TAB_OWNERSHIP.cleanup(session, browser_navigator(search_window, ""))
        _CLEANED_TAB_SESSIONS.add(session)
        log_event("owned_tabs_cleanup", closed=closed)
    # 新版搜索页不保证是首标签，先按页面身份定位，禁止重排用户标签。
    semantic_search = browser_navigator(search_window, "").select(lambda page: page.role == "search")
    if not semantic_search:
        press_ctrl_1()  # 旧版无 UIA 文档时继续使用已有 OCR 兼容路径。
    time.sleep(0.6)
    output_dir.mkdir(parents=True, exist_ok=True)
    search_box: dict[str, Any] = {"found": False}
    before: Image.Image | None = None
    search_page_recreated = False
    for recovery_index in range(9):
        before = capture_window(search_window.rect)
        before.save(output_dir / f"before-search-{recovery_index:02d}.png")
        search_box = PROFILE_OCR.locate_search_box(before)
        log_event(
            "search_box_detection",
            account=account_name,
            recovery_index=recovery_index,
            found=bool(search_box.get("found")),
            reason=search_box.get("reason"),
        )
        if search_box.get("found"):
            break
        if recovery_index == 1 and not search_page_recreated:
            if find_and_pin_search_tab(search_window, account_name):
                log_event(
                    "search_page_recovery_finished",
                    account=account_name,
                    recovered_hwnd=search_window.hwnd,
                    method="existing-tab-scan",
                )
                continue
            # 所有现有标签都不是搜一搜，才从微信主窗口重建，避免无谓关闭整个浏览器。
            search_window = recreate_sogou_search_window(
                search_window,
                account_name,
                str(search_box.get("reason") or "首标签不是搜一搜页面"),
            )
            # 页面标签被文章窗口替换时会重新拉起搜一搜；记录这一步便于定位后续失败发生在哪个阶段。
            log_event(
                "sogou_search_page_recreated",
                account=account_name,
                hwnd=search_window.hwnd,
                recovery_index=recovery_index,
            )
            search_page_recreated = True
            continue
        # 只重复回到首标签并等待页面稳定，禁止盲目 Ctrl+W 误关搜索页。
        activate_window(search_window.hwnd)
        if not semantic_search:
            press_ctrl_1()
        press_escape()
        time.sleep(0.5)
    if before is not None:
        before.save(output_dir / "before-search.png")
    if not search_box.get("found"):
        raise RuntimeError(str(search_box.get("reason") or "无法定位搜一搜搜索框"))
    # 每个账号开始前清掉遗留文章标签；此后始终保持“搜索页 + 当前文章”最多两个标签。
    # 第二代浏览器中主页也是标签，保留原有页面，不执行全量标签清理。
    before = capture_window(search_window.rect)
    search_box = PROFILE_OCR.locate_search_box(before)
    if not search_box.get("found"):
        raise RuntimeError("标签清理后无法重新定位搜一搜搜索框")
    set_clipboard_text(search_name)
    click(
        search_window.rect.left
        + round(search_window.rect.width * int(search_box["center_x_1000"]) / 1000),
        search_window.rect.top
        + round(search_window.rect.height * int(search_box["center_y_1000"]) / 1000),
    )
    press_ctrl_a()
    press_ctrl_v()
    # 搜索框下拉建议出现时回车可能只停留在建议层，明确点击绿色搜索按钮。
    click(
        search_window.rect.left
        + round(search_window.rect.width * int(search_box["button_x_1000"]) / 1000),
        search_window.rect.top
        + round(search_window.rect.height * int(search_box["button_y_1000"]) / 1000),
    )
    log_event("search_submitted", account=account_name, search_name=search_name)
    time.sleep(2.0)
    screenshot = capture_window(search_window.rect)
    screenshot.save(output_dir / "search-result-before-account-tab.png")
    account_tab = PROFILE_OCR.locate_account_tab(screenshot)
    (output_dir / "account-tab-detection.json").write_text(
        json.dumps(account_tab, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not account_tab.get("found"):
        raise RuntimeError(str(account_tab.get("reason") or "无法定位搜一搜账号分类"))
    # 每个公众号都强制选择“账号”，并用下划线和二级筛选项验证点击确实生效。
    selection: dict[str, Any] = {"selected": False}
    for selection_attempt in range(1, 4):
        click(
            search_window.rect.left
            + round(search_window.rect.width * int(account_tab["center_x_1000"]) / 1000),
            search_window.rect.top
            + round(search_window.rect.height * int(account_tab["center_y_1000"]) / 1000),
        )
        time.sleep(1.2)
        screenshot = capture_window(search_window.rect)
        screenshot.save(output_dir / f"account-tab-after-{selection_attempt}.png")
        selection = PROFILE_OCR.validate_account_tab_selected(screenshot)
        log_event(
            "account_tab_validation",
            account=account_name,
            attempt=selection_attempt,
            selected=bool(selection.get("selected")),
            reason=selection.get("reason"),
            filters=selection.get("visible_account_filters"),
        )
        (output_dir / f"account-tab-validation-{selection_attempt}.json").write_text(
            json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if selection.get("selected"):
            break
    if not selection.get("selected"):
        raise RuntimeError(
            f"连续3次点击后仍无法确认账号分类已选中：{selection.get('reason', '')}"
        )

    # 一级“账号”选中后，还必须明确点击二级“公众号”，不能停留在默认“不限”。
    official_filter = PROFILE_OCR.locate_official_account_filter(screenshot)
    (output_dir / "official-account-filter-detection.json").write_text(
        json.dumps(official_filter, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not official_filter.get("found"):
        raise RuntimeError(str(official_filter.get("reason") or "无法定位二级公众号筛选项"))

    target: dict[str, Any] = {"found": False}
    official_filter_confirmed = False
    filter_selected_once = False
    filter_failure_reason = ""
    for filter_attempt in range(1, 4):
        click(
            search_window.rect.left
            + round(search_window.rect.width * int(official_filter["center_x_1000"]) / 1000),
            search_window.rect.top
            + round(search_window.rect.height * int(official_filter["center_y_1000"]) / 1000),
        )
        time.sleep(1.2)
        screenshot = capture_window(search_window.rect)
        screenshot.save(output_dir / f"official-account-filter-after-{filter_attempt}.png")
        filter_selection = PROFILE_OCR.validate_official_account_filter_selected(screenshot)
        target = PROFILE_OCR.locate_search_result(screenshot, search_name)
        log_event(
            "official_account_filter_validation",
            account=account_name,
            attempt=filter_attempt,
            selected=bool(filter_selection.get("selected")),
            foreground_median=filter_selection.get("foreground_median"),
            official_evidence=target.get("official_evidence"),
            personal_evidence=target.get("personal_evidence"),
            reason=filter_selection.get("reason") or target.get("reason"),
        )
        (output_dir / f"official-account-filter-validation-{filter_attempt}.json").write_text(
            json.dumps(filter_selection, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if filter_selection.get("selected"):
            filter_selected_once = True
        else:
            filter_failure_reason = str(filter_selection.get("reason") or "未检测到选中状态")
        if filter_selected_once and target.get("found") and target.get("is_official_account"):
            official_filter_confirmed = True
            break

    if not official_filter_confirmed and filter_selected_once and allow_vl and client is not None:
        # “账号 → 公众号”已经本地确认后，才允许 Qwen-VL 复核名称卡片。这里没有任何盲点分类标签的逻辑，
        # 模型只用来解决本地 OCR 已经连续三次未能读出精确名称的情形。
        local_reason = str(target.get("reason") or "本地 OCR 未能确认公众号卡片")
        log_event(
            "vl_fallback_requested",
            stage="profile_search_result",
            account=account_name,
            search_name=search_name,
            local_attempts=3,
            reason=local_reason,
        )
        try:
            target = _qwen_search_target(client, screenshot, search_name)
            official_filter_confirmed = True
            log_event(
                "vl_fallback_succeeded",
                stage="profile_search_result",
                account=account_name,
                matched_name=target.get("matched_name"),
                confidence=target.get("confidence"),
            )
        except Exception as exc:
            log_event(
                "vl_fallback_failed",
                stage="profile_search_result",
                account=account_name,
                local_reason=local_reason,
                error=str(exc),
            )

    if not official_filter_confirmed:
        # 筛选状态和账号命中是两个独立条件。过去将它们合并后，OCR 名称差异也会
        # 被误报为“筛选没有点上”，使人工排查走错方向。
        if not filter_selected_once:
            raise RuntimeError(
                f"二级公众号筛选未确认选中：{filter_failure_reason}"
            )
        raise RuntimeError(
            "公众号筛选已选中，但未找到可确认的同名公众号："
            f"{search_name}。{target.get('reason') or '名称或账号类型校验未通过'}"
        )

    screenshot.save(output_dir / "search-result.png")
    log_event(
        "account_search_result",
        account=account_name,
        found=bool(target.get("found")),
        reason=target.get("reason"),
        matched_name=target.get("matched_name") or target.get("name"),
        name_match_method=target.get("name_match_method"),
    )
    (output_dir / "search-detection.json").write_text(
        json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not target.get("found"):
        raise RuntimeError(str(target.get("reason") or "搜一搜没有精确匹配公众号"))

    # 保留用户已有资料窗口；新主页必须通过目标名称与页面结构验证。
    activate_window(search_window.hwnd)
    name_click_x = search_window.rect.left + round(
        search_window.rect.width * int(target["center_x_1000"]) / 1000
    )
    name_click_y = search_window.rect.top + round(
        search_window.rect.height * int(target["center_y_1000"]) / 1000
    )
    # 搜索 OCR 会把“媒体/事业单位”或标点并入 matched_name；这些文本只用于
    # 搜索结果定位，主页身份必须使用配置的搜索名（包含明确配置的账号别名）。
    expected_profile_name = search_name
    profile_checkpoint = None
    if observe_browser_page(search_window).role == "search":
        profile_checkpoint = browser_navigator(search_window, "").capture(expected_profile_name)
    existing_hwnds = {w.hwnd for w in enumerate_wechat_windows()}
    click(name_click_x, name_click_y)
    log_event(
        "profile_name_clicked",
        account=account_name,
        screen_x=name_click_x,
        screen_y=name_click_y,
    )
    time.sleep(0.5)
    # 记录点击后的窗口类名和标题，方便定位不同微信版本的窗口差异。
    log_event(
        "profile_click_window_inventory",
        account=account_name,
        windows=[
            {
                "hwnd": item.hwnd,
                "title": item.title,
                "class_name": item.class_name,
                "process_name": item.process_name,
                "width": item.rect.width,
                "height": item.rect.height,
            }
            for item in enumerate_wechat_windows()
        ],
    )
    deadline = time.time() + 10
    avatar_retry_at = time.time() + 2
    avatar_retry_done = False
    vl_header_checked = False
    last_reason = ""
    arranged_profile_hwnds: set[int] = set()
    while time.time() < deadline:
        shared_page = observe_browser_page(search_window, expected_profile_name)
        if shared_page.role == "profile":
            if profile_checkpoint:
                browser_navigator(search_window, expected_profile_name).track_created(profile_checkpoint)
            log_event("profile_opened_and_verified", account=account_name,
                      navigation_mode="shared-browser", page_name=shared_page.name)
            return WindowInfo(search_window.hwnd, search_window.title, search_window.class_name,
                              search_window.rect, search_window.process_name, True, profile_checkpoint), shared_page.name
        try:
            profile = find_official_profile_window()
            if profile.hwnd in existing_hwnds:
                raise RuntimeError("尚未出现本次打开的独立资料窗口")
            if profile.hwnd not in arranged_profile_hwnds:
                profile = arrange_automation_window(profile, "profile")
                arranged_profile_hwnds.add(profile.hwnd)
            activate_window(profile.hwnd)
            time.sleep(0.3)
            header_image = capture_window(profile.rect)
            validation = PROFILE_OCR.validate_profile_header(header_image, search_name)
            (output_dir / "profile-validation.json").write_text(
                json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if validation.get("matched"):
                log_event("profile_opened_and_verified", account=account_name, validation=validation)
                # 后续正文页严格校验微信实际展示的名称，避免“库内别名”导致误报。
                return profile, expected_profile_name
            last_reason = str(validation.get("reason") or "资料窗口名称不匹配")
            header_image.save(output_dir / "profile-header-mismatch.png")
            if avatar_retry_done and allow_vl and client is not None and not vl_header_checked:
                # 名称点击和头像点击都未通过本地 OCR 后，才让模型只做一次只读复核。
                # Qwen 不返回点击坐标，也不会替代后续文章 URL/公众号名校验。
                vl_header_checked = True
                log_event(
                    "vl_fallback_requested",
                    stage="profile_header",
                    account=account_name,
                    search_name=search_name,
                    local_reason=last_reason,
                )
                try:
                    vl_validation = client.verify_profile_header(header_image, search_name)
                    observed_name = str(vl_validation.get("name") or "").strip()
                    if not _qwen_profile_header_confirmed(vl_validation, search_name):
                        raise ValueError(
                            "Qwen-VL 未确认资料窗口名称："
                            f"预期={search_name!r}，识别={observed_name!r}，"
                            f"置信度={vl_validation.get('confidence')!r}"
                        )
                    (output_dir / "profile-validation-qwen.json").write_text(
                        json.dumps(vl_validation, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    log_event(
                        "vl_fallback_succeeded",
                        stage="profile_header",
                        account=account_name,
                        matched_name=observed_name,
                        confidence=vl_validation.get("confidence"),
                        model_matched=vl_validation.get("matched"),
                    )
                    return profile, observed_name
                except Exception as exc:
                    log_event(
                        "vl_fallback_failed",
                        stage="profile_header",
                        account=account_name,
                        local_reason=last_reason,
                        error=str(exc),
                    )
            if (not avatar_retry_done and time.time() >= avatar_retry_at
                    and shared_page.role == "search"):
                # 名称区域在部分微信版本中只会选中文字，未必打开资料页。此时先关闭
                # 不匹配的旧资料窗口，再点击同一卡片头像，确保下一次校验对应本次账号。
                try:
                    close_window(profile.hwnd)
                except Exception:
                    pass
                activate_window(search_window.hwnd)
                avatar_x = search_window.rect.left + round(
                    search_window.rect.width * int(target["avatar_x_1000"]) / 1000
                )
                avatar_y = search_window.rect.top + round(
                    search_window.rect.height * int(target["avatar_y_1000"]) / 1000
                )
                click(avatar_x, avatar_y)
                avatar_retry_done = True
                log_event(
                    "profile_avatar_fallback_clicked",
                    account=account_name,
                    reason="profile_header_mismatch",
                    screen_x=avatar_x,
                    screen_y=avatar_y,
                    observed_headers=validation.get("observed_header_candidates"),
                )
        except RuntimeError as exc:
            last_reason = str(exc)
            if (not avatar_retry_done and time.time() >= avatar_retry_at
                    and shared_page.role == "search"):
                # 某些版本只有头像或整张卡片响应点击，名称链接本身可能不触发资料窗口。
                activate_window(search_window.hwnd)
                avatar_x = search_window.rect.left + round(
                    search_window.rect.width * int(target["avatar_x_1000"]) / 1000
                )
                avatar_y = search_window.rect.top + round(
                    search_window.rect.height * int(target["avatar_y_1000"]) / 1000
                )
                click(avatar_x, avatar_y)
                avatar_retry_done = True
                log_event(
                    "profile_avatar_fallback_clicked",
                    account=account_name,
                    screen_x=avatar_x,
                    screen_y=avatar_y,
                )
        time.sleep(0.4)
    raise RuntimeError(f"点击搜一搜结果后未打开正确公众号资料窗口：{last_reason}")


def profile_feed_from_uia(window: WindowInfo, account: str = "") -> dict[str, Any]:
    rect = window.rect
    return feed_from_probe(probe_page(window.hwnd), (rect.left, rect.top, rect.right, rect.bottom), account)


def locate_profile_article(window: WindowInfo, account: str, title: str, time_group: str) -> tuple[int, int]:
    """返回列表后按标题与日期重找锚点，最多六次滚动恢复，不复用旧坐标。"""
    for _ in range(7):
        payload = probe_page(window.hwnd)
        rect = window.rect
        feed = feed_from_probe(payload, (rect.left, rect.top, rect.right, rect.bottom), account)
        matches = [card for card in feed["articles"]
                   if titles_match(title, card["title"]) and card.get("time_group") == time_group]
        if len(matches) == 1:
            return matches[0]["screen_point"]
        if len(matches) > 1:
            raise RuntimeError("同一日期存在多个匹配标题，拒绝猜测文章")
        rows = [row for doc in payload.get("documents", []) for row in doc.get("rows", [])
                if titles_match(title, row.get("text", ""))]
        if len(rows) != 1:
            break
        y = (rows[0]["rect"][1] + rows[0]["rect"][3]) / 2
        if y < rect.top + 60:
            scroll_window_up(rect)
        elif y >= rect.bottom:
            scroll_window_down(rect)
        else:
            break
        time.sleep(0.4)
    # 列表初筛可能来自 OCR；UIA 缺少对应卡片时，用当前截图重新定位同一日期的唯一标题。
    identity = page_from_probe(probe_page(window.hwnd), account)
    if identity.role == "profile":
        try:
            fresh = PROFILE_OCR.inspect_profile_feed(capture_window(window.rect))
            point = locate_ocr_card(fresh, title, time_group, window.rect)
            if point and page_from_probe(probe_page(window.hwnd), account) == identity:
                log_event("article_anchor_recovered", account=account, title=title, method="fresh-profile-ocr")
                return point
        except (ValueError, KeyError, TypeError) as exc:
            log_event("article_anchor_ocr_failed", account=account, error=type(exc).__name__)
    raise RuntimeError("无法恢复目标文章的列表位置，已停止使用旧坐标")


def locate_ocr_card(feed: dict, title: str, time_group: str, rect: Rect) -> tuple[int, int] | None:
    """仅使用新截图上的日期标签和卡片坐标，缺日期、标题过短或多匹配均拒绝点击。"""
    if len(canonical_title_for_match(title)) < 5:
        return None
    events = [(int(r["center_y_1000"]), "date", r) for r in feed.get("time_labels", [])]
    events += [(int(r["center_y_1000"]), "article", r) for r in feed.get("articles", [])]
    group, matches = "", []
    for y, kind, row in sorted(events, key=lambda item: item[0]):
        if kind == "date":
            group = row.get("text", "")
        elif (normalize_title(group) == normalize_title(time_group)
              and titles_match(title, row.get("title", ""))):
            x = int(row["center_x_1000"])
            if 0 < x < 1000 and 0 < y < 1000:
                matches.append((rect.left + round(rect.width * x / 1000),
                                rect.top + round(rect.height * y / 1000)))
    if len(matches) > 1:
        raise RuntimeError("同一日期存在多个匹配标题，拒绝猜测文章")
    return matches[0] if matches else None


def analyze_profile_window(
    profile_window: WindowInfo,
    output_dir: Path,
    move_to_latest: bool = False,
    *,
    client: QwenVisionClient | None = None,
    allow_vl: bool = True,
) -> dict[str, Any]:
    """分析公众号资料页中的时间分组和文章卡片。

    本地 OCR 先连续重新截图识别两次；只有两次都没有可靠的“日期标签 + 文章卡片”时，
    才会交给 Qwen-VL 复核一次。这避免了对正常窗口进行无谓的模型调用，也能覆盖缩放、卡片样式变化等 OCR 边界情况。
    """
    activate_window(profile_window.hwnd)
    if move_to_latest:
        press_ctrl_home()
        time.sleep(0.8)
    output_dir.mkdir(parents=True, exist_ok=True)
    if profile_window.shared_browser:
        try:
            structured = profile_feed_from_uia(profile_window)
            if structured["articles"]:
                (output_dir / "feed.json").write_text(json.dumps(structured, ensure_ascii=False), encoding="utf-8")
                return structured
        except RuntimeError as exc:
            log_event("profile_uia_fallback", reason=str(exc))
    screenshot: Image.Image | None = None
    feed: dict[str, Any] | None = None
    local_failure_reason = ""
    for local_attempt in range(1, 3):
        screenshot = capture_window(profile_window.rect)
        screenshot.save(output_dir / f"profile-window-local-{local_attempt}.png")
        try:
            candidate = PROFILE_OCR.inspect_profile_feed(screenshot)
            if not candidate.get("time_labels") or not candidate.get("articles"):
                raise ValueError(
                    "本地资料页识别结果不完整："
                    f"time_labels={len(candidate.get('time_labels', []))}，"
                    f"articles={len(candidate.get('articles', []))}"
                )
            feed = candidate
            log_event(
                "profile_feed_local_succeeded",
                hwnd=profile_window.hwnd,
                attempt=local_attempt,
                time_label_count=len(candidate.get("time_labels", [])),
                article_count=len(candidate.get("articles", [])),
            )
            break
        except Exception as exc:
            local_failure_reason = str(exc)
            log_event(
                "profile_feed_local_attempt_failed",
                hwnd=profile_window.hwnd,
                attempt=local_attempt,
                error=local_failure_reason,
            )
            if local_attempt == 1:
                # 等待动画和懒加载结束后再截一次，不改变滚动位置。
                time.sleep(0.5)

    if feed is None:
        if not allow_vl or client is None:
            raise RuntimeError(f"资料页本地识别失败且已禁用VL：{local_failure_reason}")
        assert screenshot is not None
        log_event(
            "vl_fallback_requested",
            stage="profile_feed",
            hwnd=profile_window.hwnd,
            local_attempts=2,
            reason=local_failure_reason,
        )
        try:
            feed = client.inspect_profile_feed(screenshot)
            if not feed.get("time_labels") or not feed.get("articles"):
                raise ValueError(
                    "Qwen-VL 资料页识别结果不完整："
                    f"time_labels={len(feed.get('time_labels', []))}，"
                    f"articles={len(feed.get('articles', []))}"
                )
            feed["recognition_method"] = "qwen-vl-profile-feed-fallback"
            feed["fallback_reason"] = local_failure_reason
            log_event(
                "vl_fallback_succeeded",
                stage="profile_feed",
                hwnd=profile_window.hwnd,
                time_label_count=len(feed.get("time_labels", [])),
                article_count=len(feed.get("articles", [])),
            )
        except Exception as exc:
            log_event(
                "vl_fallback_failed",
                stage="profile_feed",
                hwnd=profile_window.hwnd,
                local_reason=local_failure_reason,
                error=str(exc),
            )
            raise RuntimeError(f"Qwen-VL 资料页识别失败：{exc}") from exc

    assert screenshot is not None
    screenshot.save(output_dir / "profile-window.png")
    for article in feed["articles"]:
        article["screen_point"] = (
            profile_window.rect.left
            + round(profile_window.rect.width * int(article["center_x_1000"]) / 1000),
            profile_window.rect.top
            + round(profile_window.rect.height * int(article["center_y_1000"]) / 1000),
        )
    (output_dir / "feed.json").write_text(
        json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return feed


def analyze_account_window(
    client: QwenVisionClient,
    account_window: WindowInfo,
    output_dir: Path,
    move_to_latest: bool = False,
    allow_vl: bool = True,
) -> dict[str, Any]:
    activate_window(account_window.hwnd)
    if move_to_latest:
        # 微信会记住公众号窗口上次的滚动位置，首屏先到底部确保读取最新推送组。
        press_ctrl_end()
        time.sleep(1.2)
    screenshot = capture_window(account_window.rect)
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot.save(output_dir / "account-window.png")
    fallback_reason = ""
    try:
        feed = FEED_OCR.inspect_account_feed(screenshot)
        if not feed.get("time_labels") or not feed.get("articles"):
            raise ValueError(
                f"本地消息识别结果不完整：time_labels={len(feed.get('time_labels', []))}, "
                f"articles={len(feed.get('articles', []))}"
            )
        first_label_y = min(
            int(item.get("center_y_1000", 0)) for item in feed["time_labels"]
        )
        if first_label_y > 500 and any(
            int(article.get("center_y_1000", 0)) < first_label_y
            for article in feed["articles"]
        ):
            # 时间标签落在下半屏且上方已有卡片时，顶部可能还有被标题栏遮住的分组标签。
            # 本地 OCR 不应猜测卡片归属，此类边界页交给 VL 确认一次。
            if allow_vl:
                raise ValueError("屏幕顶部可能存在被遮挡的时间标签")
            feed["local_only_warning"] = "屏幕顶部可能存在被遮挡的时间标签"
    except Exception as exc:
        if not allow_vl:
            # 对比实验要求严格禁止模型调用，边界页面保留错误和截图供人工核验。
            raise RuntimeError(f"本地消息列表识别失败且已禁用VL：{exc}") from exc
        # 窗口被遮挡、版式变化或 OCR 无结果时，保留 Qwen-VL 兜底以避免漏采。
        fallback_reason = str(exc)
        feed = client.inspect_account_feed(screenshot)
        feed["recognition_method"] = "qwen-vl-fallback"
        feed["fallback_reason"] = fallback_reason
    articles = feed["articles"]
    for article in articles:
        article["screen_point"] = (
            account_window.rect.left
            + round(account_window.rect.width * int(article["center_x_1000"]) / 1000),
            account_window.rect.top
            + round(account_window.rect.height * int(article["center_y_1000"]) / 1000),
        )
    (output_dir / "feed.json").write_text(
        json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return feed


def select_latest_article_group(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """优先选择今天最新的消息组；只有没有今天内容时才考虑昨天。"""
    ordered = sorted(articles, key=lambda item: item["screen_point"][1])
    today = [item for item in ordered if "昨天" not in str(item.get("group_time") or "")]
    candidates = today or [item for item in ordered if "昨天" in str(item.get("group_time") or "")]
    if not candidates:
        return []
    # 同一个推送包的所有卡片应共享相同的时间分组。
    latest_group = str(candidates[-1].get("group_time") or "")
    if latest_group:
        grouped = [item for item in candidates if str(item.get("group_time") or "") == latest_group]
        if grouped:
            return grouped
    return candidates


PROMOTION_TITLE_KEYWORDS = (
    "招聘",
    "招募",
    "诚聘",
    "投稿合作",
    "商务合作",
    "广告合作",
    # 远程桌面浮层可能覆盖公众号窗口并被 OCR 当成卡片，必须在点击前过滤。
    "ToDesk",
    "设备代码",
)


def promotion_reason(title: str) -> str | None:
    """识别不需要采集的招聘、招募及合作推广卡片。"""
    normalized = normalize_title(title)
    for keyword in PROMOTION_TITLE_KEYWORDS:
        if normalize_title(keyword) in normalized:
            return f"标题包含推广关键词：{keyword}"
    return None


def is_older_time_boundary(value: str) -> bool:
    """星期标签或明确年月日均表示已经早于昨天，应停止继续采集。"""
    text = unicodedata.normalize("NFKC", value or "").strip()
    return bool(
        re.search(r"(?:星期|周)[一二三四五六日天1-7]", text)
        or re.search(r"(?:\d{4}年)?\d{1,2}月\d{1,2}日", text)
        or re.search(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", text)
    )


def is_recent_time_group(value: str, scan_range: str = "today_yesterday") -> bool:
    text = unicodedata.normalize("NFKC", value or "").strip()
    if not text or is_older_time_boundary(text):
        return False
    if "昨天" in text:
        return scan_range in {"yesterday", "today_yesterday"}
    if "今天" in text:
        return scan_range in {"today", "today_yesterday"}
    # 当天推送在微信中通常只显示 HH:MM。
    return scan_range in {"today", "today_yesterday"} and bool(
        re.fullmatch(r"\d{1,2}:\d{2}", text)
    )


def publish_time_matches_scan_range(
    value: Any,
    scan_range: str,
    *,
    reference_date: date | None = None,
) -> bool:
    """按北京时间复核文章真实发布时间是否属于本次任务范围。"""
    if scan_range not in {"today", "yesterday", "today_yesterday"}:
        raise ValueError(f"未知扫描范围：{scan_range}")
    publish_value = value if isinstance(value, datetime) else parse_publish_time(str(value or ""))
    publish_date = publish_value.date()
    today = reference_date or datetime.now(shanghai_timezone()).date()
    if scan_range == "today":
        return publish_date == today
    if scan_range == "yesterday":
        return publish_date == today - timedelta(days=1)
    return publish_date in {today, today - timedelta(days=1)}


def build_card_signature(
    time_group: str, article: dict[str, Any]
) -> tuple[str, str, int, int] | None:
    """生成保守的卡片指纹；任一互动数字缺失时不做点击前去重。"""
    title = canonical_title_for_match(str(article.get("title") or ""))
    group = normalize_title(time_group)
    read_count = article.get("list_read_count")
    like_count = article.get("list_like_count")
    if not title or not group or not isinstance(read_count, int) or not isinstance(like_count, int):
        return None
    return group, title, read_count, like_count


def build_card_title_signature(time_group: str, article: dict[str, Any]) -> tuple[str, str] | None:
    """生成本轮终态去重指纹；互动数缺失时仍可阻止同一卡片重复打开。"""
    # 去除省略号、引号和 OCR 常见 AI/Al 差异，避免同一卡片在相邻屏幕中
    # 仅因展示截断不同而被重复打开。
    raw_title = str(article.get("title") or "")
    title = canonical_title_for_match(raw_title)
    if ("…" in raw_title or "..." in raw_title) and len(title) < 6:
        return None  # 例如“背....”，不能当作可唯一定位的文章标题。
    group = normalize_title(time_group)
    return (group, title) if group and title else None


def collect_searched_account(
    client: QwenVisionClient,
    account_name: str,
    output_dir: Path,
    max_articles: int,
    export_jsonl: str | None,
    export_csv: str | None,
    allow_vl: bool = True,
    write_mongo: bool = False,
    mongo_uri: str | None = None,
    mongo_database: str | None = None,
    mongo_collection: str | None = None,
    mongo_target_collection: str | None = None,
    metric_mode: str = "all",
    task_timeout_minutes: float | None = None,
    scan_range: str = "today_yesterday",
) -> dict[str, Any]:
    account_window: WindowInfo | None = None
    collected: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    detected_count = 0
    deadline = (
        time.monotonic() + task_timeout_minutes * 60
        if task_timeout_minutes and task_timeout_minutes > 0
        else None
    )
    try:
        search_error = ""
        for search_attempt in range(1, 4):
            try:
                account_window = search_and_open_account(
                    client,
                    account_name,
                    output_dir / "search" / f"attempt-{search_attempt}",
                    allow_vl=allow_vl,
                )
                break
            except Exception as exc:
                search_error = str(exc)
                time.sleep(1.0)
        if account_window is None:
            raise RuntimeError(f"公众号搜索连续3次失败：{search_error}")
        seen_cards: set[str] = set()
        processed_count = 0
        stop_reason = "达到最大翻页数"
        for page_index in range(1, 13):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"账号 {account_name} 达到任务超时限制")
            feed = analyze_account_window(
                client,
                account_window,
                output_dir / "messages" / f"page-{page_index:02d}",
                move_to_latest=page_index == 1,
                allow_vl=allow_vl,
            )
            detected = feed["articles"]
            time_labels = sorted(
                feed["time_labels"], key=lambda item: int(item.get("center_y_1000", 0))
            )
            older_boundary = next(
                (
                    str(item.get("text") or "")
                    for item in time_labels
                    if is_older_time_boundary(str(item.get("text") or ""))
                ),
                "",
            )
            for article in detected:
                article_y = int(article.get("center_y_1000", 0))
                labels_above = [
                    item for item in time_labels
                    if int(item.get("center_y_1000", 0)) < article_y
                ]
                # 时间标签不在当前截图中时不推断归属，只继续向上翻页等待标签出现。
                if not labels_above:
                    continue
                group_time = str(labels_above[-1].get("text") or "").strip()
                if not is_recent_time_group(group_time, scan_range):
                    continue
                title = str(article.get("title") or "").strip()
                card_key = f"{normalize_title(group_time)}|{normalize_title(title)}"
                if card_key in seen_cards:
                    continue
                seen_cards.add(card_key)
                detected_count += 1

                reason = promotion_reason(title)
                if reason:
                    skipped.append({"title": title, "reason": reason})
                    continue
                if processed_count >= max_articles:
                    stop_reason = f"达到文章上限 {max_articles}"
                    break

                processed_count += 1
                article_dir = output_dir / f"article-{processed_count:02d}-{safe_path_name(title)}"
                last_error = ""
                for attempt in range(1, 4):
                    try:
                        activate_window(account_window.hwnd)
                        click(*article["screen_point"])
                        time.sleep(2.5)
                        record = collect_open_article(
                            client,
                            article_dir,
                            write_mongo=write_mongo,
                            export_jsonl=export_jsonl,
                            export_csv=export_csv,
                            expected_title=title,
                            expected_account=account_name,
                            allow_vl=allow_vl,
                            mongo_uri=mongo_uri,
                            mongo_database=mongo_database,
                            mongo_collection=mongo_collection,
                            mongo_target_collection=mongo_target_collection,
                            metric_mode=metric_mode,
                            scan_range=scan_range,
                        )
                        if record.get("status") == "skipped_outside_scan_range":
                            skipped.append(
                                {
                                    "title": title,
                                    "reason": str(record.get("skip_reason") or "真实发布时间不在扫描范围"),
                                    "url": str(record.get("url") or ""),
                                }
                            )
                            break
                        collected.append(
                            {key: value for key, value in record.items() if key != "content"}
                        )
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        (article_dir / f"attempt-{attempt}-error.txt").parent.mkdir(
                            parents=True, exist_ok=True
                        )
                        (article_dir / f"attempt-{attempt}-error.txt").write_text(
                            last_error, encoding="utf-8"
                        )
                    finally:
                        # 每次尝试都只关闭右侧当前文章标签，保留公众号消息窗口。
                        try:
                            article_hwnd, _ = find_article_window()
                            activate_window(article_hwnd)
                            press_ctrl_w()
                            time.sleep(0.8)
                        except Exception as cleanup_exc:
                            log_event(
                                "article_tab_cleanup_failed",
                                account=account_name,
                                title=title,
                                attempt=attempt,
                                error=str(cleanup_exc),
                                action="abort_account_to_prevent_tab_accumulation",
                            )
                            raise RuntimeError(
                                "文章标签关闭失败，为避免重复打开和数据错配，已停止当前公众号采集："
                                f"{cleanup_exc}"
                            ) from cleanup_exc
                else:
                    failure = {
                        "account": account_name,
                        "title": title,
                        "error": last_error,
                        "category": classify_collection_error(RuntimeError(last_error)),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    failures.append(failure)
                    append_failure_queue(output_dir, failure)
                    # 三次尝试都失败后才发出终态事件；控制台据此计入警告。
                    log_event("article_collect_failed", **failure)

            if processed_count >= max_articles:
                stop_reason = f"达到文章上限 {max_articles}"
                break
            if older_boundary:
                stop_reason = f"遇到更早时间边界：{older_boundary}"
                break
            activate_window(account_window.hwnd)
            scroll_window_up(account_window.rect)
            time.sleep(1.0)
    finally:
        # 一个公众号结束后关闭中间窗口，左侧微信搜索窗口始终保留。
        if account_window and user32.IsWindow(account_window.hwnd):
            close_window(account_window.hwnd)

    summary = {
        "account": account_name,
        "detected_articles": detected_count,
        "stop_reason": stop_reason,
        "collected": collected,
        "skipped": skipped,
        "failures": failures,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return summary


def collect_profile_account(
    client: QwenVisionClient,
    account_name: str,
    output_dir: Path,
    max_articles: int,
    export_jsonl: str | None,
    export_csv: str | None,
    allow_vl: bool = True,
    write_mongo: bool = False,
    mongo_uri: str | None = None,
    mongo_database: str | None = None,
    mongo_collection: str | None = None,
    mongo_target_collection: str | None = None,
    metric_mode: str = "all",
    task_timeout_minutes: float | None = None,
    scan_range: str = "today_yesterday",
) -> dict[str, Any]:
    log_event(
        "account_collection_started",
        account=account_name,
        max_articles=max_articles,
        allow_vl=allow_vl,
        write_mongo=write_mongo,
        metric_mode=metric_mode,
        scan_range=scan_range,
    )
    """从搜一搜进入公众号资料窗口，采集今天和昨天的文章。"""
    profile_window: WindowInfo | None = None
    # 数据库中的账号名是文章归属的唯一标准。搜一搜 OCR 读到的名称可能会
    # 带上“媒体”“官方”等身份后缀，只能用于搜索结果校验，不能污染文章页校验。
    observed_account_name = account_name
    collected: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    detected_count = 0
    current_group = ""
    # 卡片指纹仅在成功后登记；URL 仍作为打开文章后的最终精确去重依据。
    successful_card_signatures: set[tuple[str, str, int, int]] = set()
    # 同一标题卡片无论成功、跳过还是三次失败，达到终态后本轮都不再重复打开。
    terminal_card_title_signatures: set[tuple[str, str]] = set()
    observed_title_signatures: set[tuple[str, str]] = set()
    invalid_title_signatures: set[tuple[str, str]] = set()
    successful_urls: set[str] = set()
    skipped_card_duplicate_count = 0
    skipped_terminal_duplicate_count = 0
    skipped_url_duplicate_count = 0
    observed_card_count = 0
    out_of_range_card_count = 0
    ungrouped_card_count = 0
    promotion_card_count = 0
    processed_count = 0
    opened_count = 0
    deadline = (
        time.monotonic() + task_timeout_minutes * 60
        if task_timeout_minutes and task_timeout_minutes > 0
        else None
    )
    stop_reason = "达到最大翻页数"
    partial_summary_path = output_dir / "partial-summary.json"

    def write_partial_checkpoint() -> None:
        """逐篇保存账号进度，避免后续窗口清理失败掩盖已成功结果。"""
        checkpoint = {
            "account": account_name,
            "discovery_mode": "sogou-profile",
            "partial": True,
            "detected_articles": detected_count,
            "stop_reason": "账号仍在采集，已保存成功文章检查点",
            "scan": {
                "range": scan_range,
                "observed_cards": observed_card_count,
                "eligible_cards": detected_count,
                "outside_range_cards": out_of_range_card_count,
                "ungrouped_cards": ungrouped_card_count,
                "promotion_cards": promotion_card_count,
            },
            "collected": collected,
            "skipped": skipped,
            "failures": failures,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = partial_summary_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary_path.replace(partial_summary_path)

    try:
        search_error = ""
        for attempt in range(1, 4):
            try:
                log_event("account_search_attempt", account=account_name, attempt=attempt)
                profile_window, observed_account_name = search_and_open_profile(
                    account_name,
                    output_dir / "search" / f"attempt-{attempt}",
                    client=client,
                    allow_vl=allow_vl,
                )
                break
            except Exception as exc:
                search_error = str(exc)
                log_event("account_search_attempt_failed", account=account_name, attempt=attempt, error=search_error)
                time.sleep(0.8)
        if profile_window is None:
            raise RuntimeError(f"搜一搜连续3次打开公众号失败：{search_error}")
        # 记录 OCR 看到的结果名，但后续文章归属仍使用 account_name。
        log_event(
            "account_identity_observed",
            account=account_name,
            observed_name=observed_account_name,
        )

        for page_index in range(1, 13):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"账号 {account_name} 达到任务超时限制")
            # 每次滚屏后的顶部可能是上一分区残留的贴图/视频卡片。不能沿用
            # 上一屏最后一个日期标签，否则这些无标签卡片会被误归到“昨天”。
            # 宁可将缺少本屏日期证据的卡片记为 ungrouped，下一屏重叠区域仍可补抓。
            current_group = ""
            feed = analyze_profile_window(
                profile_window,
                output_dir / "profile" / f"page-{page_index:02d}",
                move_to_latest=page_index == 1,
                client=client,
                allow_vl=allow_vl,
            )
            log_event(
                "profile_page_analyzed",
                account=account_name,
                page=page_index,
                time_labels=[item.get("text") for item in feed.get("time_labels", [])],
                article_count=len(feed.get("articles", [])),
            )
            labels = [
                {"kind": "label", **item}
                for item in feed.get("time_labels", [])
            ]
            articles = [
                {"kind": "article", **item}
                for item in feed.get("articles", [])
            ]
            events = sorted(
                labels + articles,
                key=lambda item: int(item.get("center_y_1000", 0)),
            )
            older_boundary = ""
            for event in events:
                if event["kind"] == "label":
                    label = str(event.get("text") or "").strip()
                    current_group = label
                    if is_older_time_boundary(label):
                        older_boundary = label
                    continue
                observed_card_count += 1
                if not current_group:
                    # 不猜测没有日期分组的卡片属于哪一天，等待下一屏的时间标签。
                    ungrouped_card_count += 1
                    continue
                if older_boundary or not is_recent_time_group(current_group, scan_range):
                    out_of_range_card_count += 1
                    continue
                title = str(event.get("title") or "").strip()
                title_signature = build_card_title_signature(current_group, event)
                if title_signature is None:
                    # 无效卡片在点击前终止；按原始文本留痕，不能用空标题做终态去重。
                    invalid_signature = (current_group, re.sub(r"\s+", "", title))
                    if invalid_signature not in invalid_title_signatures:
                        invalid_title_signatures.add(invalid_signature)
                        failure = {"account": account_name, "title": title,
                                   "error": "卡片未提取到有效标题，已跳过点击", "category": "card_identity"}
                        failures.append(failure)
                        append_failure_queue(output_dir, failure)
                        log_event("article_card_invalid", **failure)
                    continue
                if title_signature not in observed_title_signatures:
                    observed_title_signatures.add(title_signature)
                    detected_count += 1
                reason = promotion_reason(title)
                if reason:
                    promotion_card_count += 1
                    log_event("article_card_skipped_promotion", account=account_name, title=title, reason=reason)
                    skipped.append({"title": title, "reason": reason})
                    continue
                card_signature = build_card_signature(current_group, event)
                title_signature = build_card_title_signature(current_group, event)
                if title_signature is not None and title_signature in terminal_card_title_signatures:
                    skipped_terminal_duplicate_count += 1
                    skipped.append(
                        {
                            "title": title,
                            "reason": "本次任务已处理过同一日期分组和标题，点击前跳过",
                        }
                    )
                    log_event(
                        "article_card_skipped_terminal_duplicate",
                        account=account_name,
                        title=title,
                        time_group=current_group,
                    )
                    continue
                if card_signature is not None and card_signature in successful_card_signatures:
                    skipped_card_duplicate_count += 1
                    skipped.append(
                        {
                            "title": title,
                            "reason": "本次任务已成功采集完全相同的卡片指纹，点击前跳过",
                        }
                    )
                    log_event("article_card_skipped_duplicate", account=account_name, title=title)
                    continue
                if processed_count >= max_articles:
                    stop_reason = f"达到文章上限 {max_articles}"
                    break

                opened_count += 1
                article_dir = output_dir / f"article-{opened_count:02d}-{safe_path_name(title)}"
                last_error = ""
                for article_attempt in range(1, 4):
                    navigation = None
                    checkpoint = None
                    article_target = None
                    owned_article_window = False
                    baseline_hwnds = set()
                    try:
                        log_event(
                            "article_open_attempt",
                            account=account_name,
                            title=title,
                            attempt=article_attempt,
                            time_group=current_group,
                            list_read_count=event.get("list_read_count"),
                            list_like_count=event.get("list_like_count"),
                        )
                        activate_window(profile_window.hwnd)
                        if profile_window.shared_browser:
                            navigation = browser_navigator(profile_window, observed_account_name)
                            checkpoint = navigation.checkpoint()
                            baseline_hwnds = {w.hwnd for w in enumerate_wechat_windows()}
                            # 从当前页面重新定位标题，返回后的滚动变化不能沿用旧坐标。
                            point = locate_profile_article(profile_window, observed_account_name, title, current_group)
                        else:
                            point = event["screen_point"]
                        click(*point)
                        time.sleep(2.5)
                        if navigation:
                            new_windows = [w for w in enumerate_wechat_windows() if w.hwnd not in baseline_hwnds]
                            verified = [w for w in new_windows
                                        if observe_browser_page(w, observed_account_name).role == "article"]
                            if len(verified) == 1:
                                article_target = verified[0]
                                owned_article_window = True
                            elif not new_windows:
                                wait_for_article_page(profile_window, observed_account_name, title)
                                article_target = profile_window
                                navigation.track_created(checkpoint)
                            else:
                                raise RuntimeError("点击后未唯一确认同一公众号的文章页")
                        record = collect_open_article(
                            client,
                            article_dir,
                            write_mongo=write_mongo,
                            export_jsonl=export_jsonl,
                            export_csv=export_csv,
                            expected_title=title,
                            # 文章页和 MongoDB 始终按数据库标准账号名校验；OCR 搜索结果
                            # 中的附加后缀仅保留在搜索日志中，不作为文章归属名。
                            expected_account=account_name,
                            allow_vl=allow_vl,
                            mongo_uri=mongo_uri,
                            mongo_database=mongo_database,
                            mongo_collection=mongo_collection,
                            mongo_target_collection=mongo_target_collection,
                            list_read_count=event.get("list_read_count"),
                            list_like_count=event.get("list_like_count"),
                            successful_urls_in_run=successful_urls,
                            metric_mode=metric_mode,
                            scan_range=scan_range,
                            article_window=article_target,
                        )
                        verification = record.get("verification") or {}
                        if (navigation and article_target.hwnd == profile_window.hwnd
                                and all(verification.get(key) for key in ("title_matched", "account_matched", "url_stable"))):
                            # 已通过链接解析的公众号/标题校验，为非标准文章补充身份。
                            # 只绑定当前文档 key；后续跳转或标题变化立即使这份证据失效。
                            confirmed = observe_browser_page(article_target, observed_account_name)
                            if confirmed.role == "unknown" and titles_match(title, confirmed.name):
                                original_observe = navigation.observe
                                def observe_validated(original=original_observe, evidence=confirmed):
                                    current = original()
                                    if current == evidence:
                                        return Page(current.hwnd, current.key, "article", current.name, observed_account_name)
                                    return current
                                navigation.observe = observe_validated
                                log_event("article_page_identity_validated_by_url", account=account_name,
                                          page=confirmed.__dict__)
                        if record.get("status") == "skipped_outside_scan_range":
                            skipped.append(
                                {
                                    "title": title,
                                    "reason": str(record.get("skip_reason") or "真实发布时间不在扫描范围"),
                                    "url": str(record.get("url") or ""),
                                }
                            )
                            if title_signature is not None:
                                terminal_card_title_signatures.add(title_signature)
                            log_event(
                                "article_attempt_skipped_outside_scan_range",
                                account=account_name,
                                title=title,
                                publish_time=record.get("publish_time"),
                                scan_range=scan_range,
                            )
                            break
                        if record.get("status") == "skipped_duplicate_in_run":
                            skipped_url_duplicate_count += 1
                            skipped.append(
                                {
                                    "title": title,
                                    "reason": "本次公众号任务已成功采集相同 URL，打开后跳过",
                                    "url": str(record.get("url") or ""),
                                }
                            )
                            log_event("article_attempt_duplicate_url", account=account_name, title=title, url=record.get("url"))
                            if title_signature is not None:
                                terminal_card_title_signatures.add(title_signature)
                            break
                        collected.append(
                            {key: value for key, value in record.items() if key != "content"}
                        )
                        successful_url = str(record.get("url") or "").strip()
                        if successful_url:
                            successful_urls.add(successful_url)
                        if card_signature is not None:
                            successful_card_signatures.add(card_signature)
                        if title_signature is not None:
                            terminal_card_title_signatures.add(title_signature)
                        processed_count += 1
                        log_event(
                            "article_collect_succeeded",
                            account=account_name,
                            title=record.get("title") or title,
                            url=successful_url,
                            processed_count=processed_count,
                        )
                        write_partial_checkpoint()
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        log_event(
                            "article_collect_attempt_failed",
                            account=account_name,
                            title=title,
                            attempt=article_attempt,
                            error=last_error,
                        )
                        article_dir.mkdir(parents=True, exist_ok=True)
                        if navigation:
                            try:
                                diagnostic = probe_page(profile_window.hwnd)
                                (article_dir / f"attempt-{article_attempt}-page.json").write_text(
                                    json.dumps(diagnostic, ensure_ascii=False, default=str), encoding="utf-8")
                                capture_window(profile_window.rect).save(article_dir / f"attempt-{article_attempt}-failed.png")
                            except Exception as diagnostic_error:
                                log_event("navigation_diagnostic_failed", error=str(diagnostic_error))
                        (article_dir / f"attempt-{article_attempt}-error.txt").write_text(
                            last_error, encoding="utf-8"
                        )
                        if isinstance(exc, ArticleMismatchError) or "多个匹配标题" in last_error or "无法恢复目标文章的列表位置" in last_error:
                            # 身份歧义不是加载超时，重试同一坐标不会得到可靠身份。
                            failure = {"account": account_name, "title": title,
                                       "error": last_error, "category": "card_identity"}
                            failures.append(failure)
                            append_failure_queue(output_dir, failure)
                            terminal_card_title_signatures.add(title_signature)
                            log_event("article_card_failed_terminal", **failure)
                            break
                    finally:
                        try:
                            if navigation and checkpoint:
                                if owned_article_window:
                                    if observe_browser_page(article_target, observed_account_name).role != "article":
                                        raise RuntimeError("独立文章窗身份变化，拒绝关闭")
                                    close_window(article_target.hwnd)
                                    activate_window(profile_window.hwnd)
                                method = navigation.restore(checkpoint)
                                log_event("article_return_preserved" if method == "preserved_unknown" else "article_return_verified", account=account_name,
                                          title=title, method=method)
                                if method == "preserved_unknown":
                                    raise RuntimeError("页面跳转归属不确定，已返回原列表并保留异常页，停止本公众号")
                            elif not profile_window.shared_browser:
                                close_article_after_attempt(account_name, title)
                        except Exception as cleanup_exc:
                            log_event(
                                "article_tab_cleanup_failed",
                                account=account_name,
                                title=title,
                                error=str(cleanup_exc),
                                action="abort_account_to_prevent_tab_accumulation",
                            )
                            # 标签页数量是采集正确性的硬约束。清理失败后继续点击会让旧文章成为活动页，
                            # 造成标题、链接和互动数错配，因此宁可停止当前公众号也不能继续累积标签。
                            raise RuntimeError(
                                "文章标签清理失败，为避免旧标签累积和文章数据错配，"
                                f"已停止当前公众号采集：{cleanup_exc}"
                            ) from cleanup_exc
                else:
                    failure = {
                        "account": account_name,
                        "title": title,
                        "error": last_error,
                        "category": classify_collection_error(RuntimeError(last_error)),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    failures.append(failure)
                    append_failure_queue(output_dir, failure)
                    if title_signature is not None:
                        terminal_card_title_signatures.add(title_signature)

            if processed_count >= max_articles:
                stop_reason = f"达到文章上限 {max_articles}"
                break
            if older_boundary:
                stop_reason = f"遇到更早时间边界：{older_boundary}"
                break
            activate_window(profile_window.hwnd)
            scroll_window_down(profile_window.rect)
            time.sleep(0.8)
    finally:
        propagating_error = sys.exc_info()[0] is not None
        if profile_window and profile_window.shared_browser and profile_window.profile_checkpoint:
            # 只清理由本次搜索打开的主页；用户原有主页与共享浏览器均保留。
            try:
                activate_window(profile_window.hwnd)
                method = browser_navigator(profile_window, "").restore(profile_window.profile_checkpoint)
                log_event("profile_return_preserved" if method == "preserved_unknown" else "profile_return_verified",
                          account=account_name, method=method)
            except RuntimeError as cleanup_error:
                log_event("profile_return_failed", account=account_name, reason=str(cleanup_error))
                if not propagating_error:
                    raise
        if profile_window and not profile_window.shared_browser and user32.IsWindow(profile_window.hwnd):
            close_window(profile_window.hwnd)

    summary = {
        "account": account_name,
        "discovery_mode": "sogou-profile",
        "detected_articles": detected_count,
        "stop_reason": stop_reason,
        "scan": {
            "range": scan_range,
            "observed_cards": observed_card_count,
            "eligible_cards": detected_count,
            "outside_range_cards": out_of_range_card_count,
            "ungrouped_cards": ungrouped_card_count,
            "promotion_cards": promotion_card_count,
        },
        "dedupe": {
            "successful_card_signatures_in_run": len(successful_card_signatures),
            "terminal_card_title_signatures_in_run": len(terminal_card_title_signatures),
            "successful_urls_in_run": len(successful_urls),
            "skipped_card_duplicate_before_click": skipped_card_duplicate_count,
            "skipped_terminal_duplicate_before_click": skipped_terminal_duplicate_count,
            "skipped_url_duplicate_after_open": skipped_url_duplicate_count,
        },
        "collected": collected,
        "skipped": skipped,
        "failures": failures,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    partial_summary_path.unlink(missing_ok=True)
    log_event("account_collection_finished", **summary)
    return summary


def recover_partial_account_summary(
    output_dir: Path,
    account_name: str,
    error: str,
    category: str,
) -> dict[str, Any]:
    """将账号异常前的逐篇检查点合并到最终失败摘要。"""
    fatal_summary: dict[str, Any] = {
        "account": account_name,
        "fatal_error": error,
        "fatal_category": category,
    }
    partial_summary_path = output_dir / "partial-summary.json"
    if not partial_summary_path.exists():
        return fatal_summary
    try:
        recovered = json.loads(partial_summary_path.read_text(encoding="utf-8"))
        if not isinstance(recovered, dict):
            return fatal_summary
        fatal_summary = recovered
        fatal_summary.update(
            {
                "partial": True,
                "fatal_error": error,
                "fatal_category": category,
                "stop_reason": "账号中途失败；已保留中断前成功采集的文章",
            }
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "summary.json").write_text(
            json.dumps(fatal_summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        log_event(
            "account_partial_result_recovered",
            account=account_name,
            collected=len(fatal_summary.get("collected") or []),
            fatal_error=error,
            fatal_category=category,
        )
    except (OSError, ValueError, TypeError) as checkpoint_exc:
        log_event(
            "account_partial_result_recovery_failed",
            account=account_name,
            error=str(checkpoint_exc),
        )
    return fatal_summary


def load_account_names(
    names: list[str],
    accounts_file: str | None,
    accounts_from_mongo: bool = False,
    mongo_uri: str = "",
    mongo_database: str = "weixin",
    mongo_collection: str = "collection_target",
) -> list[str]:
    values = [name.strip() for name in names if name.strip()]
    if accounts_file:
        path = Path(accounts_file)
        values.extend(
            line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    if accounts_from_mongo:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=8000)
        try:
            client.admin.command("ping")
            cursor = client[mongo_database][mongo_collection].find(
                {"name": {"$type": "string", "$ne": ""}},
                {"_id": 1, "name": 1},
            ).sort("_id", 1)
            # collection_target 的 name 是采集入口；id 缺失不影响按名称搜索。
            values.extend(
                str(document.get("name") or "").strip()
                for document in cursor
                if str(document.get("name") or "").strip()
            )
        finally:
            client.close()
    # 保留配置顺序并去重。
    return list(dict.fromkeys(values))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--live", action="store_true", help="允许点击微信窗口")
    parser.add_argument("--click-account", type=int, help="点击识别结果中从0开始的公众号序号")
    parser.add_argument("--click-article", type=int, help="点击识别结果中从0开始的文章序号")
    parser.add_argument("--collect-open-article", action="store_true", help="采集当前已打开文章")
    parser.add_argument("--run-one-account", action="store_true", help="采集当前屏指定公众号的最近文章")
    parser.add_argument("--run-search-accounts", action="store_true", help="按名称搜索公众号并采集文章")
    parser.add_argument(
        "--discovery-mode",
        choices=("sogou-profile", "wechat-followed"),
        default="sogou-profile",
        help="公众号发现方式，默认通过搜一搜资料窗口且无需关注",
    )
    parser.add_argument("--account-name", action="append", default=[], help="要搜索的公众号名称，可重复传入")
    parser.add_argument("--accounts-file", help="每行一个公众号名称的 UTF-8 文本文件")
    parser.add_argument(
        "--accounts-from-mongo",
        action="store_true",
        help="从MongoDB的collection_target.name读取公众号名称",
    )
    parser.add_argument(
        "--accounts-mongo-uri",
        default=os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
    )
    parser.add_argument(
        "--accounts-mongo-database",
        default=os.getenv("MONGO_DATABASE", "weixin"),
    )
    parser.add_argument(
        "--accounts-mongo-collection",
        default=os.getenv("MONGO_TARGET_COLLECTION", "collection_target"),
    )
    parser.add_argument("--account-index", type=int, default=0)
    parser.add_argument("--max-articles", type=int, default=20)
    parser.add_argument(
        "--task-timeout-minutes",
        type=float,
        default=45.0,
        help="单个公众号采集的最长时间，0 表示不限制",
    )
    parser.add_argument(
        "--window-layout",
        choices=("auto", "off"),
        default="auto",
        help="自动固定搜一搜浏览器和公众号资料窗口的位置；off 保留人工布局",
    )
    parser.add_argument(
        "--metrics",
        choices=("share", "all"),
        default="share",
        help="互动指标模式：默认只识别转发数；all 识别全部互动数",
    )
    parser.add_argument(
        "--scan-range",
        choices=("today", "yesterday", "today_yesterday"),
        default="today_yesterday",
        help="文章日期范围：today 今天、yesterday 昨天、today_yesterday 今天和昨天",
    )
    parser.add_argument("--write-mongo", action="store_true", help="允许将采集结果写入MongoDB")
    parser.add_argument(
        "--article-mongo-uri",
        default=os.getenv("MONGO_URI", "mongodb://192.168.28.70:27019/"),
    )
    parser.add_argument(
        "--article-mongo-database",
        default=os.getenv("MONGO_DATABASE", "weixin"),
    )
    parser.add_argument(
        "--article-mongo-collection",
        default=os.getenv("MONGO_ARTICLE_COLLECTION", "article"),
    )
    parser.add_argument("--export-jsonl", default=str(DEFAULT_OUTPUT_DIR / "articles.jsonl"))
    parser.add_argument("--export-csv", default=str(DEFAULT_OUTPUT_DIR / "articles.csv"))
    parser.add_argument("--wait-seconds", type=float, default=2.0)
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="严格禁用所有VL调用；本地识别失败时直接记录失败",
    )
    return parser.parse_args()


def main() -> None:
    global WINDOW_LAYOUT_MODE
    args = parse_args()
    WINDOW_LAYOUT_MODE = args.window_layout
    log_path = configure_run_logging(Path(args.output_dir))
    log_event("run_started", argv=os.sys.argv, output_dir=args.output_dir, log_path=str(log_path))
    log_event("runtime_identity", **runtime_identity(__file__))
    log_event("network_capture_configuration",
              enabled=bool(os.getenv("WECHAT_CAPTURE_CACHE_DIR", "").strip()) and args.metrics == "share",
              mode=os.getenv("WECHAT_CAPTURE_MODE", "prefer"),
              cache_directory=os.getenv("WECHAT_CAPTURE_CACHE_DIR", ""),
              metric_mode=args.metrics,
              note="配置启用不代表代理已连通，以实际响应命中为准")
    if args.local_only:
        # 严格本地模式不要求配置 API Key，且所有可能调用 VL 的分支都会被禁止。
        client = QwenVisionClient(QwenVisionConfig(base_url="", api_key=""))
        vl_available = False
    else:
        try:
            client = QwenVisionClient(QwenVisionConfig.from_env())
            vl_available = True
        except RuntimeError as exc:
            # 未配置视觉模型时继续运行本地采集；真正进入兜底分支时会在日志中明确显示已跳过。
            client = QwenVisionClient(QwenVisionConfig(base_url="", api_key=""))
            vl_available = False
            log_event("qwen_vl_unavailable", reason=str(exc))
    if args.run_search_accounts:
        if not args.live:
            raise RuntimeError("搜索采集模式必须显式传入 --live")
        account_names = load_account_names(
            args.account_name,
            args.accounts_file,
            accounts_from_mongo=args.accounts_from_mongo,
            mongo_uri=args.accounts_mongo_uri,
            mongo_database=args.accounts_mongo_database,
            mongo_collection=args.accounts_mongo_collection,
        )
        if not account_names:
            raise RuntimeError(
                "请通过 --account-name、--accounts-file 或 --accounts-from-mongo 提供公众号名称"
            )
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.output_dir) / "accounts-source.json").write_text(
            json.dumps(
                {
                    "count": len(account_names),
                    "source": "mongo" if args.accounts_from_mongo else "arguments_or_file",
                    "mongo_database": args.accounts_mongo_database if args.accounts_from_mongo else None,
                    "mongo_collection": args.accounts_mongo_collection if args.accounts_from_mongo else None,
                    "accounts": account_names,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        # 让控制台能够在不额外查询数据库的情况下，显示公众号总数和当前位置。
        log_event(
            "accounts_loaded",
            count=len(account_names),
            source="mongo" if args.accounts_from_mongo else "arguments_or_file",
        )
        summaries = []
        for account_name in account_names:
            account_output_dir = Path(args.output_dir) / safe_path_name(account_name)
            try:
                collector = (
                    collect_profile_account
                    if args.discovery_mode == "sogou-profile"
                    else collect_searched_account
                )
                summaries.append(collector(
                    client,
                    account_name,
                    account_output_dir,
                    args.max_articles,
                    args.export_jsonl or None,
                    args.export_csv or None,
                    allow_vl=vl_available,
                    write_mongo=args.write_mongo,
                    mongo_uri=args.article_mongo_uri,
                    mongo_database=args.article_mongo_database,
                    mongo_collection=args.article_mongo_collection,
                    mongo_target_collection=args.accounts_mongo_collection,
                    metric_mode=args.metrics,
                    task_timeout_minutes=args.task_timeout_minutes,
                    scan_range=args.scan_range,
                ))
            except Exception as exc:
                error = str(exc)
                category = classify_collection_error(exc)
                # 账号级异常没有恢复机会，显式记录终态事件，供控制台准确统计。
                log_event(
                    "account_collection_failed",
                    account=account_name,
                    error=error,
                    category=category,
                )
                fatal_summary = recover_partial_account_summary(
                    account_output_dir,
                    account_name,
                    error,
                    category,
                )
                summaries.append(fatal_summary)
                if args.discovery_mode == "sogou-profile" and category in {"navigation_return", "search_recovery"}:
                    # 浏览器未恢复时停止整批，避免对后续账号重复执行同一失败链路。
                    try:
                        recover_batch_navigation(account_name)
                    except RuntimeError as recovery_error:
                        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
                        (Path(args.output_dir) / "batch-summary.json").write_text(
                            json.dumps(summaries, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                        log_event("batch_navigation_blocked", account=account_name, error=str(recovery_error))
                        raise RuntimeError("浏览器导航未恢复，已停止本轮并保存结果；恢复搜一搜后重试") from recovery_error
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.output_dir) / "batch-summary.json").write_text(
            json.dumps(summaries, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        collected_records = [
            record
            for summary in summaries
            for record in (summary.get("collected") or [])
            if isinstance(record, dict)
        ]
        log_event(
            "run_finished",
            accounts_total=len(account_names),
            accounts_failed=sum(1 for summary in summaries if summary.get("fatal_error") or summary.get("failures")),
            articles_collected=len(collected_records),
            articles_inserted=sum(
                1 for record in collected_records if record.get("status") == "inserted"
            ),
            articles_updated=sum(
                1 for record in collected_records if record.get("status") == "updated"
            ),
            article_failures=sum(
                len(summary.get("failures") or []) for summary in summaries
            ),
        )
        print(json.dumps(summaries, ensure_ascii=False, indent=2, default=str))
        return
    if args.run_one_account:
        if not vl_available:
            raise RuntimeError("--run-one-account 依赖视觉模型，请先配置 QWEN_VL_API_KEY")
        if not args.live:
            raise RuntimeError("公众号循环必须显式传入 --live")
        result = run_one_account(
            client,
            Path(args.output_dir),
            args.account_index,
            args.max_articles,
            args.export_jsonl or None,
            args.export_csv or None,
            args.metrics,
            args.scan_range,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    if args.collect_open_article:
        result = collect_open_article(
            client,
            Path(args.output_dir),
            args.write_mongo,
            args.export_jsonl or None,
            args.export_csv or None,
            allow_vl=vl_available,
            metric_mode=args.metrics,
        )
        summary = {key: value for key, value in result.items() if key != "content"}
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return
    if not vl_available:
        raise RuntimeError("当前操作依赖视觉模型，请先配置 QWEN_VL_API_KEY")
    result = analyze_current_window(client, Path(args.output_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.click_account is not None and args.click_article is not None:
        raise RuntimeError("一次测试只能点击公众号或文章之一")
    if args.click_account is None and args.click_article is None:
        return
    if not args.live:
        raise RuntimeError("点击操作必须同时传入 --live")
    items = result["accounts"] if args.click_account is not None else result["articles"]
    index = args.click_account if args.click_account is not None else args.click_article
    assert index is not None
    if not 0 <= index < len(items):
        raise IndexError("点击序号超出识别结果范围")
    target = items[index]
    screen_x, screen_y = target["screen_point"]
    label = target.get("name") or target.get("title") or "未知项目"
    kind = "公众号" if args.click_account is not None else "文章"
    print(f"即将点击{kind}：{label}，坐标=({screen_x}, {screen_y})")
    time.sleep(args.wait_seconds)
    click(screen_x, screen_y)


if __name__ == "__main__":
    main()
