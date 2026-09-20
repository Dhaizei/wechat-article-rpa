"""缓存减少按键，但不能绕过关闭前的新鲜页面校验。"""
import unittest

from browser_navigation import Navigator, Page, Checkpoint, InventoryCache, tab_signature


class NavigationCacheTests(unittest.TestCase):
    def setUp(self):
        self.profile = Page(1, "p", "profile", "甲", "甲")
        self.search = Page(1, "s", "search", "搜一搜")
        self.pages = [self.profile, self.search]
        self.cursor = 0
        self.switches = 0
        self.signature = (("p", "甲"), ("s", "搜一搜"))
        self.cache = InventoryCache()

    def navigator(self):
        def next_tab():
            self.switches += 1
            self.cursor = (self.cursor + 1) % len(self.pages)
        return Navigator(lambda: self.pages[self.cursor], next_tab,
                         lambda: self.fail("不得误关用户页面"), lambda: None,
                         signature=lambda: self.signature, cache=self.cache)

    def test_repeated_checkpoint_across_instances_needs_no_switch(self):
        self.navigator().inventory()
        self.switches = 0
        self.assertEqual(self.navigator().checkpoint().origin, self.profile)
        self.assertEqual(self.switches, 0)

    def test_changed_tab_list_invalidates_cache(self):
        self.navigator().inventory()
        self.pages.append(Page(1, "u", "unknown", "用户页面"))
        self.signature += (("u", "用户页面"),)
        self.switches = 0
        self.assertEqual(len(self.navigator().inventory()), 3)
        self.assertEqual(self.switches, 3)

    def test_same_tab_title_with_changed_document_invalidates_cache(self):
        self.navigator().inventory()
        self.pages[0] = Page(1, "new", "profile", "甲", "甲")
        self.switches = 0
        self.assertEqual(self.navigator().inventory()[0].key, "new")
        self.assertEqual(self.switches, 2)

    def test_restore_rechecks_even_with_matching_signature(self):
        article = Page(1, "a", "article", "正文", "甲")
        self.pages.append(article)
        self.signature += (("a", "正文"),)
        self.cursor = 2
        nav = self.navigator()
        nav.inventory()
        self.switches = 0
        self.assertEqual(nav.restore(Checkpoint(self.profile, tuple(self.pages))), "existing_tab")
        self.assertGreaterEqual(self.switches, 3)

    def test_missing_signature_never_reuses_cache(self):
        self.navigator().inventory()
        self.signature = ()
        self.switches = 0
        self.navigator().inventory()
        self.assertEqual(self.switches, 2)

    def test_probe_requires_complete_unique_tab_ids(self):
        self.assertEqual(tab_signature({"ok": True, "tabs": [{"id": [], "name": "甲"}]}), ())
        self.assertEqual(tab_signature({"ok": True, "tabs": [{"id": [1], "name": "甲"}] * 2}), ())
        self.assertEqual(tab_signature({"ok": True, "tabs": [{"id": [1], "name": "甲"}]}), (((1,), "甲"),))

