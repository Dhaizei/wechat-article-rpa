"""正常新增标签零遍历；证据变化时不误关页面。"""
import unittest
from browser_navigation import Page, Checkpoint, Navigator, selected_tab, tab_signature


class DirectNavigationTests(unittest.TestCase):
    def setUp(self):
        self.search = Page(1, "s", "search", "搜一搜")
        self.profile = Page(1, "p", "profile", "甲", "甲")
        self.article = Page(1, "a", "article", "文章", "甲")
        self.tabs = (((1,), "搜一搜"), ((2,), "甲"), ((3,), "文章"))
        self.current = self.article
        self.active = (3,)
        self.closed = 0
        self.before = Checkpoint(self.profile, (self.profile, self.search), "", self.tabs[:2], (2,))

    def navigator(self, fail_close=False):
        def close():
            self.closed += 1
            if not fail_close:
                self.tabs = self.tabs[:2]
                self.current, self.active = self.search, (1,)
        def select(identity):
            self.assertEqual(identity, (2,))
            self.current, self.active = self.profile, identity
            return True
        return Navigator(lambda: self.current, lambda: self.fail("正常路径不得遍历"), close,
                         lambda: self.fail("新标签不得后退"), signature=lambda: self.tabs,
                         selected=lambda: self.active, select_tab=select)

    def test_open_and_close_without_traversal(self):
        nav = self.navigator()
        nav.track_created(self.before)
        self.assertEqual(nav.cache.pages[0], self.article)
        self.assertEqual(nav.restore(self.before), "new_tab")
        self.assertEqual(self.closed, 1)
        self.assertEqual(self.current, self.profile)
        self.assertEqual(nav.checkpoint().origin, self.profile)

    def test_failed_close_keeps_ownership(self):
        nav = self.navigator(fail_close=True)
        forgotten = []
        nav.on_closed = forgotten.append
        with self.assertRaisesRegex(RuntimeError, "基线不一致"):
            nav.restore(self.before)
        self.assertEqual(forgotten, [])

    def test_unknown_multiple_changes_and_missing_selected_reject_fast_path(self):
        nav = self.navigator()
        self.assertFalse(nav.added_target(self.before, Page(1, "a", "unknown", "文章")))
        self.tabs += (((4,), "用户页面"),)
        self.assertFalse(nav.added_target(self.before, self.article))
        self.tabs = self.tabs[:3]
        self.active = ()
        self.assertFalse(nav.added_target(self.before, self.article))

    def test_existing_tab_never_becomes_new(self):
        nav = self.navigator()
        before = Checkpoint(self.profile, (self.profile, self.search, self.article), "", self.tabs, (2,))
        self.assertFalse(nav.added_target(before, self.article))

    def test_profile_open_uses_same_fast_path(self):
        self.tabs = self.tabs[:2]
        self.current, self.active = self.profile, (2,)
        before = Checkpoint(self.search, (self.search,), "甲", self.tabs[:1], (1,))
        self.assertTrue(self.navigator().added_target(before, self.profile))

    def test_selection_must_be_unique(self):
        rows = [{"id": [1], "name": "甲", "selected": True},
                {"id": [2], "name": "乙", "selected": False}]
        self.assertEqual(selected_tab({"ok": True, "tabs": rows}), (1,))
        rows[1]["selected"] = True
        self.assertEqual(selected_tab({"ok": True, "tabs": rows}), ())

    def test_custom_added_tab_requires_direct_confirmation(self):
        nav = self.navigator()
        self.active = ()
        nav.custom_tabs = lambda: True
        chosen = []
        def choose(identity):
            chosen.append(identity)
            self.active = identity
            return True
        nav.select_tab = choose
        nav.track_created(self.before)
        self.assertEqual(chosen, [(3,)])
        self.assertEqual(nav.cache.pages[0], self.article)

    def test_custom_selection_identity_mismatch_aborts(self):
        nav = self.navigator()
        self.active = ()
        nav.custom_tabs = lambda: True
        def choose(identity):
            self.current = self.search
            return True
        nav.select_tab = choose
        with self.assertRaisesRegex(RuntimeError, "页面身份不一致"):
            nav.track_created(self.before)
        self.assertEqual(self.closed, 0)

    def test_custom_missing_origin_still_falls_back(self):
        nav = self.navigator()
        nav.custom_tabs = lambda: True
        self.active = ()
        before = Checkpoint(self.profile, self.before.pages, "", self.before.tabs)
        self.assertFalse(nav.confirm_added(before, self.article))

    def test_custom_signature_needs_explicit_probe_mode_and_unique_ids(self):
        data = dict(ok=True, tabs=[], custom_tabs=[dict(id=[1], name="")])
        self.assertEqual(tab_signature(data), ())
        data["tab_probe_mode"] = "custom-unverified"
        self.assertEqual(tab_signature(data), (((1,), "<custom-tab>"),))
        self.assertEqual(selected_tab(data), ())
        data["custom_tabs"].append(dict(id=[1], name=""))
        self.assertEqual(tab_signature(data), ())

