"""网络统计只在文章身份完整、数值明确且缓存新鲜时使用。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import article_ingest
import network_metrics as nm
import wechat_capture_addon as addon

URL = "https://mp.weixin.qq.com/s/test"
HTML = '''<h1 id="activity-name">测试标题</h1><span id="js_name">测试账号</span>
<span id="publish_time">2026-09-14 10:00</span><div id="js_content">完整正文</div>
<script>var stats = {share_count: 0};</script>'''
PICTURE = '''<meta property="og:title" content="图片标题">
<div id="js_article" class="share_content_page"></div>
<script>window.cgiDataNew = {item_show_type: '8', nick_name: '测试账号',
create_time: '2026-09-14 10:00', content_noencode: '图片正文', share_count: 4};</script>'''


class NetworkMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.record = nm.build_snapshot(URL, HTML, 1000)
        self.page = {**self.record, "network_article_id": self.record["article_id"]}

    def test_identity_discards_auth_and_distinguishes_articles(self):
        url = "https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1"
        identity = nm.article_identity(url)
        self.assertEqual(identity, nm.article_identity(url + "&key=SECRET&uin=SECRET"))
        self.assertRegex(identity, r"^article:[a-f0-9]{64}$")
        self.assertNotEqual(identity, nm.article_identity(url.replace("idx=1", "idx=2")))
        for bad in (url + "&mid=123", url.replace("https", "http"),
                    url.replace("mp.weixin.qq.com", "evil.example"),
                    url.replace("mp.weixin.qq.com", "user@mp.weixin.qq.com")):
            self.assertIsNone(nm.article_identity(bad))

    def test_both_article_types_and_zero(self):
        self.assertEqual(self.record["share_count"], 0)
        self.assertEqual(nm.build_snapshot(URL, PICTURE, 1000)["share_count"], 4)

    def test_metric_conflict_and_invalid_values_rejected(self):
        for token in ("-1", "null", "true", "1.5", "'4万'", "0, share_count: 1"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                nm.build_snapshot(URL, HTML.replace("share_count: 0", "share_count: " + token), 1000)
        valid = HTML.replace("share_count: 0", 'share_count: 0, "share_count": "0"')
        self.assertEqual(nm.build_snapshot(URL, valid, 1000)["share_count"], 0)

    def test_body_text_is_not_a_metric(self):
        document = HTML.replace('<script>var stats = {share_count: 0};</script>', '')
        document = document.replace("完整正文", "share_count: 888")
        with self.assertRaises(ValueError):
            nm.build_snapshot(URL, document, 1000)

    def test_redirect_identity_is_opt_in(self):
        response = Mock(text=HTML, url="https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&key=SECRET")
        with patch.object(article_ingest.requests, "get", return_value=response):
            self.assertNotIn("network_article_id", article_ingest.parse_page(URL))
            page = article_ingest.parse_page(URL, include_network_identity=True)
        self.assertEqual(page["network_article_id"], nm.article_identity(response.url))
        self.assertNotIn("SECRET", json.dumps(page))

    def test_short_page_public_identifiers_without_redirect(self):
        script = "<script>window.biz = '' || 'abc'; window.mid = '' || '' || '123'; window.idx = '1';</script>"
        self.assertEqual(nm.page_article_identity(URL, HTML + script),
                         nm.article_identity("https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1"))
        for invalid in (script + script, script.replace("'123'", "getMid()"), script.replace("'1'", "'x'")):
            self.assertEqual(nm.page_article_identity(URL, HTML + invalid), nm.article_identity(URL))

    def test_standard_page_repeated_constants_must_agree(self):
        script = "<script>var biz = 'abc' || ''; var mid = '123'; var idx = '1'; var mid = '' || '123';</script>"
        self.assertEqual(nm.page_article_identity(URL, HTML + script),
                         nm.article_identity("https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1"))
        self.assertEqual(nm.page_article_identity(URL, HTML + script.replace("'' || '123'", "'999'")), nm.article_identity(URL))

    def test_cache_hit_and_no_raw_content(self):
        nm.save_snapshot(self.directory, self.record)
        self.assertEqual(nm.read_snapshot(str(self.directory), self.page, now=1001)[1], "hit")
        stored = next(self.directory.iterdir()).read_text(encoding="utf-8")
        self.assertNotIn("完整正文", stored)
        self.assertNotIn("https://", stored)

    def test_invalid_cache_never_used(self):
        cases = [{"captured_at": 879}, {"captured_at": 1001}, {"captured_at": float("nan")},
                 {"share_count": True}, {"share_count": -1}, {"title": "其他文章"},
                 {"account_name": "其他账号"}, {"publish_time": "2020-01-01"}, {"valid": False}]
        for changes in cases:
            with self.subTest(changes=changes):
                nm.snapshot_path(self.directory, self.record["article_id"]).write_text(
                    json.dumps({**self.record, **changes}), encoding="utf-8")
                self.assertIsNone(nm.read_snapshot(str(self.directory), self.page, now=1000)[0])
        nm.snapshot_path(self.directory, self.record["article_id"]).write_text("broken", encoding="utf-8")
        self.assertIsNone(nm.read_snapshot(str(self.directory), self.page, now=1000)[0])

    def test_late_success_cannot_replace_new_failure(self):
        nm.save_snapshot(self.directory, {**self.record, "captured_at": 1002, "valid": False})
        nm.save_snapshot(self.directory, self.record)
        self.assertIsNone(nm.read_snapshot(str(self.directory), self.page, now=1003)[0])

    def test_long_response_matches_short_link_and_failure_invalidates_both(self):
        long_url = "https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1"
        record = nm.build_snapshot(long_url, '<meta property="og:url" content="' + URL + '">' + HTML, 1000)
        nm.save_snapshot(self.directory, record)
        self.assertEqual(nm.read_snapshot(str(self.directory), self.page, now=1001)[1], "hit")
        nm.save_snapshot(self.directory, {"schema": 1, "valid": False,
                         "article_id": record["article_id"], "captured_at": 1002})
        nm.save_snapshot(self.directory, record)
        self.assertIsNone(nm.read_snapshot(str(self.directory), self.page, now=1003)[0])

    def test_addon_failure_invalidates_previous_success(self):
        flow = SimpleNamespace(request=SimpleNamespace(url=URL, timestamp_start=1000),
                               response=Mock(status_code=200, headers={"content-type": "text/html"}))
        flow.response.get_text.return_value = HTML
        with patch.dict("os.environ", {"WECHAT_CAPTURE_CACHE_DIR": str(self.directory)}):
            addon.response(flow)
            self.assertEqual(nm.read_snapshot(str(self.directory), self.page, now=1000)[1], "hit")
            flow.request.timestamp_start = 1001
            addon.error(flow)
            self.assertIsNone(nm.read_snapshot(str(self.directory), self.page, now=1002)[0])


if __name__ == "__main__":
    unittest.main()

