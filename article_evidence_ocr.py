"""从同一张微信文章窗口截图识别活动标签标题和正文大标题。"""

from __future__ import annotations

import difflib
import unicodedata
from typing import Any

from PIL import Image
from rapidocr_onnxruntime import RapidOCR


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip()
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return "".join(text.split())


def _bounds(box: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return min(xs), min(ys), max(xs), max(ys)


def _score(candidate: str, expected: str) -> float:
    candidate_value = _normalize(candidate).rstrip(".……")
    expected_value = _normalize(expected)
    if not candidate_value or not expected_value:
        return 0.0
    if expected_value.startswith(candidate_value):
        # 标签标题经常被宽度截断；前缀越长，证据越强。
        return 0.80 + min(len(candidate_value) / len(expected_value), 1.0) * 0.20
    return difflib.SequenceMatcher(None, candidate_value, expected_value).ratio()


class ArticleEvidenceOCR:
    def __init__(self) -> None:
        self.ocr = RapidOCR()

    def inspect(self, screenshot: Image.Image, expected_title: str) -> dict[str, Any]:
        """识别顶部活动标签和正文标题，返回与网页真实标题最接近的候选。"""
        width, height = screenshot.size
        top_height = min(height, max(220, round(height * 0.24)))
        result, _ = self.ocr(screenshot.crop((0, 0, width, top_height)))
        rows: list[dict[str, Any]] = []
        for box, text, confidence in result or []:
            left, top, right, bottom = _bounds(box)
            rows.append(
                {
                    "text": str(text).strip(),
                    "confidence": float(confidence),
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "center_y": (top + bottom) / 2,
                }
            )

        tab_candidates = [
            {**row, "text": row["text"].rstrip("xX×")}
            for row in rows
            if row["center_y"] <= height * 0.055 and len(_normalize(row["text"])) >= 4
        ]
        title_rows = [
            row for row in rows
            if height * 0.055 < row["center_y"] <= height * 0.19
            and row["left"] < width * 0.92
            and len(_normalize(row["text"])) >= 4
        ]

        # 长标题可能换成两行，把相邻的标题行组合后一起参与匹配。
        title_candidates = list(title_rows)
        ordered = sorted(title_rows, key=lambda row: (row["top"], row["left"]))
        for index, first in enumerate(ordered):
            combined = first["text"]
            confidence = first["confidence"]
            previous = first
            for following in ordered[index + 1:index + 3]:
                gap = following["top"] - previous["bottom"]
                if gap < -height * 0.015 or gap > height * 0.045:
                    break
                combined += following["text"]
                confidence = min(confidence, following["confidence"])
                title_candidates.append(
                    {"text": combined, "confidence": confidence}
                )
                previous = following

        def choose(candidates: list[dict[str, Any]]) -> dict[str, Any]:
            if not candidates:
                return {"text": "", "confidence": 0.0, "score": 0.0}
            best = max(candidates, key=lambda item: _score(item["text"], expected_title))
            return {
                "text": best["text"],
                "confidence": round(float(best["confidence"]), 4),
                "score": round(_score(best["text"], expected_title), 4),
            }

        return {
            "expected_title": expected_title,
            "tab_title": choose(tab_candidates),
            "viewport_title": choose(title_candidates),
            "method": "rapidocr-same-frame-evidence",
        }
