"""搜一搜结果和微信公众号资料窗口的本地 OCR 定位。"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

import cv2
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR


def _normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value or "").split())


def _strip_ocr_edge_punctuation(value: str) -> str:
    """移除 OCR 在账号名称首尾偶发识别出的标点，不改变名称正文。"""
    normalized = _normalize(value)
    # 仅处理首尾标点，例如截图中的“Kimi智能助手?”。中间字符不会被删除，
    # 因此“账号A”和“账号-A”等相似名称仍会按不同账号处理。
    while normalized and unicodedata.category(normalized[0]).startswith("P"):
        normalized = normalized[1:]
    while normalized and unicodedata.category(normalized[-1]).startswith("P"):
        normalized = normalized[:-1]
    return normalized


def _account_name_match(expected_name: str, observed_name: str) -> tuple[bool, str]:
    """判断搜一搜名称是否可安全地映射为目标公众号。

    默认只接受完全一致；少数公众号会在搜索结果中附加“媒体”“事业单位”等
    账号身份后缀。这类受限后缀可接受，其他相似名一律拒绝，避免误点同名账号。
    """
    expected = _normalize(expected_name)
    observed = _normalize(observed_name)
    if observed == expected:
        return True, "exact"

    # 英文品牌名在不同页面的大小写并不稳定，例如 ComfyUI / ComfyUi。
    # 仅忽略 ASCII 大小写，中文和其他字符仍必须逐字一致。
    if observed.casefold() == expected.casefold():
        return True, "ascii-case-ignored"

    # RapidOCR 偶尔会把大写 I 识别成小写 l（AI -> Al）。只接受长度相同、
    # 且除此之外逐字一致的 I/l 单字符差异，避免使用宽泛的模糊匹配误点账号。
    if len(expected) >= 6 and len(observed) == len(expected):
        differences = [
            (left, right)
            for left, right in zip(expected.casefold(), observed.casefold())
            if left != right
        ]
        if differences and all({left, right} == {"i", "l"} for left, right in differences):
            return True, "ocr-i-l-confusion"

    # 账号名前缀 AI 在小字号窗口标题中偶尔会漏掉 I（AI新工匠 -> A新工匠）。
    # 只允许这个固定前缀的单字符缺失，且余下正文完全一致，不采用泛化的编辑距离。
    if (
        len(expected) >= 5
        and expected.startswith("AI")
        and observed == "A" + expected[2:]
    ):
        return True, "ocr-ai-prefix-i-omitted"

    # 少数中文字符形近会被 OCR 稳定混淆。这里只放行本项目截图验证过的
    # “哔/哗”和“千/干”，并且仍要求其余字符、账号筛选和后续资料窗口全部一致。
    if len(expected) >= 4 and len(observed) == len(expected):
        differences = [
            (left, right)
            for left, right in zip(expected, observed)
            if left != right
        ]
        accepted_pairs = ({"哔", "哗"}, {"千", "干"})
        if differences and all({left, right} in accepted_pairs for left, right in differences):
            return True, "ocr-chinese-confusion"
    # 账号结果行偶尔会在末尾多出 ?、！等 OCR 噪声；只忽略首尾标点后再比较。
    # 这一步不会接受正文不同的近似名称，仍由后续“公众号/原创内容”证据二次约束。
    expected_without_edge_punctuation = _strip_ocr_edge_punctuation(expected)
    observed_without_edge_punctuation = _strip_ocr_edge_punctuation(observed)
    if (
        expected_without_edge_punctuation
        and observed_without_edge_punctuation == expected_without_edge_punctuation
    ):
        return True, "edge-punctuation-ignored"
    suffix = observed[len(expected) :] if observed.startswith(expected) else ""
    # 微信搜一搜会把认证账号的主体类型拼在名称 OCR 行末，例如“书生Intern 事业单位”。
    # 仅放行固定身份标签，且仍须在后续卡片中找到“原创内容”等证据，避免扩大误匹配范围。
    safe_suffixes = {
        "媒体",
        "官方",
        "公众号",
        "社区",
        "频道",
        "服务号",
        "事业单位",
        "企业",
        "学校",
        "机构",
        "政府",
    }
    if len(expected) >= 4 and suffix in safe_suffixes:
        return True, "safe-suffix-alias"
    return False, ""


def _bounds(box: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return min(xs), min(ys), max(xs), max(ys)


def _number(value: str) -> int | None:
    text = unicodedata.normalize("NFKC", value or "").strip().rstrip("+")
    match = re.fullmatch(r"([\d.]+)(万)?", text)
    if not match:
        return None
    multiplier = 10000 if match.group(2) else 1
    return round(float(match.group(1)) * multiplier)


TIME_LABEL_RE = re.compile(
    r"^(?:置顶|今天|昨天|(?:星期|周)[一二三四五六日天1-7]|"
    r"(?:\d{4}年)?\d{1,2}月\d{1,2}日)$"
)
NON_ARTICLE_SECTION_LABELS = {"贴图", "视频号"}
METRIC_FRIEND_SUFFIX = r"(?:\s*[\d.]+万?\+?个朋友(?:看过|转发))?"
METRICS_RE = re.compile(
    rf"^阅读\s*([\d.]+万?\+?)\s*赞\s*([\d.]+万?\+?){METRIC_FRIEND_SUFFIX}$"
)
COMBINED_METRICS_RE = re.compile(
    rf"^(.+?)阅读\s*([\d.]+万?\+?)\s*赞\s*([\d.]+万?\+?){METRIC_FRIEND_SUFFIX}$"
)


class WeChatProfileOCR:
    def __init__(self) -> None:
        self.ocr = RapidOCR()
        self._cached_screenshot: Image.Image | None = None
        self._cached_rows: list[dict[str, Any]] = []

    def _rows(self, screenshot: Image.Image) -> list[dict[str, Any]]:
        # 一个决策阶段会对同一张截图做多项校验，复用 OCR 结果可明显减少等待时间。
        if self._cached_screenshot is screenshot:
            return self._cached_rows
        result, _ = self.ocr(screenshot)
        rows = []
        for box, text, confidence in result or []:
            left, top, right, bottom = _bounds(box)
            rows.append(
                {
                    "text": str(text).strip(),
                    "normalized": _normalize(str(text)),
                    "confidence": float(confidence),
                    "left": left,
                    "top": top,
                    "right": right,
                    "bottom": bottom,
                    "center_x": (left + right) / 2,
                    "center_y": (top + bottom) / 2,
                }
            )
        self._cached_screenshot = screenshot
        self._cached_rows = rows
        return rows

    def locate_copy_link_action(self, screenshot: Image.Image) -> dict[str, Any]:
        """在文章浏览器菜单中定位“复制链接”，避免依赖不同电脑上的固定坐标。"""
        width, height = screenshot.size
        candidates = [
            row
            for row in self._rows(screenshot)
            if "复制" in row["normalized"]
            and "链接" in row["normalized"]
            # 浏览器菜单位于窗口上半部分；排除正文中偶然出现的同名文字。
            and row["center_y"] < height * 0.45
            and row["center_x"] > width * 0.30
        ]
        if not candidates:
            return {"found": False, "reason": "浏览器菜单中未识别到复制链接"}
        row = min(candidates, key=lambda item: (item["center_y"], -item["confidence"]))
        return {
            "found": True,
            "text": row["text"],
            "center_x_1000": round(row["center_x"] * 1000 / width),
            "center_y_1000": round(row["center_y"] * 1000 / height),
            "confidence": row["confidence"],
            "method": "rapidocr-browser-copy-link",
        }

    def locate_browser_menu_button(self, screenshot: Image.Image) -> dict[str, Any]:
        """通过标题栏中的三个横向圆点定位浏览器“更多”菜单按钮。

        这里只分析截图顶部右半区，并要求三个小连通域水平对齐、间距接近，
        避免把最小化、最大化或正文中的省略号当成菜单按钮。
        """
        rgb = np.asarray(screenshot.convert("RGB"))
        height, width = rgb.shape[:2]
        if width < 120 or height < 80:
            return {"found": False, "reason": "文章窗口截图过小"}

        # 只检查浏览器顶部工具栏。文章正文和网页工具栏也可能出现“...”，
        # 如果把识别区域放到页面内容区，会误点网页自身的分享/更多按钮。
        title_bottom = max(44, min(round(height * 0.06), 72))
        search_left = round(width * 0.45)
        search_right = round(width * 0.96)
        gray = cv2.cvtColor(rgb[:title_bottom, search_left:search_right], cv2.COLOR_RGB2GRAY)
        # 标题栏通常是浅色背景；较严格的深色阈值可排除大部分抗锯齿文字边缘。
        mask = cv2.threshold(gray, 105, 255, cv2.THRESH_BINARY_INV)[1]
        component_count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        dots: list[dict[str, float]] = []
        max_dot_size = max(9, round(title_bottom * 0.18))
        for index in range(1, component_count):
            left, top, box_width, box_height, area = stats[index]
            if not (1 <= area <= max_dot_size * max_dot_size):
                continue
            if not (1 <= box_width <= max_dot_size and 1 <= box_height <= max_dot_size):
                continue
            aspect = box_width / max(box_height, 1)
            if not 0.45 <= aspect <= 2.2:
                continue
            center_x, center_y = centroids[index]
            dots.append(
                {
                    "x": float(center_x),
                    "y": float(center_y),
                    "width": float(box_width),
                    "height": float(box_height),
                    "area": float(area),
                }
            )

        best: tuple[float, dict[str, float], dict[str, float], dict[str, float]] | None = None
        dots.sort(key=lambda item: item["x"])
        for first_index in range(len(dots)):
            for second_index in range(first_index + 1, len(dots)):
                for third_index in range(second_index + 1, len(dots)):
                    first, second, third = dots[first_index], dots[second_index], dots[third_index]
                    gap_one = second["x"] - first["x"]
                    gap_two = third["x"] - second["x"]
                    average_size = max(
                        1.0,
                        (first["width"] + second["width"] + third["width"]) / 3,
                    )
                    if not (average_size * 0.8 <= gap_one <= average_size * 5.5):
                        continue
                    if not (average_size * 0.8 <= gap_two <= average_size * 5.5):
                        continue
                    gap_similarity = min(gap_one, gap_two) / max(gap_one, gap_two)
                    y_spread = max(first["y"], second["y"], third["y"]) - min(
                        first["y"], second["y"], third["y"]
                    )
                    if gap_similarity < 0.65 or y_spread > max(2.5, average_size * 0.9):
                        continue
                    size_similarity = min(first["area"], second["area"], third["area"]) / max(
                        first["area"], second["area"], third["area"]
                    )
                    if size_similarity < 0.45:
                        continue
                    # 优先选择标题栏更靠右、也更靠上的候选；浏览器菜单通常位于
                    # 标签栏/工具栏右侧，而不是网页正文区域。
                    rightness = third["x"] / max(search_right - search_left, 1)
                    topness = 1.0 - min(1.0, max(first["y"], second["y"], third["y"]) / title_bottom)
                    score = (
                        gap_similarity * 0.40
                        + size_similarity * 0.20
                        + rightness * 0.25
                        + topness * 0.15
                    )
                    if best is None or score > best[0]:
                        best = (score, first, second, third)

        if best is None:
            return {"found": False, "reason": "标题栏未识别到三点菜单按钮"}
        score, first, second, third = best
        center_x = search_left + (first["x"] + second["x"] + third["x"]) / 3
        center_y = (first["y"] + second["y"] + third["y"]) / 3
        return {
            "found": True,
            "center_x_1000": round(center_x * 1000 / width),
            "center_y_1000": round(center_y * 1000 / height),
            "confidence": round(min(0.99, score), 3),
            "method": "opencv-browser-ellipsis",
        }

    def locate_search_box(self, screenshot: Image.Image) -> dict[str, Any]:
        """通过顶部绿色“搜索”按钮定位其左侧输入框。"""
        width, height = screenshot.size
        buttons = [
            row
            for row in self._rows(screenshot)
            if row["normalized"] == "搜索"
            # 搜索结果页在顶部，搜一搜首页则位于页面中央，两种布局都要兼容。
            and row["center_y"] < height * 0.55
            and row["center_x"] > width * 0.55
        ]
        if not buttons:
            return {"found": False, "reason": "未找到搜一搜顶部搜索框"}
        button = min(buttons, key=lambda row: row["center_y"])
        return {
            "found": True,
            "center_x_1000": 430,
            "center_y_1000": round(button["center_y"] * 1000 / height),
            "button_x_1000": round(button["center_x"] * 1000 / width),
            "button_y_1000": round(button["center_y"] * 1000 / height),
            "confidence": button["confidence"],
            "method": "rapidocr-sogou-search-box",
        }

    def locate_search_result(self, screenshot: Image.Image, expected_name: str) -> dict[str, Any]:
        """定位搜一搜“账号/公众号”结果中的精确名称，排除顶部输入框和相似账号。"""
        width, height = screenshot.size
        expected = _normalize(expected_name)
        rows = self._rows(screenshot)
        navigation_rows = [
            row
            for row in rows
            if "账号" in row["normalized"]
            and height * 0.08 <= row["center_y"] <= height * 0.30
            and row["center_x"] < width * 0.55
        ]
        navigation_bottom = min(
            (row["bottom"] for row in navigation_rows),
            default=height * 0.14,
        )
        filter_labels = {"不限", "小程序", "公众号", "服务号", "视频号"}
        filter_rows = [
            row
            for row in rows
            if row["normalized"] in filter_labels
            # 只认一级导航紧下方的二级筛选栏，排除结果卡片里的“公众号”类型文字。
            and navigation_bottom < row["center_y"] < navigation_bottom + height * 0.13
        ]
        # 以二级账号筛选栏的真实底边为锚点，不再依赖窗口高度百分比。
        # 如果某次 OCR 没读到筛选栏，再退回顶部导航底边作为结果区起点。
        if filter_rows:
            result_top = max(row["bottom"] for row in filter_rows)
        else:
            fallback_navigation_rows = [
                row
                for row in rows
                if row["normalized"] in {"全部", "账号", "文章", "视频"}
                and row["center_y"] < height * 0.35
            ]
            result_top = max(
                (row["bottom"] for row in fallback_navigation_rows),
                default=height * 0.15,
            )
        text_height = max(
            (row["bottom"] - row["top"] for row in filter_rows),
            default=max(12, height * 0.015),
        )
        result_top += max(4, text_height * 0.35)
        exact_matches = [
            row
            for row in rows
            if row["normalized"] == expected
            and row["center_y"] > result_top
            and row["left"] < width * 0.55
        ]
        # 例如库内名称“腾讯技术工程”在微信中实际展示为“腾讯技术工程媒体”。
        # 仅接受白名单后缀，不能把任意包含关系当作同一公众号。
        alias_matches = [
            row
            for row in rows
            if row["center_y"] > result_top
            and row["left"] < width * 0.55
            and _account_name_match(expected_name, row["text"])[0]
        ]
        matches = exact_matches or alias_matches
        if not matches:
            return {"found": False, "reason": "搜一搜结果中没有精确匹配名称"}
        row = min(matches, key=lambda item: item["center_y"])
        _, name_match_method = _account_name_match(expected_name, row["text"])
        # 已由上一层“公众号”二级筛选限制结果范围。卡片本身有时显示“个人”
        # （主体类型）而非“公众号”，因此仍需结合原创内容等卡片证据判断，不能
        # 仅因“个人”二字拒绝一个已经处于公众号筛选结果内的账号。
        nearby_rows = [
            item
            for item in rows
            if row["bottom"] <= item["center_y"] <= row["bottom"] + height * 0.20
            and abs(item["left"] - row["left"]) < width * 0.12
            and item["normalized"] != expected
        ]
        official_type_evidence = [
            item["text"]
            for item in nearby_rows
            if item["normalized"] == "公众号"
        ]
        original_content_evidence = [
            item["text"]
            for item in nearby_rows
            if "篇原创内容" in item["normalized"]
        ]
        personal_evidence = [
            item["text"] for item in nearby_rows if item["normalized"] == "个人"
        ]
        official_evidence = official_type_evidence + original_content_evidence
        if not official_evidence:
            return {
                "found": False,
                "reason": "同名搜索结果缺少公众号内容证据，拒绝点击可能的无关账号",
                "official_evidence": official_evidence,
                "original_content_evidence": original_content_evidence,
                "personal_evidence": personal_evidence,
            }

        # 主体公司通常位于类型文字下一行，保存下来便于后续审计同名账号。
        company = next(
            (
                item["text"]
                for item in nearby_rows
                if item["normalized"] != "公众号"
                and "篇原创内容" not in item["normalized"]
            ),
            "",
        )
        return {
            "found": True,
            "name": row["text"],
            "matched_name": row["text"],
            "name_match_method": name_match_method,
            "company": company,
            "official_evidence": official_evidence,
            "original_content_evidence": original_content_evidence,
            "is_official_account": True,
            "center_x_1000": round(row["center_x"] * 1000 / width),
            "center_y_1000": round(row["center_y"] * 1000 / height),
            # 名称点击未触发时改点同一结果左侧头像，兼容不同搜一搜版本的点击热区。
            "avatar_x_1000": round(max(0, row["left"] - width * 0.04) * 1000 / width),
            "avatar_y_1000": round(min(height, row["center_y"] + height * 0.015) * 1000 / height),
            "result_top": round(result_top, 1),
            "confidence": row["confidence"],
            "method": "rapidocr-sogou-result-structural-anchor",
        }

    def locate_account_tab(self, screenshot: Image.Image) -> dict[str, Any]:
        """定位搜一搜结果页的一级“账号”分类，不依赖上一次选中的分类。"""
        width, height = screenshot.size
        candidates = [
            row
            for row in self._rows(screenshot)
            if "账号" in row["normalized"]
            and height * 0.08 <= row["center_y"] <= height * 0.30
            and row["center_x"] < width * 0.55
        ]
        if not candidates:
            return {"found": False, "reason": "搜一搜结果页未找到一级账号分类"}
        row = min(candidates, key=lambda item: item["center_y"])
        # 导航间距较小时，RapidOCR 会把“账号 文章 划线”合并为一行。
        # 按字符位置还原“账号”子区域，避免误点相邻的文章分类。
        normalized = row["normalized"]
        account_index = normalized.index("账号")
        char_width = (row["right"] - row["left"]) / max(len(normalized), 1)
        account_left = row["left"] + char_width * account_index
        account_right = account_left + char_width * 2
        return {
            "found": True,
            "center_x_1000": round((account_left + account_right) / 2 * 1000 / width),
            "center_y_1000": round(row["center_y"] * 1000 / height),
            "confidence": row["confidence"],
            "ocr_text": row["text"],
            "method": "rapidocr-sogou-account-tab-subregion",
        }

    def locate_official_account_filter(self, screenshot: Image.Image) -> dict[str, Any]:
        """定位“账号”分类下的二级“公众号”筛选项。"""
        width, height = screenshot.size
        candidates = [
            row
            for row in self._rows(screenshot)
            if "公众号" in row["normalized"]
            and height * 0.12 <= row["center_y"] <= height * 0.38
            and row["center_x"] < width * 0.55
        ]
        if not candidates:
            return {"found": False, "reason": "账号结果页未找到二级公众号筛选项"}

        row = min(candidates, key=lambda item: item["center_y"])
        normalized = row["normalized"]
        official_index = normalized.index("公众号")
        char_width = (row["right"] - row["left"]) / max(len(normalized), 1)
        official_left = row["left"] + char_width * official_index
        official_right = official_left + char_width * 3
        return {
            "found": True,
            "center_x_1000": round((official_left + official_right) / 2 * 1000 / width),
            "center_y_1000": round(row["center_y"] * 1000 / height),
            "confidence": row["confidence"],
            "ocr_text": row["text"],
            "method": "rapidocr-sogou-official-account-filter-subregion",
        }

    def validate_official_account_filter_selected(self, screenshot: Image.Image) -> dict[str, Any]:
        """通过文字颜色确认二级“公众号”已选中；选中项明显深于其他筛选项。"""
        width, height = screenshot.size
        candidates = [
            row
            for row in self._rows(screenshot)
            if "公众号" in row["normalized"]
            and height * 0.12 <= row["center_y"] <= height * 0.38
            and row["center_x"] < width * 0.55
        ]
        if not candidates:
            return {"selected": False, "reason": "未找到二级公众号筛选文字"}
        row = min(candidates, key=lambda item: item["center_y"])
        normalized = row["normalized"]
        official_index = normalized.index("公众号")
        char_width = (row["right"] - row["left"]) / max(len(normalized), 1)
        left = max(0, round(row["left"] + char_width * official_index))
        right = min(width, round(left + char_width * 3))
        top = max(0, round(row["top"]))
        bottom = min(height, round(row["bottom"]))
        crop = np.asarray(screenshot.convert("L").crop((left, top, right, bottom)))
        foreground = crop[crop < 220]
        median = float(np.median(foreground)) if foreground.size else 255.0
        selected = median <= 100
        return {
            "selected": selected,
            "foreground_median": round(median, 2),
            "reason": "" if selected else "公众号文字仍为未选中的浅色",
            "method": "opencv-secondary-filter-text-darkness",
        }

    def validate_account_tab_selected(self, screenshot: Image.Image) -> dict[str, Any]:
        """通过账号下划线和二级账号筛选项确认点击确实生效。"""
        width, height = screenshot.size
        rows = self._rows(screenshot)
        exact_account_rows = [
            row
            for row in rows
            if row["normalized"] == "账号"
            and height * 0.08 <= row["center_y"] <= height * 0.30
            and row["center_x"] < width * 0.55
        ]
        if exact_account_rows:
            account = min(exact_account_rows, key=lambda item: item["center_y"])
        else:
            # RapidOCR 偶尔会把相邻的“全部”“账号”合并，按末尾两个汉字估算账号区域。
            merged_rows = [
                row
                for row in rows
                if "账号" in row["normalized"]
                and height * 0.08 <= row["center_y"] <= height * 0.30
                and row["center_x"] < width * 0.55
            ]
            if not merged_rows:
                return {"selected": False, "reason": "点击后未找到账号分类文字"}
            merged = min(merged_rows, key=lambda item: item["center_y"])
            normalized = merged["normalized"]
            account_index = normalized.index("账号")
            char_width = (merged["right"] - merged["left"]) / max(len(normalized), 1)
            account = {
                **merged,
                "left": merged["left"] + char_width * account_index,
                "right": merged["left"] + char_width * (account_index + 2),
            }

        # 选中状态会在文字正下方显示一条较长的深色横线。
        left = max(0, round(account["left"] - width * 0.02))
        right = min(width, round(account["right"] + width * 0.02))
        top = max(0, round(account["bottom"] + 2))
        bottom = min(height, round(account["bottom"] + height * 0.035))
        underline_crop = np.asarray(
            screenshot.convert("L").crop((left, top, right, bottom))
        )
        if underline_crop.size:
            dark_ratio_by_row = (underline_crop < 100).mean(axis=1)
            underline_ratio = float(dark_ratio_by_row.max())
        else:
            underline_ratio = 0.0

        filter_names = {"不限", "小程序", "公众号", "服务号", "视频号"}
        visible_filters = sorted(
            {
                row["normalized"]
                for row in rows
                if row["normalized"] in filter_names
                and account["bottom"] < row["center_y"] < account["bottom"] + height * 0.13
            }
        )
        browser_title_confirmed = any(
            "账号" in row["normalized"] and "搜一搜" in row["normalized"]
            for row in rows
            if row["center_y"] < height * 0.07
        )
        # 新版搜一搜账号页不一定展示“不限/公众号/服务号”等二级筛选项，
        # 但会出现“搜索词-账号”的结果区标题或明确的“公众号”类型文字。
        account_result_confirmed = any(
            (
                row["normalized"].endswith("-账号")
                or row["normalized"] == "公众号"
            )
            and account["bottom"] < row["center_y"] < height * 0.55
            for row in rows
        )
        # 浏览器标签标题可能被截断，或被 OCR 识别成“搜”而不是“搜一搜”。
        # 它只能作为辅助证据；页面上的账号下划线、筛选项和结果区才是主判断依据。
        selected = (
            underline_ratio >= 0.45
            and (len(visible_filters) >= 3 or account_result_confirmed)
        )
        return {
            "selected": selected,
            "underline_ratio": round(underline_ratio, 4),
            "visible_account_filters": visible_filters,
            "browser_title_confirmed": browser_title_confirmed,
            "account_result_confirmed": account_result_confirmed,
            "reason": "" if selected else "账号下划线或账号结果证据未确认",
            "method": "opencv-underline-plus-account-result-evidence",
        }

    def validate_profile_header(self, screenshot: Image.Image, expected_name: str) -> dict[str, Any]:
        """校验公众号资料窗口顶部名称，避免同名候选或旧窗口串号。"""
        width, height = screenshot.size
        header_rows = [
            row
            for row in self._rows(screenshot)
            if row["center_y"] < height * 0.20 and row["center_x"] < width * 0.65
        ]
        matches = [
            row
            for row in header_rows
            if _account_name_match(expected_name, row["text"])[0]
        ]
        if not matches:
            return {
                "matched": False,
                "reason": "公众号资料窗口顶部名称不匹配",
                # 记录实际读到的顶部候选，便于区分 OCR 偏差、旧窗口残留和误点。
                "observed_header_candidates": [row["text"] for row in header_rows[:8]],
            }
        row = min(matches, key=lambda item: item["center_y"])
        _, name_match_method = _account_name_match(expected_name, row["text"])
        return {
            "matched": True,
            "name": row["text"],
            "confidence": row["confidence"],
            "method": f"rapidocr-profile-header-{name_match_method}",
        }

    def inspect_profile_feed(self, screenshot: Image.Image) -> dict[str, Any]:
        """识别资料窗口中的时间分组、文章标题、阅读数和点赞数。"""
        started = time.perf_counter()
        width, height = screenshot.size
        rows = self._rows(screenshot)
        labels = []
        label_ids: set[int] = set()
        metric_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            normalized = row["normalized"]
            combined = COMBINED_METRICS_RE.fullmatch(normalized)
            if combined:
                # OCR 偶尔会把标题末行和“阅读/赞”合成一个文本框，先拆开再参与分组。
                metric = dict(row)
                metric["normalized"] = f"阅读{combined.group(2)}赞{combined.group(3)}"
                metric["combined_title"] = combined.group(1)
                metric_rows.append(metric)
                row = dict(row)
                row["text"] = combined.group(1)
                row["normalized"] = combined.group(1)
                rows[index] = row
                normalized = row["normalized"]
            is_time_label = bool(TIME_LABEL_RE.fullmatch(normalized))
            # 新版微信常把“昨天 20:23”合并成一个时间标签，兼容这种形式。
            is_time_label = is_time_label or bool(
                re.fullmatch(r"(?:今天|昨天)\s+\d{1,2}:\d{2}", normalized)
            )
            is_section_label = normalized in NON_ARTICLE_SECTION_LABELS
            if is_time_label or is_section_label:
                # 翻页后日期标签可能已经靠近窗口顶部，不能只检查首屏下半区。
                # 顶部导航也有“贴图/视频号”，专区标签只接受内容区中的同名文本。
                minimum_y = height * (0.20 if is_section_label else 0.08)
                if row["center_y"] > minimum_y and row["left"] < width * 0.45:
                    label_ids.add(index)
                    labels.append(
                        {
                            "text": normalized,
                            "center_y_1000": round(row["center_y"] * 1000 / height),
                            "confidence": row["confidence"],
                        }
                    )
            if METRICS_RE.fullmatch(normalized):
                metric_rows.append(row)

        excluded = {
            "全部", "贴图", "文章", "视频号", "私信", "已关注",
            "AI开源项目", "AI研究前沿", "AI产业动态",
        }
        title_rows = []
        for index, row in enumerate(rows):
            normalized = row["normalized"]
            if index in label_ids or normalized in excluded or METRICS_RE.fullmatch(normalized):
                continue
            if row["left"] < width * 0.15 or row["left"] > width * 0.62:
                continue
            # 资料页滚动后第一张卡片可能紧贴顶部，后续仍会通过时间分组约束是否采集。
            if not height * 0.08 <= row["center_y"] <= height * 0.97:
                continue
            if len(normalized) < 5:
                continue
            title_rows.append(row)

        title_rows.sort(key=lambda row: (row["center_y"], row["left"]))

        # 公众号卡片的真实标题位于“阅读/赞”指标正上方；封面图内部可能包含
        # Logo、榜单、表格和海报文案，不能把所有 OCR 行都视为文章标题。
        # 优先以指标行为锚点反向寻找相邻标题，只在整页完全没有指标时才使用
        # 旧的自由文本分组作为兼容回退。
        anchored_groups: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
        for metric in sorted(metric_rows, key=lambda row: (row["center_y"], row["left"])):
            combined_title = str(metric.get("combined_title") or "").strip()
            if combined_title:
                title_row = dict(metric)
                title_row["text"] = combined_title
                title_row["normalized"] = _normalize(combined_title)
                anchored_groups.append(([title_row], metric))
                continue

            candidates = [
                row
                for row in title_rows
                if row["bottom"] <= metric["top"] + height * 0.008
                and 0 <= metric["top"] - row["bottom"] <= height * 0.12
                # 标题和指标左边缘基本对齐。适度放宽以兼容 OCR 框偏移，
                # 但不能跨到双列布局中的相邻卡片。
                and abs(row["left"] - metric["left"]) <= width * 0.12
                and metric["left"] - width * 0.05
                <= row["center_x"]
                <= metric["left"] + width * 0.36
            ]
            if not candidates:
                continue
            nearest = max(candidates, key=lambda row: (row["bottom"], row["confidence"]))
            group = [nearest]
            # 只取最靠近指标的一行。卡片标题被截断时，后续标题校验支持可靠的
            # 8 字以上前缀；向上盲目合并反而会吞入封面底部的表格或海报文字。
            anchored_groups.append((group, metric))

        if anchored_groups:
            articles = []
            seen_anchors: set[tuple[str, int, int]] = set()
            for group, metric in anchored_groups:
                title = "".join(row["text"] for row in group).strip()
                top = min(row["top"] for row in group)
                bottom = max(row["bottom"] for row in group)
                left = min(row["left"] for row in group)
                right = max(row["right"] for row in group)
                match = METRICS_RE.fullmatch(metric["normalized"])
                read_count = _number(match.group(1)) if match else None
                like_count = _number(match.group(2)) if match else None
                anchor_key = (_normalize(title), round(metric["center_x"]), round(metric["center_y"]))
                if not title or anchor_key in seen_anchors:
                    continue
                seen_anchors.add(anchor_key)
                articles.append(
                    {
                        "title": title,
                        "center_x_1000": round(((left + right) / 2) * 1000 / width),
                        "center_y_1000": round(((top + bottom) / 2) * 1000 / height),
                        "confidence": round(min(row["confidence"] for row in group), 4),
                        "list_read_count": read_count,
                        "list_like_count": like_count,
                    }
                )
            return {
                "time_labels": sorted(labels, key=lambda item: item["center_y_1000"]),
                "articles": articles,
                "recognition_method": "rapidocr-profile-feed-metric-anchored",
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }

        groups: list[list[dict[str, Any]]] = []
        for row in title_rows:
            if not groups:
                groups.append([row])
                continue
            previous = groups[-1][-1]
            gap = row["top"] - previous["bottom"]
            if gap <= height * 0.035 and abs(row["left"] - previous["left"]) <= width * 0.06:
                groups[-1].append(row)
            else:
                groups.append([row])

        articles = []
        for group in groups:
            title = "".join(row["text"] for row in group).strip()
            top = min(row["top"] for row in group)
            bottom = max(row["bottom"] for row in group)
            left = min(row["left"] for row in group)
            right = max(row["right"] for row in group)
            metric = next(
                (
                    row
                    for row in metric_rows
                    if bottom <= row["center_y"] <= bottom + height * 0.055
                ),
                None,
            )
            read_count = like_count = None
            if metric:
                match = METRICS_RE.fullmatch(metric["normalized"])
                if match:
                    read_count = _number(match.group(1))
                    like_count = _number(match.group(2))
            articles.append(
                {
                    "title": title,
                    "center_x_1000": round(((left + right) / 2) * 1000 / width),
                    "center_y_1000": round(((top + bottom) / 2) * 1000 / height),
                    "confidence": round(min(row["confidence"] for row in group), 4),
                    "list_read_count": read_count,
                    "list_like_count": like_count,
                }
            )

        return {
            "time_labels": sorted(labels, key=lambda item: item["center_y_1000"]),
            "articles": articles,
            "recognition_method": "rapidocr-profile-feed",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
