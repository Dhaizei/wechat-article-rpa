"""调用 OpenAI 兼容的 Qwen-VL 接口解析微信界面截图。"""

from __future__ import annotations

import base64
import io
import json
import os
from dataclasses import dataclass
from typing import Any

import requests
from PIL import Image


@dataclass(frozen=True)
class QwenVisionConfig:
    base_url: str
    api_key: str
    model: str = "dashscope/qwen3-vl-plus"
    timeout_seconds: int = 120

    @classmethod
    def from_env(cls) -> "QwenVisionConfig":
        api_key = os.getenv("QWEN_VL_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("缺少环境变量 QWEN_VL_API_KEY")
        return cls(
            base_url=os.getenv("QWEN_VL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/"),
            api_key=api_key,
            model=os.getenv("QWEN_VL_MODEL", "dashscope/qwen3-vl-plus"),
        )


class QwenVisionClient:
    def __init__(self, config: QwenVisionConfig) -> None:
        self.config = config

    @staticmethod
    def _image_data_url(image: Image.Image) -> str:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=94)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        content = content.strip()
        if content.startswith("```"):
            content = content.removeprefix("```json").removeprefix("```")
            content = content.removesuffix("```").strip()
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("模型返回值不是 JSON 对象")
        return parsed

    def analyze(self, image: Image.Image, prompt: str, max_tokens: int = 2500) -> dict[str, Any]:
        payload = {
            "model": self.config.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": self._image_data_url(image)}},
                ],
            }],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        response = requests.post(
            f"{self.config.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        return self._parse_json(data["choices"][0]["message"]["content"])

    def detect_accounts(self, sidebar: Image.Image) -> list[dict[str, Any]]:
        prompt = """This is the complete LEFT sidebar of a WeChat official-account manager.
Detect EVERY visible account row from top to bottom, including partially visible rows.
Exclude the header. Preserve Chinese names exactly. Return ONLY valid JSON:
{"accounts":[{"name":"string","center_x_1000":integer,"center_y_1000":integer,"confidence":number}]}.
Coordinates must be normalized integers from 0 to 1000 relative to this cropped image."""
        result = self.analyze(sidebar, prompt)
        accounts = result.get("accounts", [])
        return accounts if isinstance(accounts, list) else []

    def detect_manager_layout(self, window: Image.Image) -> dict[str, Any]:
        prompt = """Locate the WeChat official-account manager inside this complete WeChat window.
IMPORTANT: the far-left area contains WeChat navigation and a conversation list with a search
box. That conversation list may itself contain one selected row named 公众号. IGNORE that whole
conversation list. The requested account sidebar is the SECOND vertical list to its right: it has
its own header 公众号 and contains rows such as 趣车书、北京日报、第一财经. The article content panel
is immediately to the right of this second list. Return ONLY valid JSON:
{"is_manager_visible":boolean,"account_sidebar_bbox_1000":[left,top,right,bottom],
"article_content_bbox_1000":[left,top,right,bottom],"confidence":number}.
All bbox values are normalized integers from 0 to 1000 relative to the complete image.
The account sidebar bbox must NOT include the far-left search box, avatars, chat timestamps,
or the selected conversation row named 公众号. Exclude the window title bar."""
        return self.analyze(window, prompt, max_tokens=800)

    def detect_articles(self, content_area: Image.Image) -> list[dict[str, Any]]:
        prompt = """This is the RIGHT content area of a WeChat official-account manager.
Detect every clickable article card visible on screen, from top to bottom. A multi-article
bundle may contain multiple independently clickable article cards. Exclude navigation and
the current-account heading. Return ONLY valid JSON:
{"articles":[{"title":"string","center_x_1000":integer,"center_y_1000":integer,"visible_time":"string","confidence":number}]}.
Coordinates must be normalized integers from 0 to 1000 relative to this cropped image.
Do not infer a time for an article if the time label cannot be associated reliably; use an empty string."""
        result = self.analyze(content_area, prompt)
        articles = result.get("articles", [])
        return articles if isinstance(articles, list) else []

    def detect_search_account(self, window: Image.Image, expected_name: str) -> dict[str, Any]:
        prompt = f"""This is a WeChat search-result window after searching for {expected_name!r}.
Locate the exact official-account row under the section labeled 公众号. Ignore chat history,
favorites and web search suggestions. Return ONLY valid JSON:
{{"found":boolean,"name":string|null,"center_x_1000":integer|null,
"center_y_1000":integer|null,"avatar_x_1000":integer|null,
"avatar_y_1000":integer|null,"confidence":number}}.
Only set found=true when the visible account name exactly equals {expected_name!r}.
Coordinates are normalized from 0 to 1000 relative to the complete window."""
        return self.analyze(window, prompt, max_tokens=500)

    def verify_profile_header(self, window: Image.Image, expected_name: str) -> dict[str, Any]:
        """复核公众号资料窗口头部名称。

        这个方法只负责证明“当前资料页是不是此账号”，不返回点击坐标，
        避免视觉模型在资料窗口上进行任何高风险操作。
        """
        prompt = f"""This is a WeChat official-account profile window.
Read only the account name shown in the profile header. Return ONLY valid JSON:
{{"matched":boolean,"name":string|null,"confidence":number}}.
Set matched=true ONLY when the visible header name exactly equals {expected_name!r}.
Do not infer the name from article titles, the browser title, or previously supplied context."""
        return self.analyze(window, prompt, max_tokens=300)

    def inspect_profile_feed(self, window: Image.Image) -> dict[str, Any]:
        """解析公众号资料页中的时间分组与可点击文章卡片。"""
        prompt = """Inspect this complete WeChat official-account profile window.
Return every independently clickable article card and every time divider that is ACTUALLY
VISIBLE in this screenshot. Time dividers may look like 今天 11:35, 昨天 11:11, 11:35,
星期一, 7月16日12:29, or 2026年7月16日. Do not infer, repeat, or invent a time
label when it is outside the screenshot. Return ONLY valid JSON:
{"time_labels":[{"text":"string","center_y_1000":integer}],
"articles":[{"title":"string","center_x_1000":integer,
"center_y_1000":integer,"confidence":number}]}.
Coordinates are normalized relative to the complete window. Exclude profile navigation,
topic chips, pinned-section controls, and bottom navigation buttons."""
        result = self.analyze(window, prompt, max_tokens=1800)
        if not isinstance(result.get("time_labels"), list):
            result["time_labels"] = []
        if not isinstance(result.get("articles"), list):
            result["articles"] = []
        return result

    def detect_account_articles(self, window: Image.Image) -> list[dict[str, Any]]:
        prompt = """This is a WeChat official-account message window. Detect every visible,
independently clickable article card from top to bottom. Multi-article bundles contain one large
lead card followed by smaller cards; return each one separately. Preserve each visible title.
Track the nearest time-group label above the bundle, such as 今天 11:35 or 昨天 11:11.
Return ONLY valid JSON:
{"articles":[{"title":"string","group_time":"string","center_x_1000":integer,
"center_y_1000":integer,"confidence":number}]}.
Coordinates are normalized relative to the complete window. Exclude bottom navigation buttons."""
        result = self.analyze(window, prompt, max_tokens=1800)
        articles = result.get("articles", [])
        return articles if isinstance(articles, list) else []

    def inspect_account_feed(self, window: Image.Image) -> dict[str, Any]:
        prompt = """Inspect this complete WeChat official-account message window.
Return every independently clickable article card AND every time divider that is ACTUALLY VISIBLE.
Do not infer, repeat, or invent a time label when its text is outside the screenshot.
Time dividers may look like 11:35, 昨天 11:11, 星期三, 周4, 7月16日 12:29,
or 2026年7月16日. Return ONLY valid JSON:
{"time_labels":[{"text":"string","center_y_1000":integer}],
"articles":[{"title":"string","center_x_1000":integer,
"center_y_1000":integer,"confidence":number}]}.
Coordinates are normalized relative to the complete window. Exclude bottom navigation buttons."""
        result = self.analyze(window, prompt, max_tokens=1800)
        if not isinstance(result.get("time_labels"), list):
            result["time_labels"] = []
        if not isinstance(result.get("articles"), list):
            result["articles"] = []
        return result

    def extract_article_metrics(self, article_view: Image.Image) -> dict[str, Any]:
        prompt = """Extract metadata and engagement counts from this WeChat article screenshot.
Return ONLY valid JSON. Use null when a field is not visible; never guess.
Schema: {"title":string|null,"account_name":string|null,"publish_time":string|null,
"location":string|null,"read_count":integer|null,"like_count":integer|null,
"share_count":integer|null,"favorite_count":integer|null,"comment_count":integer|null}.
The four bottom icons from left to right are like, share, favorite, comment.
A headphone icon near article metadata is not read count."""
        return self.analyze(article_view, prompt, max_tokens=1200)

    def extract_interaction_counts(self, article_footer: Image.Image) -> dict[str, Any]:
        """只识别文章底部互动栏，避免让视觉模型重复解析页面元数据。"""
        prompt = """This image shows the bottom engagement bar of a WeChat article.
Return ONLY valid JSON and use null for any value not clearly visible:
{"like_count":integer|null,"share_count":integer|null,
"favorite_count":integer|null,"comment_count":integer|null}.
The four icons from left to right are like, share, favorite, comment. Never guess."""
        return self.analyze(article_footer, prompt, max_tokens=500)
