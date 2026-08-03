"""使用本地 OCR 识别微信公众号消息列表中的时间标签和文章卡片。"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR


TIME_PATTERNS = (
    re.compile(r"^(?:今天|昨天)?\s*\d{1,2}:\d{2}$"),
    re.compile(r"^(?:星期|周)[一二三四五六日天1-7](?:\s*\d{1,2}:\d{2})?$"),
    re.compile(r"^(?:\d{4}年)?\d{1,2}月\d{1,2}日(?:\s*\d{1,2}:\d{2})?$"),
    re.compile(r"^\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?(?:\s*\d{1,2}:\d{2})?$"),
)


def _normalize(value: str) -> str:
    """统一全半角和空白，保留标题中的标点。"""
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def _box_bounds(box: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return min(xs), min(ys), max(xs), max(ys)


def _is_time_label(text: str) -> bool:
    compact = _normalize(text)
    return any(pattern.fullmatch(compact) for pattern in TIME_PATTERNS)


class WeChatFeedOCR:
    """识别固定版式的公众号消息窗口，失败时由调用方交给 VL 兜底。"""

    def __init__(self) -> None:
        self.ocr = RapidOCR()

    @staticmethod
    def _is_card_background(image: np.ndarray, bounds: tuple[float, float, float, float]) -> bool:
        """判断文字是否位于文章卡片内，排除白色标题栏和底部导航。"""
        left, top, right, bottom = bounds
        height, width = image.shape[:2]
        x = max(0, min(width - 1, round((left + right) / 2)))
        # 在文字上下取样，避开黑色字形本身；图片标题和灰色卡片都不是纯白背景。
        offsets = (-12, 12)
        samples = []
        center_y = (top + bottom) / 2
        for offset in offsets:
            y = max(0, min(height - 1, round(center_y + offset)))
            samples.append(image[y, x])
        return any(float(np.mean(sample)) < 247.0 for sample in samples)

    @staticmethod
    def _large_image_runs(image: np.ndarray) -> list[tuple[int, int]]:
        """检测消息卡片中的横向大图，随后仅保留大图底部的标题文字。"""
        height, width = image.shape[:2]
        roi = image[:, round(width * 0.20):round(width * 0.80)]
        channel_spread = roi.max(axis=2) - roi.min(axis=2)
        brightness = roi.mean(axis=2)
        content_ratio = ((channel_spread > 18) | (brightness < 215)).mean(axis=1)
        mask = content_ratio > 0.38
        runs: list[tuple[int, int]] = []
        start: int | None = None
        for y, active in enumerate(mask):
            if active and start is None:
                start = y
            elif not active and start is not None:
                if y - start >= max(45, round(height * 0.055)):
                    runs.append((start, y - 1))
                start = None
        if start is not None and height - start >= max(45, round(height * 0.055)):
            runs.append((start, height - 1))
        return runs

    @staticmethod
    def _has_right_thumbnail(
        image: np.ndarray, group: list[dict[str, Any]], width: int, height: int
    ) -> bool:
        """次级文章右侧通常带缩略图，可据此避免把它合并到主图文章。"""
        top = max(0, round(min(row["top"] for row in group) - height * 0.025))
        bottom = min(height, round(max(row["bottom"] for row in group) + height * 0.025))
        left = round(width * 0.66)
        right = round(width * 0.80)
        roi = image[top:bottom, left:right]
        if roi.size == 0:
            return False
        channel_spread = roi.max(axis=2) - roi.min(axis=2)
        brightness = roi.mean(axis=2)
        return float(((channel_spread > 18) | (brightness < 215)).mean()) > 0.22

    def inspect_account_feed(self, screenshot: Image.Image) -> dict[str, Any]:
        started = time.perf_counter()
        result, _ = self.ocr(screenshot)
        width, height = screenshot.size
        image = np.asarray(screenshot.convert("RGB"))
        image_runs = self._large_image_runs(image)
        rows: list[dict[str, Any]] = []
        for box, raw_text, confidence in result or []:
            left, top, right, bottom = _box_bounds(box)
            text = _normalize(str(raw_text))
            if not text:
                continue
            rows.append(
                {
                    "text": text,
                    "confidence": float(confidence),
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "center_x": (left + right) / 2,
                    "center_y": (top + bottom) / 2,
                    # 最暗一成像素可区分黑/白标题与浅灰色摘要文字。
                    "ink_darkness": float(
                        np.sort(
                            image[
                                max(0, round(top)):min(height, round(bottom) + 1),
                                max(0, round(left)):min(width, round(right) + 1),
                            ].mean(axis=2).ravel()
                        )[: max(1, round(max(1.0, (right - left) * (bottom - top)) * 0.10))].mean()
                    ),
                }
            )

        time_labels = []
        time_row_ids: set[int] = set()
        for index, row in enumerate(rows):
            if not _is_time_label(row["text"]):
                continue
            # 时间分隔标签位于消息区域中部，排除系统标题栏中的偶然数字。
            if not 0.18 * width <= row["center_x"] <= 0.82 * width:
                continue
            if not 0.09 * height <= row["center_y"] <= 0.90 * height:
                continue
            time_row_ids.add(index)
            time_labels.append(
                {
                    "text": row["text"],
                    "center_y_1000": round(row["center_y"] * 1000 / height),
                    "confidence": row["confidence"],
                }
            )

        outside_rows = []
        rows_by_image: list[list[dict[str, Any]]] = [[] for _ in image_runs]
        for index, row in enumerate(rows):
            if index in time_row_ids:
                continue
            # 消息卡片集中在窗口中央；顶部公众号名称和底部导航均在该范围之外。
            if row["left"] < width * 0.17 or row["left"] > width * 0.62:
                continue
            if row["center_y"] < height * 0.14 or row["center_y"] > height * 0.88:
                continue
            if len(row["text"].replace(" ", "")) < 4:
                continue
            image_index = next(
                (
                    run_index
                    for run_index, (run_top, run_bottom) in enumerate(image_runs)
                    if run_top <= row["center_y"] <= run_bottom
                ),
                None,
            )
            if image_index is not None:
                rows_by_image[image_index].append(row)
            else:
                bounds = (row["left"], row["top"], row["right"], row["bottom"])
                if self._is_card_background(image, bounds):
                    outside_rows.append(row)

        # RapidOCR 通常会把双行标题拆成两行；相邻行距较小时将其合并为一篇文章。
        outside_rows.sort(key=lambda row: (row["center_y"], row["left"]))
        groups: list[list[dict[str, Any]]] = []
        max_line_gap = max(18.0, height * 0.045)
        for row in outside_rows:
            if not groups:
                groups.append([row])
                continue
            previous = groups[-1][-1]
            vertical_gap = row["top"] - previous["bottom"]
            left_delta = abs(row["left"] - previous["left"])
            same_ink_class = (row["ink_darkness"] < 70) == (previous["ink_darkness"] < 70)
            if (
                vertical_gap <= max_line_gap
                and left_delta <= width * 0.08
                and same_ink_class
            ):
                groups[-1].append(row)
            else:
                groups.append([row])

        # 对每张大图只产生一个点击目标。若大图下方紧跟无缩略图标题，则标题属于主文章；
        # 否则使用大图底部的白色覆盖标题。
        image_groups: list[list[dict[str, Any]]] = []
        consumed_group_ids: set[int] = set()
        for run_index, (run_top, run_bottom) in enumerate(image_runs):
            following = next(
                (
                    (group_index, group)
                    for group_index, group in enumerate(groups)
                    if group_index not in consumed_group_ids
                    and 0 <= min(row["top"] for row in group) - run_bottom <= height * 0.13
                ),
                None,
            )
            if following and not self._has_right_thumbnail(image, following[1], width, height):
                consumed_group_ids.add(following[0])
                image_groups.append(following[1])
                continue
            overlay_rows = [
                row
                for row in rows_by_image[run_index]
                if row["center_y"] >= run_top + (run_bottom - run_top) * 0.62
            ]
            if overlay_rows:
                image_groups.append(overlay_rows)

        final_groups = image_groups + [
            group for group_index, group in enumerate(groups)
            if group_index not in consumed_group_ids
        ]
        final_groups.sort(key=lambda group: min(row["center_y"] for row in group))

        articles = []
        for group in final_groups:
            # 微信常在主标题下显示浅灰色摘要；它不可单独点击，也不是文章标题。
            if min(row["ink_darkness"] for row in group) >= 70:
                continue
            title = "".join(row["text"] for row in group).strip()
            if len(title.replace(" ", "")) < 4:
                continue
            left = min(row["left"] for row in group)
            right = max(row["right"] for row in group)
            top = min(row["top"] for row in group)
            bottom = max(row["bottom"] for row in group)
            articles.append(
                {
                    "title": title,
                    "center_x_1000": round(((left + right) / 2) * 1000 / width),
                    "center_y_1000": round(((top + bottom) / 2) * 1000 / height),
                    "confidence": round(min(row["confidence"] for row in group), 4),
                }
            )

        return {
            "time_labels": sorted(time_labels, key=lambda item: item["center_y_1000"]),
            "articles": articles,
            "recognition_method": "rapidocr-layout",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
