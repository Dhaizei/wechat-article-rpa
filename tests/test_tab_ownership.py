"""跨任务清理仅作用于明确拥有且身份未变的标签。"""
import tempfile
import unittest
from pathlib import Path
from browser_navigation import Navigator, Page, Checkpoint
from tab_ownership import TabOwnership


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TabOwnership(Path(self.temp.name) / "owned.json")
        self.search = Page(1, "s", "search", "搜一搜")
        self.article = Page(1, "a", "article", "正文", "甲")

    def navigator(self, pages, *, close_works=True):
        cursor = [0]
        def next_tab():
            cursor[0] = (cursor[0] + 1) % len(pages)
        def close():
            if close_works:
                pages.pop(cursor[0])
                cursor[0] %= len(pages)
        return Navigator(lambda: pages[cursor[0]], next_tab, close, lambda: None)

    def test_persisted_owned_page_closed_user_page_preserved(self):
        user = Page(1, "u", "article", "原有文章", "乙")
        self.store.remember("session", self.article)
        store = TabOwnership(self.store.path)
        pages = [self.search, user, self.article]
        self.assertEqual(store.cleanup("session", self.navigator(pages)), 1)
        self.assertEqual(pages, [self.search, user])
        self.assertEqual(store.records(), [])

    def test_restarted_browser_or_changed_page_not_closed(self):
        self.store.remember("old", self.article)
        pages = [self.search, self.article]
        self.assertEqual(self.store.cleanup("new", self.navigator(pages)), 0)
        for page in (Page(1, "a", "article", "新正文", "甲"), Page(1, "a", "unknown", "正文")):
            pages = [self.search, page]
            self.assertEqual(self.store.cleanup("old", self.navigator(pages)), 0)
            self.assertEqual(len(pages), 2)

    def test_failed_close_preserves_record(self):
        self.store.remember("session", self.article)
        with self.assertRaisesRegex(RuntimeError, "关闭未生效"):
            self.store.cleanup("session", self.navigator([self.search, self.article], close_works=False))
        self.assertEqual(len(self.store.records()), 1)

    def test_existing_tab_never_registered(self):
        profile = Page(1, "p", "profile", "甲", "甲")
        pages = [self.article, self.search, profile]
        nav = self.navigator(pages)
        nav.on_created = lambda p: self.store.remember("session", p)
        nav.track_created(Checkpoint(profile, tuple(pages)))
        self.assertEqual(self.store.records(), [])
        nav.track_created(Checkpoint(profile, (profile, self.search)))
        self.assertEqual(len(self.store.records()), 1)

    def test_search_is_never_owned(self):
        self.store.remember("session", self.search)
        self.assertEqual(self.store.records(), [])

    def test_profile_reuse_transfers_only_existing_ownership(self):
        old = Page(1, "p", "profile", "甲", "甲")
        new = Page(1, "q", "profile", "乙", "乙")
        self.assertFalse(self.store.reuse("session", old, new))
        self.store.remember("session", old)
        pages = [new, self.search]
        nav = self.navigator(pages)
        nav.on_reused = lambda a, b: self.store.reuse("session", a, b)
        nav.on_closed = lambda p: self.store.forget("session", p)
        before = Checkpoint(self.search, (self.search, old), "乙")
        nav.track_created(before)
        self.assertEqual(self.store.records()[0]["page"]["key"], "q")
        self.assertEqual(nav.restore(before), "reused_profile_tab")
        self.assertEqual(pages, [self.search])
        self.assertEqual(self.store.records(), [])

    def test_user_profile_reuse_returns_without_closing(self):
        old = Page(1, "p", "profile", "甲", "甲")
        new = Page(1, "q", "profile", "乙", "乙")
        pages = [new, self.search]
        nav = self.navigator(pages)
        self.assertEqual(nav.restore(Checkpoint(self.search, (self.search, old), "乙")), "reused_profile_tab")
        self.assertEqual(len(pages), 2)

