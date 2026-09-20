"""页面导航回归：误关闭风险与日期/文章对应关系必须独立验证。"""

import unittest
from browser_navigation import Page, Checkpoint, Navigator, return_action, page_from_probe, feed_from_probe, picture_share_count


class NavigationTests(unittest.TestCase):
    def test_picture_share_excludes_recommendations_and_wrong_account(self):
        data = {"documents": [{"name": "标题", "rows": [{"text": t} for t in
                ["评论分享999", "甲", "赞 18", "分享 0", "在看 3", "更多相关贴图", "分享 999"]]}]}
        self.assertEqual(picture_share_count(data, "甲", "标题"), 0)
        self.assertIsNone(picture_share_count(data, "乙", "标题"))
        self.assertIsNone(picture_share_count(data, "甲", "其他标题"))

    def setUp(self):
        self.search = Page(1, "s", "search", "搜一搜")
        self.profile = Page(1, "p", "profile", "甲", "甲")
        self.article = Page(1, "a", "article", "一篇文章", "甲")
        self.before = Checkpoint(self.profile, (self.profile, self.search))

    def test_new_tab_requires_all_baseline_pages(self):
        self.assertEqual(return_action(self.before, self.article,
                         (self.search, self.profile, self.article)), "new_tab")
        self.assertEqual(return_action(self.before, self.article,
                         (self.article,)), "unknown")

    def test_replaced_profile_uses_back(self):
        self.assertEqual(return_action(self.before, self.article,
                         (self.search, self.article)), "same_tab")

    def test_preexisting_article_is_not_closed(self):
        before = Checkpoint(self.profile, (self.search, self.profile, self.article))
        self.assertEqual(return_action(before, self.article, before.pages), "existing_tab")

    def test_profile_cleanup_preserves_preexisting_pages(self):
        before = Checkpoint(self.search, (self.search, self.article), "甲")
        self.assertEqual(return_action(before, self.profile,
                         (self.search, self.article, self.profile)), "new_tab")
        wrong = Page(1, "w", "profile", "乙", "乙")
        self.assertEqual(return_action(before, wrong,
                         (self.search, self.article, wrong)), "unknown")

    def test_wrong_account_and_unknown_are_preserved(self):
        wrong = Page(1, "a", "article", "一篇文章", "乙")
        self.assertEqual(return_action(self.before, wrong,
                         (self.profile, self.search, wrong)), "unknown")
        self.assertEqual(return_action(self.before, self.profile, self.before.pages), "unchanged")

    def test_restore_chooses_profile_even_when_close_selects_search(self):
        pages = [self.search, self.profile, self.article]
        cursor = [2]
        def next_tab():
            cursor[0] = (cursor[0] + 1) % len(pages)
        def close_tab():
            self.assertEqual(pages.pop(cursor[0]), self.article)
            cursor[0] = 0
        nav = Navigator(lambda: pages[cursor[0]], next_tab, close_tab,
                        lambda: self.fail("新标签不得后退"))
        self.assertEqual(nav.restore(self.before), "new_tab")
        self.assertEqual(pages[cursor[0]], self.profile)
        self.assertEqual(pages, [self.search, self.profile])

    def test_duplicate_profile_name_does_not_replace_origin_identity(self):
        duplicate = Page(1, "other-profile", "profile", "甲", "甲")
        pages = [duplicate, self.search, self.profile, self.article]
        cursor = [3]
        before = Checkpoint(self.profile, tuple(pages[:-1]))
        def next_tab():
            cursor[0] = (cursor[0] + 1) % len(pages)
        def close_tab():
            pages.pop(cursor[0])
            cursor[0] = 0
        nav = Navigator(lambda: pages[cursor[0]], next_tab, close_tab, lambda: None)
        self.assertEqual(nav.restore(before), "new_tab")
        self.assertEqual(pages[cursor[0]].key, self.profile.key)

    def test_unknown_transition_never_closes(self):
        nav = Navigator(lambda: self.article, lambda: None,
                        lambda: self.fail("不得关闭"), lambda: self.fail("不得后退"))
        with self.assertRaises(RuntimeError):
            nav.restore(self.before)

    def test_unknown_article_returns_to_exact_origin_without_closing(self):
        unknown = Page(1, "u", "unknown", "视频文章")
        pages = [unknown, self.search, self.profile]
        cursor = [0]
        def next_tab():
            cursor[0] = (cursor[0] + 1) % len(pages)
        def forbidden():
            self.fail("未知页面不得关闭或后退")
        nav = Navigator(lambda: pages[cursor[0]], next_tab, forbidden, forbidden)
        self.assertEqual(nav.restore(self.before), "preserved_unknown")
        self.assertEqual(pages[cursor[0]], self.profile)
        self.assertEqual(len(pages), 3)

    def test_find_search_from_unknown_tab_and_preserve_order(self):
        pages = [Page(1, "u", "unknown", "加载失败"), self.profile, self.search]
        cursor = [0]
        def next_tab():
            cursor[0] = (cursor[0] + 1) % len(pages)
        nav = Navigator(lambda: pages[cursor[0]], next_tab, lambda: None, lambda: None)
        self.assertTrue(nav.select(lambda p: p.role == "search"))
        self.assertEqual(cursor[0], 2)
        self.assertFalse(nav.select(lambda p: p.role == "missing"))
        self.assertEqual(cursor[0], 2)

    def test_missing_document_never_sends_keys(self):
        def forbidden():
            self.fail("无法读取文档时不盲目切换")
        nav = Navigator(lambda: Page(1, "", "unknown", ""), forbidden, forbidden, forbidden)
        self.assertFalse(nav.select(lambda p: p.role == "search"))

    def test_same_tab_back_restores_profile_without_close(self):
        pages = [self.search, self.article]
        cursor = [1]
        def next_tab():
            cursor[0] = (cursor[0] + 1) % len(pages)
        def back():
            pages[cursor[0]] = self.profile
        nav = Navigator(lambda: pages[cursor[0]], next_tab,
                        lambda: self.fail("同页跳转不得关闭"), back)
        self.assertEqual(nav.restore(self.before), "same_tab")
        self.assertEqual(pages[cursor[0]], self.profile)

    def test_no_navigation_performs_no_input(self):
        def unexpected():
            self.fail("未跳转不得输入")
        nav = Navigator(lambda: self.profile, unexpected, unexpected, unexpected)
        self.assertEqual(nav.restore(self.before), "unchanged")

    def test_profile_requires_identity_and_structure(self):
        data = {"hwnd": 1, "documents": [{"id": [1], "name": "甲", "rows": [
            {"text": "全部"}, {"text": "文章"}, {"text": "私信"}]}]}
        self.assertEqual(page_from_probe(data, "甲").role, "profile")
        self.assertNotEqual(page_from_probe(data, "乙").role, "profile")
        data["documents"][0]["rows"] = [{"text": "甲"}]
        self.assertNotEqual(page_from_probe(data, "甲").role, "profile")

    def test_feed_pairs_full_title_and_metrics_skips_offscreen(self):
        def row(text, y, offscreen=False):
            return {"text": text, "rect": [200, y, 600, y+20], "offscreen": offscreen}
        data = {"hwnd": 1, "documents": [{"id": [1], "name": "甲", "rows": [
            row("全部", 60), row("文章", 80), row("私信", 100), row("今天", 150),
            row("完整标题不会被省略号截断", 400), row("阅读3.3万 赞244", 450),
            row("昨天", 900), row("屏幕外的文章", 1100, True), row("阅读1万 赞2", 1150, True),
        ]}]}
        feed = feed_from_probe(data, (0, 0, 1000, 1000), "甲")
        self.assertEqual(len(feed["articles"]), 1)
        self.assertEqual(feed["time_labels"][0]["text"], "今天")
        self.assertEqual(feed["articles"][0]["screen_point"], (400, 410))
        self.assertEqual(feed["articles"][0]["list_read_count"], 33000)


if __name__ == "__main__":
    unittest.main()

