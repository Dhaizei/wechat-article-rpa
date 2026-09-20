"""持久化本程序确认创建的标签；页面身份变化时保留页面。"""
import json
import os
from dataclasses import asdict
from pathlib import Path
from browser_navigation import Page


class TabOwnership:
    def __init__(self, path: Path):
        self.path = path

    def records(self):
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("标签归属记录格式错误，停止自动清理")
        return data

    def _save(self, records):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)

    def remember(self, session: str, page: Page):
        if not session or not page.key or page.role not in ("article", "profile"):
            return
        records = [r for r in self.records() if not (r["session"] == session and r["page"]["key"] == page.key)]
        records.append({"session": session, "page": asdict(page)})
        self._save(records)

    def forget(self, session: str, page: Page):
        self._save([r for r in self.records() if not (r["session"] == session and r["page"]["key"] == page.key)])

    def reuse(self, session: str, old: Page, new: Page) -> bool:
        """只转移已有归属，不能因微信复用了用户页面就取得关闭权。"""
        if not session or old.role != "profile" or new.role != "profile" or old.hwnd != new.hwnd:
            return False
        records = self.records()
        if any(r["session"] == session and r["page"] == asdict(new) for r in records):
            return True
        if not any(r["session"] == session and r["page"] == asdict(old) for r in records):
            return False
        records = [r for r in records if not (r["session"] == session and r["page"]["key"] == old.key)]
        records.append({"session": session, "page": asdict(new)})
        self._save(records)
        return True

    def cleanup(self, session: str, navigator):
        """只关闭同一进程会话中 key、窗口、标题和角色均未改变的标签。"""
        candidates = [r for r in self.records() if r["session"] == session]
        closed = 0
        # 文章先关闭，再清理公众号主页；搜索页从不登记。
        candidates.sort(key=lambda r: r["page"]["role"] != "article")
        for record in candidates:
            owned = Page(**record["page"])
            def matches(page):
                return (page.hwnd, page.key, page.name, page.role) == (owned.hwnd, owned.key, owned.name, owned.role)
            if not navigator.select(matches):
                continue
            if not matches(navigator.observe()):
                raise RuntimeError("清理前标签身份已改变，未发送关闭操作")
            navigator.close_tab()
            if any(matches(p) for p in navigator.inventory()):
                raise RuntimeError("遗留标签关闭未生效，停止清理")
            self.forget(session, owned)
            closed += 1
        return closed

