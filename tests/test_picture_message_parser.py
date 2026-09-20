"""图片消息解析：不把摘要、引用账号或验证页当作完整文章。"""
import unittest
from unittest.mock import Mock, patch
from bs4 import BeautifulSoup
import article_ingest as ingest


class PictureMessageParserTests(unittest.TestCase):
    def fixture(self, extra="", content=r"正文\x0a第二行\x26amp;详情"):
        return '''<meta property="og:title" content="图片消息标题">
        <div id="js_article" class="share_content_page"></div>
        <script>window.cgiDataNew = {item_show_type: '8', nick_name: '测试公众号',
        create_time: '2026-09-12 18:00', content_noencode: '%s', %s};</script>
        <script>window.cgiDataNew = window.cgiDataNew || {};</script>''' % (content, extra)

    def test_reads_complete_picture_text_and_identity(self):
        response = Mock(text=self.fixture())
        with patch.object(ingest.requests, "get", return_value=response):
            result = ingest.parse_page("https://mp.weixin.qq.com/s/test")
        self.assertEqual(result, {"title": "图片消息标题", "content": "正文\n第二行&详情",
                                  "account_name": "测试公众号", "publish_time": "2026-09-12 18:00"})

    def test_ambiguous_account_is_rejected(self):
        soup = BeautifulSoup(self.fixture("nick_name: '引用公众号'"), "html.parser")
        self.assertEqual(ingest.extract_picture_message(soup), {})

    def test_description_cannot_replace_missing_body(self):
        soup = BeautifulSoup(self.fixture(content="") + '<meta name="description" content="摘要">', "html.parser")
        self.assertEqual(ingest.extract_picture_message(soup), {})

    def test_verification_page_is_rejected(self):
        response = Mock(text='<h2>环境异常</h2><meta property="og:title" content="验证">')
        with patch.object(ingest.requests, "get", return_value=response):
            with self.assertRaisesRegex(ValueError, "没有标题"):
                ingest.parse_page("https://mp.weixin.qq.com/s/test")


if __name__ == "__main__":
    unittest.main()

