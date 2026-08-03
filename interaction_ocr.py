"""使用图标模板匹配和本地 OCR 识别微信文章互动数。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
METRIC_NAMES = ("like", "share", "favorite", "comment")


def _to_gray(image: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)


def _match_icon(search: np.ndarray, template_path: Path) -> tuple[int, int, int, int, float]:
    template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
    if template is None:
        raise FileNotFoundError(f"缺少互动图标模板：{template_path}")
    best: tuple[int, int, int, int, float] | None = None
    # 微信内置浏览器缩放或窗口宽度变化时图标会同步缩放，逐级匹配避免固定模板失效。
    for scale in np.arange(0.60, 1.41, 0.05):
        scaled_width = max(8, round(template.shape[1] * float(scale)))
        scaled_height = max(8, round(template.shape[0] * float(scale)))
        if scaled_width >= search.shape[1] or scaled_height >= search.shape[0]:
            continue
        scaled = cv2.resize(
            template, (scaled_width, scaled_height), interpolation=cv2.INTER_AREA
        )
        result = cv2.matchTemplate(search, scaled, cv2.TM_CCOEFF_NORMED)
        _, confidence, _, location = cv2.minMaxLoc(result)
        candidate = (
            int(location[0]), int(location[1]), scaled_width, scaled_height, float(confidence)
        )
        if best is None or candidate[4] > best[4]:
            best = candidate
    if best is None:
        raise ValueError(f"互动图标模板尺寸超过搜索区域：{template_path}")
    return best


def _parse_count(text: str) -> int | None:
    normalized = text.strip().lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万w]?)", normalized)
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2):
        value *= 10_000
    return round(value)


class InteractionOCR:
    """先定位四个互动图标，再识别每个图标右侧的小块数字。"""

    def __init__(self, template_dir: Path = TEMPLATE_DIR) -> None:
        self.template_dir = template_dir
        self.ocr = RapidOCR()

    def extract_share(self, screenshot: Image.Image) -> dict[str, Any]:
        """只定位转发图标并识别其右侧数字，减少模板匹配和 OCR 范围。"""
        gray = _to_gray(screenshot)
        height, width = gray.shape
        search_top = round(height * 0.82)
        x_offset = round(width * 0.55)
        search = gray[search_top:, x_offset:]
        x, y, icon_width, icon_height, confidence = _match_icon(
            search, self.template_dir / "share.png"
        )
        x += x_offset
        y += search_top
        if confidence < 0.72:
            raise ValueError(f"转发图标模板匹配置信度不足：{confidence:.4f}")

        # 只截取图标右侧约一个指标的宽度，避免把收藏数一并识别进来。
        number_left = max(0, x + icon_width - 3)
        number_right = min(width, x + icon_width + max(52, round(width * 0.065)))
        number_top = max(0, y - 8)
        number_bottom = min(height, y + icon_height + 8)
        number_crop = screenshot.crop((number_left, number_top, number_right, number_bottom))
        result, _ = self.ocr(number_crop)
        texts: list[str] = []
        for item in result or []:
            box, text = item[0], str(item[1])
            center_x = sum(point[0] for point in box) / len(box)
            # 裁剪左缘仍包含少量图标像素，忽略贴近左边缘的误识别。
            if center_x > 5:
                texts.append(text)
        count = _parse_count(" ".join(texts))
        if count is None and confidence >= 0.90:
            count = 0
        return {
            "share_count": count,
            "details": {
                "share": {
                    "template_confidence": round(confidence, 4),
                    "ocr_text": texts,
                },
                "number_box": (number_left, number_top, number_right, number_bottom),
            },
        }

    def extract(self, screenshot: Image.Image) -> dict[str, Any]:
        gray = _to_gray(screenshot)
        height, width = gray.shape
        # 互动栏稳定出现在文章窗口底部；限制搜索范围可避免评论区图标误匹配。
        search_top = round(height * 0.82)
        x_offset = round(width * 0.55)
        search = gray[search_top:, x_offset:]

        matches: dict[str, tuple[int, int, int, int, float]] = {}
        for name in METRIC_NAMES:
            x, y, icon_width, icon_height, confidence = _match_icon(
                search, self.template_dir / f"{name}.png"
            )
            matches[name] = (x + x_offset, y + search_top, icon_width, icon_height, confidence)

        ordered = [matches[name] for name in METRIC_NAMES]
        if any(item[4] < 0.72 for item in ordered):
            raise ValueError(f"互动图标模板匹配置信度不足：{matches}")
        if [item[0] for item in ordered] != sorted(item[0] for item in ordered):
            raise ValueError(f"互动图标顺序异常：{matches}")

        bar_left = ordered[0][0]
        bar_top = max(0, min(item[1] for item in ordered) - 8)
        bar_right = min(width, ordered[-1][0] + ordered[-1][2] + 80)
        bar_bottom = min(height, max(item[1] + item[3] for item in ordered) + 8)
        bar = screenshot.crop((bar_left, bar_top, bar_right, bar_bottom))
        result, _ = self.ocr(bar)

        # 根据 OCR 文本中心点的横坐标，归属到其左侧最近的互动图标。
        assigned: dict[str, list[str]] = {name: [] for name in METRIC_NAMES}
        for item in result or []:
            box, text = item[0], str(item[1])
            center_x = bar_left + sum(point[0] for point in box) / len(box)
            # OCR 偶尔会把线框图标误认成数字，落在任一图标范围内的文本直接忽略。
            if any(data[0] - 3 <= center_x <= data[0] + data[2] + 3 for data in ordered):
                continue
            candidates = [
                (name, data) for name, data in matches.items()
                if center_x >= data[0] + data[2] - 4
            ]
            if candidates:
                name = max(candidates, key=lambda candidate: candidate[1][0])[0]
                assigned[name].append(text)

        values: dict[str, Any] = {}
        details: dict[str, Any] = {}
        for name in METRIC_NAMES:
            confidence = matches[name][4]
            count = _parse_count(" ".join(assigned[name]))
            # 微信互动数为零时只显示图标或“赞”；模板可靠但无数字时按零处理。
            zero_threshold = 0.80 if name == "comment" else 0.90
            if count is None and confidence >= zero_threshold:
                count = 0
            values[f"{name}_count"] = count
            details[name] = {
                "template_confidence": round(confidence, 4),
                "ocr_text": assigned[name],
            }
        details["bar_box"] = (bar_left, bar_top, bar_right, bar_bottom)

        values["details"] = details
        return values
