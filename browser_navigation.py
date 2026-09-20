"""微信页面导航状态：用页面证据决定返回操作，不猜测窗口或标签序号。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from title_geometry import adjacent_title_lines


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def picture_share_count(data: dict, account: str, title: str) -> int | None:
    """图片消息使用文字互动栏；限定正文末尾和账号，排除评论及推荐卡片。"""
    for doc in data.get("documents", []):
        if normalize(doc.get("name", "")) != normalize(title):
            continue
        names = [normalize(row.get("text", "")) for row in doc.get("rows", [])]
        if "更多相关贴图" not in names:
            continue
        footer = names[:names.index("更多相关贴图")][-12:]
        if normalize(account) not in footer:
            continue
        footer = footer[len(footer) - 1 - footer[::-1].index(normalize(account)) + 1:]
        counts = [int(m[1]) for text in footer if (m := re.fullmatch(r"分享(\d+)", text))]
        if len(counts) == 1 and any(re.fullmatch(r"赞\d+", text) for text in footer):
            return counts[0]
    return None


@dataclass(frozen=True)
class Page:
    hwnd: int
    key: str
    role: str
    name: str
    account: str = ""


def page_from_probe(data: dict, account: str = "") -> Page:
    """主页要求名称、分类导航和私信/关注控件同时成立，避免撞到正文署名。"""
    hwnd = int(data.get("hwnd", 0))
    documents = data.get("documents", [])
    for doc in documents:
        names = {normalize(row["text"]) for row in doc.get("rows", [])}
        name = doc.get("name", "")
        key = str(doc.get("id", [])) + ":" + normalize(name)
        profile = {"全部", "文章"} <= names and bool({"私信", "关注", "已关注"} & names)
        if profile and (not account or normalize(account) == normalize(name)):
            return Page(hwnd, key, "profile", name, name)
        if "搜一搜" in name and "搜索" in names:
            return Page(hwnd, key, "search", name)
        has_date = any(re.search(r"\d{4}年\d{1,2}月\d{1,2}日", n) for n in names)
        if name and has_date and (not account or normalize(account) in names):
            return Page(hwnd, key, "article", name, account)
    if documents:
        doc = documents[0]
        name = doc.get("name", "")
        return Page(hwnd, str(doc.get("id", [])) + ":" + normalize(name), "unknown", name)
    return Page(hwnd, "", "unknown", "")


@dataclass(frozen=True)
class Checkpoint:
    origin: Page
    pages: tuple[Page, ...]
    target_account: str = ""
    tabs: tuple = ()
    origin_tab: tuple = ()


@dataclass
class InventoryCache:
    signature: tuple = ()
    pages: tuple[Page, ...] = ()


def tab_signature(data: dict) -> tuple:
    """只接受完整、唯一的标签身份；缺失证据时禁用缓存。"""
    tabs = data.get("tabs", [])
    if not tabs and data.get("tab_probe_mode") == "custom-unverified":
        # 此标记仅表示结构身份，不是标题；当前页面仍单独校验。
        tabs = [dict(t, name="<custom-tab>") for t in data.get("custom_tabs", [])]
    if not data.get("ok") or not tabs:
        return ()
    rows = tuple((tuple(t.get("id") or ()), t.get("name") or "") for t in tabs)
    if any(not identity or not name for identity, name in rows):
        return ()
    return rows if len({identity for identity, _ in rows}) == len(rows) else ()


def selected_tab(data: dict) -> tuple:
    if not tab_signature(data):
        return ()
    selected = [tuple(t["id"]) for t in data["tabs"] if t.get("selected") is True]
    return selected[0] if len(selected) == 1 else ()


def feed_from_probe(data: dict, rect: tuple[int, int, int, int], account: str = "") -> dict:
    """按文档顺序将日期、完整标题与阅读/点赞行关联，只点击视口内卡片。"""
    page = page_from_probe(data, account)
    if page.role != "profile":
        raise RuntimeError("无障碍页面不是目标公众号主页")
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    articles, labels = [], []
    date = ""
    previous = None
    title_rows = []
    date_pattern = r"(?:置顶|今天|昨天|(?:星期|周)[一二三四五六日天]|(?:\d{4}年)?\d{1,2}月\d{1,2}日)"
    for doc in data["documents"]:
        if normalize(doc["name"]) != normalize(page.name):
            continue
        for row in doc["rows"]:
            text = normalize(row["text"])
            if re.fullmatch(date_pattern, text):
                date = text
                previous = None
                title_rows = []
                continue
            metric = re.fullmatch(r"阅读([\d.]+万?\+?)赞([\d.]+万?\+?)", text)
            if metric:
                if previous and date:
                    x1, y1, x2, y2 = previous["rect"]
                    x, y = (x1 + x2) / 2, (y1 + y2) / 2
                    if not previous.get("offscreen") and left < x < right and top + 50 < y < bottom:
                        cy = round((y - top) * 1000 / height)
                        labels.append({"text": date, "center_y_1000": cy - 1, "confidence": 1.0})
                        def number(value: str) -> int:
                            return round(float(value.rstrip("万+")) * (10000 if "万" in value else 1))
                        group = adjacent_title_lines(title_rows, title_rows[-1])
                        articles.append({"title": "".join(r["text"] for r in group), "center_x_1000": round((x-left)*1000/width),
                                         "center_y_1000": cy, "screen_point": (round(x), round(y)),
                                         "time_group": date,
                                         "list_read_count": number(metric[1]), "list_like_count": number(metric[2]),
                                         "list_metrics_display": [metric[1], metric[2]], "confidence": 1.0})
                previous = None
                title_rows = []
            elif re.fullmatch(r"(?:阅读|赞|分享|在看)[\d.万亿+]+", text):
                previous = None  # 拆分的互动控件同样不能成为标题。
                title_rows = []
            elif text:
                previous = row
                x1, y1, x2, y2 = row["rect"]
                title_rows.append(dict(text=row["text"], left=x1, top=y1, right=x2, bottom=y2))
    # 同一控件可能经父子节点重复暴露；仅合并标题、日期和物理位置完全相同的卡片。
    unique = {(a["time_group"], normalize(a["title"]), a["screen_point"]): a for a in articles}
    labels = list({(label["text"], label["center_y_1000"]): label for label in labels}.values())
    return {"time_labels": labels, "articles": list(unique.values()), "recognition_method": "uia-profile-feed"}


def return_action(before: Checkpoint, current: Page, after: tuple[Page, ...]) -> str:
    """只有完整基线仍存在且只新增目标页面时，才允许关闭标签。"""
    if current == before.origin:
        return "unchanged"
    expected_role = "profile" if before.origin.role == "search" else "article"
    expected_account = before.target_account or before.origin.account
    if (current.hwnd != before.origin.hwnd or current.role != expected_role
            or normalize(current.account) != normalize(expected_account)):
        return "unknown"
    old = {page.key for page in before.pages}
    new = {page.key for page in after}
    if not old or "" in old or "" in new:
        return "unknown"
    if old == new and current.key in old:
        return "existing_tab"
    if old <= new and new - old == {current.key}:
        return "new_tab"
    if len(old) == len(new) and old - new == {before.origin.key} and new - old == {current.key}:
        return "same_tab"
    # 微信可能复用另一个公众号主页，搜索页本身仍然保留。
    removed = [p for p in before.pages if p.key in old - new]
    if (before.origin.role == "search" and before.origin.key in new
            and len(old) == len(new) and new - old == {current.key}
            and len(removed) == 1 and removed[0].role == "profile"
            and removed[0].hwnd == current.hwnd):
        return "reused_profile_tab"
    return "unknown"


class Navigator:
    """操作通过回调接入采集器，便于离线验证所有清理边界。"""

    def __init__(self, observe: Callable[[], Page], next_tab: Callable[[], None],
                 close_tab: Callable[[], None], back: Callable[[], None], limit: int = 12,
                 trace: Callable[[dict], None] | None = None,
                 on_created: Callable[[Page], None] | None = None,
                 on_closed: Callable[[Page], None] | None = None,
                 on_reused: Callable[[Page, Page], bool] | None = None,
                 signature: Callable[[], tuple] | None = None,
                 cache: InventoryCache | None = None,
                 selected: Callable[[], tuple] | None = None,
                 select_tab: Callable[[tuple], bool] | None = None,
                 custom_tabs: Callable[[], bool] | None = None):
        self.observe = observe
        self.next_tab = next_tab
        self.close_tab = close_tab
        self.back = back
        self.limit = limit
        self.trace = trace or (lambda data: None)
        self.on_created = on_created or (lambda page: None)
        self.on_closed = on_closed or (lambda page: None)
        self.on_reused = on_reused or (lambda old, new: False)
        self.signature = signature or (lambda: ())
        self.cache = cache if cache is not None else InventoryCache()
        self.selected = selected or (lambda: ())
        self.select_tab = select_tab or (lambda identity: False)
        self.custom_tabs = custom_tabs or (lambda: False)

    def confirm_added(self, before: Checkpoint, current: Page) -> bool:
        if self.added_target(before, current):
            return True
        old, new = dict(before.tabs), dict(self.signature())
        added = set(new) - set(old)
        role = "profile" if before.origin.role == "search" else "article"
        if (not self.custom_tabs() or not before.origin_tab or len(added) != 1
                or len(new) != len(old) + 1 or any(new.get(k) != v for k, v in old.items())
                or current.role != role or current.hwnd != before.origin.hwnd
                or normalize(current.account) != normalize(before.target_account or before.origin.account)):
            return False
        # 自定义标签没有 selected 状态：直接选择唯一新增标签，并再次核对文档。
        # 这一步不离开目标文章，也不把“新增”直接等同于当前活动页。
        target = next(iter(added))
        if not self.select_tab(target) or self.observe() != current:
            raise RuntimeError("直接选择新增标签后页面身份不一致，已停止导航")
        return self.added_target(before, current)

    def added_target(self, before: Checkpoint, current: Page) -> bool:
        """仅唯一新增的目标标签可走快速路径；复用、替换和不完整证据回退。"""
        tabs, selected = self.signature(), self.selected()
        old, new = dict(before.tabs), dict(tabs)
        role = "profile" if before.origin.role == "search" else "article"
        return bool(before.origin_tab and before.origin_tab in old and selected
                    and len(tabs) == len(before.tabs) + 1
                    and set(new) - set(old) == {selected}
                    and all(new.get(k) == v for k, v in old.items())
                    and current.key and current.key not in {p.key for p in before.pages}
                    and current.hwnd == before.origin.hwnd and current.role == role
                    and normalize(current.account) == normalize(before.target_account or before.origin.account))

    def cache_added(self, before: Checkpoint, current: Page) -> None:
        self.cache.signature = self.signature()
        self.cache.pages = (current,) + before.pages

    def trace_fallback(self, before: Checkpoint, current: Page, phase: str) -> None:
        """仅记录证据缺口，不进行额外探测或改变导航判断。"""
        tabs, selected = self.signature(), self.selected()
        old, new = dict(before.tabs), dict(tabs)
        expected_role = "profile" if before.origin.role == "search" else "article"
        checks = {
            "baseline_tabs_missing": not before.tabs,
            "current_tabs_missing": not tabs,
            "origin_tab_unbound": not before.origin_tab,
            "origin_tab_not_in_baseline": bool(before.origin_tab) and before.origin_tab not in old,
            "active_tab_unconfirmed": not selected,
            "not_single_added_tab": len(tabs) != len(before.tabs)+1 or len(set(new)-set(old)) != 1,
            "baseline_tabs_changed": any(new.get(k) != v for k, v in old.items()),
            "selected_tab_not_added": bool(selected) and set(new)-set(old) != {selected},
            "document_missing_or_existing": not current.key or current.key in {p.key for p in before.pages},
            "window_changed": current.hwnd != before.origin.hwnd,
            "page_role_mismatch": current.role != expected_role,
            "account_mismatch": normalize(current.account) != normalize(before.target_account or before.origin.account),
        }
        self.trace(dict(stage="direct_navigation_fallback", phase=phase,
                        reasons=[reason for reason, failed in checks.items() if failed],
                        custom_tabs=self.custom_tabs(), baseline_tab_count=len(before.tabs),
                        current_tab_count=len(tabs), current_role=current.role,
                        origin_tab=before.origin_tab, selected_tab=selected))

    def track_created(self, before: Checkpoint) -> None:
        current = self.observe()
        if self.confirm_added(before, current):
            self.on_created(current)
            self.cache_added(before, current)
            self.trace({"stage": "created_direct", "page": current.__dict__})
            return
        self.trace_fallback(before, current, "track_created")
        pages = self.inventory()
        action = return_action(before, current, pages)
        if action == "new_tab":
            self.on_created(current)
        elif action == "reused_profile_tab":
            removed = next(p for p in before.pages if p.key not in {q.key for q in pages})
            self.on_reused(removed, current)

    def inventory(self, *, force: bool = False) -> tuple[Page, ...]:
        first = self.observe()
        signature = self.signature()
        if (not force and signature and signature == self.cache.signature
                and first in self.cache.pages):
            index = self.cache.pages.index(first)
            self.trace({"stage": "inventory_cached", "page_count": len(self.cache.pages)})
            return self.cache.pages[index:] + self.cache.pages[:index]
        self.cache.signature, self.cache.pages = (), ()
        if not first.key:
            raise RuntimeError("无法确认当前页面，拒绝建立导航检查点")
        pages = [first]
        for _ in range(self.limit):
            self.next_tab()
            page = self.observe()
            if page.key == first.key:
                # 遍历期间标签也可能变化；前后不一致的快照不得缓存。
                if signature and signature == self.signature() and len(signature) == len(pages):
                    self.cache.signature, self.cache.pages = signature, tuple(pages)
                return tuple(pages)
            if not page.key or page.key in {p.key for p in pages}:
                raise RuntimeError("标签遍历状态不确定，已停止导航")
            pages.append(page)
        raise RuntimeError("标签数量超过导航上限，未关闭任何标签")

    def capture(self, target_account: str = "") -> Checkpoint:
        pages = self.inventory()
        return Checkpoint(pages[0], pages, target_account, self.signature(), self.selected())

    def checkpoint(self) -> Checkpoint:
        result = self.capture()
        pages = result.pages
        self.trace({"stage": "checkpoint", "pages": [p.__dict__ for p in pages]})
        if pages[0].role != "profile":
            raise RuntimeError("点击文章前未确认目标公众号主页")
        return result

    def select(self, predicate: Callable[[Page], bool]) -> bool:
        """有界查找已有页面；查找失败回到起点，不关闭任何用户标签。"""
        first = self.observe()
        if not first.key:
            return False
        for _ in range(self.limit):
            page = self.observe()
            if predicate(page):
                return True
            self.next_tab()
            if first.key and self.observe().key == first.key:
                return False
        raise RuntimeError("标签遍历超过上限，无法确认浏览器状态")

    def restore(self, before: Checkpoint) -> str:
        current = self.observe()
        if current == before.origin:
            return "unchanged"
        if self.confirm_added(before, current):
            added = self.selected()
            self.on_created(current)
            self.close_tab()
            restored = self.observe()
            # 关闭后标签集必须精确回到基线，不能只因为窗口切走就宣称关闭成功。
            if self.signature() != before.tabs:
                raise RuntimeError("关闭后标签列表与基线不一致，保留归属记录并停止")
            if self.selected() != before.origin_tab:
                if not self.select_tab(before.origin_tab):
                    # 直接选择不受支持时才有界搜索，绝不再关闭一次。
                    if not self.select(lambda p: p == before.origin):
                        raise RuntimeError("关闭后无法恢复原页面")
                restored = self.observe()
            if restored != before.origin or self.signature() != before.tabs:
                raise RuntimeError("关闭后原页面身份变化，已停止导航")
            self.on_closed(current)
            self.cache.signature, self.cache.pages = before.tabs, before.pages
            self.trace({"stage": "restore_direct", "closed_tab": added})
            return "new_tab"
        # 关闭是破坏性动作，必须重新确认所有页面，不以缓存授予关闭权。
        self.trace_fallback(before, current, "restore")
        pages = self.inventory(force=True)
        action = return_action(before, current, pages)
        closed_owned = action == "new_tab"
        self.trace({"stage": "restore", "origin": before.origin.__dict__,
                    "before": [p.__dict__ for p in before.pages],
                    "current": current.__dict__, "after": [p.__dict__ for p in pages], "action": action})
        if action == "new_tab":
            self.on_created(current)
            self.close_tab()
        elif action == "same_tab":
            self.back()
        elif action == "existing_tab":
            pass  # 微信可能复用用户已经打开的文章标签，返回主页即可，不拥有关闭权。
        elif action == "reused_profile_tab":
            removed = next(p for p in before.pages if p.key not in {q.key for q in pages})
            closed_owned = self.on_reused(removed, current)
            if closed_owned:
                self.close_tab()
        else:
            # 页面归属不明时没有关闭权，但仍可返回精确匹配的原页面。
            # 保留异常页供诊断，防止单个页面让后续公众号找不到搜索入口。
            if self.select(lambda page: page == before.origin):
                return "preserved_unknown"
            raise RuntimeError("页面跳转归属不确定，保留所有页面并停止本公众号")
        # 新标签关闭后浏览器不一定选择原主页，按身份查找而不是写死 Ctrl+2。
        for _ in range(self.limit):
            page = self.observe()
            if (page.role == before.origin.role
                    and normalize(page.name) == normalize(before.origin.name)
                    and normalize(page.account) == normalize(before.origin.account)
                    and (page.key == before.origin.key or (
                        action == "same_tab" and not any(
                            p.key != before.origin.key and p.role == page.role
                            and normalize(p.name) == normalize(page.name) for p in before.pages)))):
                if closed_owned:
                    if any(p.key == current.key for p in self.inventory(force=True)):
                        raise RuntimeError("文章标签关闭未生效，保留归属记录")
                    self.on_closed(current)
                return action
            self.next_tab()
        raise RuntimeError("返回后未找到目标公众号主页，已停止本公众号")

