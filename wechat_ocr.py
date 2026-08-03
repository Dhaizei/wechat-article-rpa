"""微信固定界面的本地 OCR 定位工具。"""

from __future__ import annotations

import difflib
import unicodedata
from typing import Any

from PIL import Image
from rapidocr_onnxruntime import RapidOCR


def _normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value or "").split())


def _center(box: list[list[float]]) -> tuple[float, float]:
    return (
        sum(point[0] for point in box) / len(box),
        sum(point[1] for point in box) / len(box),
    )


class WeChatOCR:
    def __init__(self) -> None:
        self.ocr = RapidOCR()

    def locate_search_box(self, screenshot: Image.Image, current_text: str = "") -> dict[str, Any]:
        """定位微信左上角搜索框中的占位文字或现有查询文字。"""
        result, _ = self.ocr(screenshot)
        expected = _normalize(current_text)
        candidates = []
        for box, text, confidence in result or []:
            center_x, center_y = _center(box)
            normalized = _normalize(str(text)).lstrip("Qα口")
            if center_x > screenshot.width * 0.35 or center_y > screenshot.height * 0.22:
                continue
            if "搜索" in normalized or (expected and expected in normalized):
                candidates.append((center_y, center_x, str(text), float(confidence)))
        if not candidates:
            return {"found": False, "reason": "OCR未找到微信搜索框"}
        center_y, center_x, text, confidence = min(candidates)
        return {
            "found": True,
            "text": text,
            "center_x_1000": round(center_x * 1000 / screenshot.width),
            "center_y_1000": round(center_y * 1000 / screenshot.height),
            "confidence": confidence,
        }

    def locate_official_account_result(
        self, screenshot: Image.Image, expected_name: str
    ) -> dict[str, Any]:
        """定位“公众号”分组下名称匹配的第一条搜索结果。"""
        result, _ = self.ocr(screenshot)
        rows = [
            {"box": item[0], "text": str(item[1]), "confidence": float(item[2])}
            for item in (result or [])
        ]
        section_rows = [
            row for row in rows
            if _normalize(row["text"]) == "公众号"
            and _center(row["box"])[0] < screenshot.width * 0.25
        ]
        if not section_rows:
            return {"found": False, "reason": "OCR未找到公众号分组", "ocr_rows": rows}

        section = min(section_rows, key=lambda row: _center(row["box"])[1])
        section_y = _center(section["box"])[1]
        expected = _normalize(expected_name)
        candidates = []
        for row in rows:
            center_x, center_y = _center(row["box"])
            # 搜索结果名称位于“公众号”标题下方紧邻区域，排除输入框和聊天记录。
            if center_x >= screenshot.width * 0.35:
                continue
            if not section_y + 15 <= center_y <= section_y + screenshot.height * 0.13:
                continue
            actual = _normalize(row["text"])
            similarity = difflib.SequenceMatcher(None, expected, actual).ratio()
            if actual == expected or similarity >= 0.92:
                candidates.append((similarity, row, center_x, center_y))

        if not candidates:
            return {
                "found": False,
                "reason": "公众号结果匹配数量异常：0",
                "ocr_rows": rows,
            }
        # 某些账号名称会被 OCR 同时识别为主标题和头像下方的小字。
        # 优先采用置信度最高、文字更接近目标名称的结果，避免把正常结果当成重复项。
        similarity, row, center_x, center_y = max(
            candidates,
            key=lambda item: (item[1]["confidence"], item[0], item[2]),
        )
        return {
            "found": True,
            "name": row["text"],
            "center_x_1000": round(center_x * 1000 / screenshot.width),
            "center_y_1000": round(center_y * 1000 / screenshot.height),
            "confidence": row["confidence"],
            "similarity": similarity,
            "method": "rapidocr-layout",
        }
