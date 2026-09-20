"""离线验证网络命中与界面回退，不操作真实微信或数据库。"""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import wechat_visual_rpa as rpa


class NetworkCollectionTests(unittest.TestCase):
    def run_collection(self, hit=True, changed=False, download_failed=False, strict=False, old_article=False):
        page = dict(title="测试文章", account_name="测试账号", publish_time="2026-09-14 10:00", content="正文")
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"WECHAT_CAPTURE_CACHE_DIR": directory, "WECHAT_CAPTURE_MODE": "strict" if strict else "prefer"}))
            for name in ("activate_window", "log_event", "append_local_exports"):
                stack.enter_context(patch.object(rpa, name))
            stack.enter_context(patch.object(rpa, "user32"))
            stack.enter_context(patch.object(rpa, "find_article_window", return_value=(0, rpa.Rect(0, 0, 100, 100))))
            stack.enter_context(patch.object(rpa, "copy_article_url", side_effect=["https://mp.weixin.qq.com/s/a", "https://mp.weixin.qq.com/s/b" if changed else "https://mp.weixin.qq.com/s/a"]))
            stack.enter_context(patch.object(rpa, "parse_page", return_value=page, side_effect=RuntimeError("offline") if download_failed else None))
            stack.enter_context(patch.object(rpa, "load_cached_page", return_value=page))
            stack.enter_context(patch.object(rpa, "capture_window", return_value=Image.new("RGB", (100, 100))))
            stack.enter_context(patch.object(rpa.ARTICLE_EVIDENCE_OCR, "inspect", return_value={}))
            lookup = stack.enter_context(patch.object(rpa, "acquire_network_metrics", return_value=({"share_count": 4}, "hit") if hit else (None, "expired_cache")))
            probe = stack.enter_context(patch.object(rpa, "probe_page"))
            stack.enter_context(patch.object(rpa, "picture_share_count", return_value=None))
            fallback = stack.enter_context(patch.object(rpa, "extract_local_interaction_metrics", return_value=({"share_count": 7}, "template-ocr-share-only", None)))
            ingest = stack.enter_context(patch.object(rpa, "ingest", return_value={"status": "dry_run"}))
            if old_article:
                stack.enter_context(patch.object(rpa, "publish_time_matches_scan_range", return_value=False))
                if changed:
                    with self.assertRaises(rpa.ArticleMismatchError):
                        rpa.collect_open_article(None, Path(directory), False, None, None, metric_mode="share", scan_range="today")
                else:
                    result = rpa.collect_open_article(None, Path(directory), False, None, None, metric_mode="share", scan_range="today")
                    self.assertEqual(result["status"], "skipped_outside_scan_range")
                    self.assertTrue(all(result["verification"][k] for k in ("title_matched", "account_matched", "url_stable")))
                ingest.assert_not_called()
                lookup.assert_not_called()
                fallback.assert_not_called()
                return
            if strict and not hit:
                with self.assertRaisesRegex(RuntimeError, "仅抓包采集失败"):
                    rpa.collect_open_article(None, Path(directory), False, None, None, metric_mode="share", allow_vl=True)
                ingest.assert_not_called()
                fallback.assert_not_called()
                probe.assert_not_called()
                return
            if changed:
                with self.assertRaises(rpa.ArticleMismatchError):
                    rpa.collect_open_article(None, Path(directory), False, None, None, metric_mode="share", allow_vl=False)
                ingest.assert_not_called()
                return
            rpa.collect_open_article(None, Path(directory), download_failed, None, None, metric_mode="share", allow_vl=False)
            self.assertEqual(ingest.call_args.kwargs["metrics"]["share_count"], 4 if hit else 7)
            if hit:
                probe.assert_not_called()
                fallback.assert_not_called()
            else:
                fallback.assert_called_once()
            if download_failed:
                self.assertNotIn("network_article_id", lookup.call_args.args[1])

    def test_hit_bypasses_ui_metrics(self):
        self.run_collection()

    def test_miss_falls_back(self):
        self.run_collection(hit=False)

    def test_url_change_still_rejects_network_result(self):
        self.run_collection(changed=True)

    def test_download_failure_preserves_cached_page_fallback(self):
        self.run_collection(hit=False, download_failed=True)

    def test_strict_miss_never_falls_back_or_writes(self):
        self.run_collection(hit=False, strict=True)

    def test_old_article_preserves_cleanup_evidence_without_collecting_metrics(self):
        self.run_collection(old_article=True)

    def test_old_article_switched_tab_is_not_verified(self):
        self.run_collection(old_article=True, changed=True)

    def test_refresh_accepts_only_new_response(self):
        with patch.object(rpa.time, "sleep"), patch.object(rpa.time, "time", return_value=100), \
             patch.object(rpa, "activate_window"), patch.object(rpa, "press_ctrl_shift_r") as refresh, \
             patch.object(rpa, "log_event"), patch.object(rpa, "read_snapshot", side_effect=[
                 *([(None, "unavailable_cache")] * 7),
                 ({"captured_at": 99, "share_count": 1}, "hit"),
                 ({"captured_at": 101, "share_count": 2}, "hit")]):
            record, reason = rpa.acquire_network_metrics("cache", {}, 0)
        self.assertEqual(record["share_count"], 2)
        self.assertEqual(reason, "hit_after_refresh")
        refresh.assert_called_once()

    def test_refresh_timeout_is_bounded(self):
        with patch.object(rpa.time, "sleep") as sleep, patch.object(rpa, "activate_window"), \
             patch.object(rpa, "press_ctrl_shift_r"), patch.object(rpa, "log_event"), \
             patch.object(rpa, "read_snapshot", return_value=(None, "unavailable_cache")):
            record, reason = rpa.acquire_network_metrics("cache", {}, 0)
        self.assertIsNone(record)
        self.assertIn("refresh_timeout", reason)
        self.assertEqual(sleep.call_count, 36)

