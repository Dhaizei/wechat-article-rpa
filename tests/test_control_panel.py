"""控制台核心行为的离线回归测试。

这些测试不拉起真实微信，也不连接 MongoDB，适合在每次改动控制台时快速执行。
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from http.server import ThreadingHTTPServer

import rpa_control_panel as panel


def ready_preflight() -> dict[str, object]:
    """构造一份可启动的桌面检查结果，避免测试依赖当前电脑窗口状态。"""
    return {
        "ready": True,
        "wechat": {"ok": True, "message": "已检测到微信主窗口"},
        "search": {"ok": True, "message": "已检测到搜一搜窗口"},
        "desktop": {},
    }


def blocked_preflight() -> dict[str, object]:
    """构造一份被拦截的检查结果，用来确认失败原因也会进入任务历史。"""
    return {
        "ready": False,
        "wechat": {"ok": True, "message": "已检测到微信主窗口"},
        "search": {"ok": False, "message": "未检测到搜一搜窗口"},
        "desktop": {},
    }


class ControlPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_run_status_reflects_business_failures_even_with_zero_exit_code(self) -> None:
        """子进程正常退出不等于采集成功，全部账号失败时必须显示异常退出。"""
        status, message = panel.determine_final_run_status(
            exit_code=0,
            manually_stopped=False,
            summary={"accounts_failed": 69, "accounts_succeeded": 0, "accounts_no_updates": 0},
        )

        self.assertEqual(status, "failed")
        self.assertIn("全部公众号", message)

    def test_run_status_marks_mixed_results_as_partial(self) -> None:
        status, _message = panel.determine_final_run_status(
            exit_code=0,
            manually_stopped=False,
            summary={"accounts_failed": 2, "accounts_succeeded": 67, "accounts_no_updates": 0},
        )

        self.assertEqual(status, "partial")

    def test_history_marks_abandoned_running_record_as_interrupted(self) -> None:
        history_path = self.temp_path / "run_history.json"
        history = panel.RunHistory(history_path)
        history.create({"run_id": "running-one", "status": "running"})

        reloaded = panel.RunHistory(history_path)
        record = reloaded.get("running-one")

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["status"], "interrupted")
        self.assertIn("控制台重启", record["result_message"])

    def test_blocked_preflight_creates_traceable_run_record(self) -> None:
        state = panel.ControlState()
        state.history = panel.RunHistory(self.temp_path / "run_history.json")
        with patch.object(panel, "collect_preflight", return_value=blocked_preflight()), patch.object(
            panel, "PANEL_LOG_PATH", self.temp_path / "control-panel.log"
        ):
            started, message = state.start_job(
                "manual", max_articles=5, scan_range="today", metrics="share"
            )

        self.assertFalse(started)
        self.assertIn("搜一搜", message)
        record = state.history.recent(1)[0]
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["parameters"]["scan_range"], "today")
        self.assertEqual(record["parameters"]["metrics"], "share")

    def test_invalid_run_options_are_rejected_before_process_launch(self) -> None:
        state = panel.ControlState()
        state.history = panel.RunHistory(self.temp_path / "run_history.json")
        with patch.object(panel, "collect_preflight", return_value=ready_preflight()), patch.object(
            panel.subprocess, "Popen"
        ) as popen:
            with self.assertRaises(ValueError):
                state.start_job("manual", max_articles=0, scan_range="today", metrics="share")

        popen.assert_not_called()

    def test_preflight_recovery_never_competes_with_an_active_collection(self) -> None:
        """采集进行中不能因重新检测而抢占微信窗口，必须明确提示后等待任务结束。"""

        class RunningState:
            @staticmethod
            def running() -> bool:
                return True

        with patch.object(panel, "collect_preflight", return_value=blocked_preflight()), patch.object(
            panel, "STATE", RunningState()
        ):
            result = panel.recover_sogou_preflight()

        self.assertFalse(result["recovery"]["attempted"])
        self.assertFalse(result["recovery"]["succeeded"])
        self.assertIn("采集", result["recovery"]["message"])

    def test_manual_scan_range_is_preserved_in_collector_command(self) -> None:
        """Protect against a manual 'today' run being replaced by schedule defaults."""
        output_dir = self.temp_path / "manual-today"
        command = panel.build_collector_command(
            output_dir,
            {"max_articles": 7, "scan_range": "today", "metrics": "share"},
        )

        self.assertEqual(command[command.index("--scan-range") + 1], "today")
        self.assertEqual(command[command.index("--metrics") + 1], "share")
        self.assertEqual(command[command.index("--max-articles") + 1], "7")
        self.assertNotIn("--local-only", command)

    def test_portable_start_script_does_not_disable_vl_fallback(self) -> None:
        """便携启动脚本必须允许 .env 中配置的 Qwen-VL 在本地 OCR 失败后兜底。"""
        script = (panel.RPA_DIR / "start-rpa.ps1").read_text(encoding="utf-8")
        self.assertNotIn('"--local-only"', script)

    def test_schedule_ranges_keep_morning_and_evening_rules(self) -> None:
        """早晚任务必须各自携带范围，不能再被全局 scan_range 覆盖。"""
        ranges = panel.validate_schedule_ranges(
            {"08:00": "today_yesterday", "22:00": "today"},
            ["08:00", "22:00"],
            "today_yesterday",
        )
        self.assertEqual(ranges, {"08:00": "today_yesterday", "22:00": "today"})
        self.assertEqual(
            panel.validate_schedule_ranges(None, ["08:00", "22:00"], "today_yesterday"),
            {"08:00": "today_yesterday", "22:00": "today"},
        )

    def test_next_run_text_displays_the_range_for_the_next_time_slot(self) -> None:
        """下次任务提示必须说明实际范围，避免用户误以为早晚任务完全相同。"""

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return cls(2026, 8, 2, 7, 30)

        config = {
            "enabled": True,
            "times": ["08:00", "22:00"],
            "scan_range": "today_yesterday",
            "schedule_ranges": {"08:00": "today_yesterday", "22:00": "today"},
        }
        with patch.object(panel, "datetime", FixedDateTime):
            self.assertEqual(panel.next_run_text(config), "2026-08-02 08:00 · 今天和昨天")

    def test_static_assets_are_revalidated_after_page_refresh(self) -> None:
        """避免浏览器缓存旧版管理页，导致已发布的前端优化看起来未生效。"""
        server = ThreadingHTTPServer(("127.0.0.1", 0), panel.ControlHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/accounts.js", timeout=3) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-cache, max-age=0, must-revalidate")
        finally:
            server.shutdown()
            worker.join(timeout=3)
            server.server_close()

    def test_save_config_persists_schedule_ranges(self) -> None:
        state = panel.ControlState()
        config_path = self.temp_path / "control_panel.json"
        with patch.object(panel, "CONFIG_PATH", config_path), patch.object(
            panel, "PANEL_LOG_PATH", self.temp_path / "control-panel.log"
        ):
            config = state.save_config(
                {
                    "enabled": True,
                    "times": ["08:00", "22:00"],
                    "max_articles": 20,
                    "scan_range": "today_yesterday",
                    "schedule_ranges": {"08:00": "today_yesterday", "22:00": "today"},
                    "metrics": "share",
                }
            )

        self.assertEqual(config["schedule_ranges"]["08:00"], "today_yesterday")
        self.assertEqual(config["schedule_ranges"]["22:00"], "today")

    def test_no_update_keeps_range_diagnostics_for_the_console(self) -> None:
        state = panel.ControlState()
        state._record_progress_event(
            {
                "event": "account_collection_finished",
                "account": "测试公众号",
                "detected_articles": 0,
                "stop_reason": "遇到更早时间边界：7月20日",
                "scan": {
                    "range": "today",
                    "observed_cards": 8,
                    "outside_range_cards": 8,
                    "ungrouped_cards": 0,
                    "promotion_cards": 2,
                },
            }
        )

        summary = state._run_summary_locked()
        self.assertEqual(summary["accounts_no_updates"], 1)
        self.assertEqual(summary["no_update_samples"][0]["range"], "today")
        self.assertEqual(summary["no_update_samples"][0]["outside_range_cards"], 8)
        self.assertEqual(summary["no_update_samples"][0]["promotion_cards"], 2)

    def test_partial_metrics_are_kept_out_of_failure_count(self) -> None:
        """只确认转发数的文章应被标记为待补齐，而不是把公众号判定为失败。"""
        state = panel.ControlState()
        state._record_progress_event(
            {
                "event": "article_metrics_partial",
                "retained_metrics": {"share_count": 106},
                "unavailable_metrics": ["favorite_count", "comment_count"],
            }
        )

        summary = state._run_summary_locked()
        self.assertEqual(summary["articles_partial_metrics"], 1)
        self.assertEqual(summary["accounts_failed"], 0)

    def test_tab_cleanup_warning_is_traceable_without_counting_collection_failed(self) -> None:
        """保护搜一搜页而停止清理标签时，要提示人工复位，但不能误算成采集失败。"""
        state = panel.ControlState()
        state._record_progress_event(
            {
                "event": "article_tab_cleanup_failed",
                "account": "测试公众号",
                "title": "测试文章",
                "error": "清理浏览器标签后没有回到搜一搜页面",
            }
        )

        summary = state._run_summary_locked()
        self.assertEqual(summary["article_tab_cleanup_warnings"], 1)
        self.assertEqual(summary["accounts_failed"], 0)
        self.assertEqual(summary["tab_cleanup_samples"][0]["account"], "测试公众号")
        self.assertEqual(state.progress["phase"], "浏览器标签待确认，后续账号可能受影响")

    def test_title_evidence_warning_is_visible_without_marking_collection_failed(self) -> None:
        """标题 OCR 辅助证据不足时，保留提示但不能否定 URL 与账号已通过的文章。"""
        state = panel.ControlState()
        state._record_progress_event({"event": "article_title_evidence_warning"})

        summary = state._run_summary_locked()
        self.assertEqual(summary["articles_title_evidence_warnings"], 1)
        self.assertEqual(summary["accounts_failed"], 0)

    def test_process_event_summary_keeps_no_update_diagnostics_readable(self) -> None:
        """控制台应把冗长 JSON 结果压缩为可直接判断是否需要补采的摘要。"""
        message = panel.format_process_event_message(
            {
                "event": "account_collection_finished",
                "account": "测试公众号",
                "detected_articles": 0,
                "stop_reason": "遇到更早时间边界：7月20日",
                "scan": {"observed_cards": 8, "outside_range_cards": 6},
            }
        )

        self.assertEqual(
            message,
            "公众号无更新：测试公众号；已检查 8 张卡片，范围外 6 张；原因：遇到更早时间边界：7月20日。",
        )

    def test_process_event_summary_retains_failure_category_and_reason(self) -> None:
        """失败日志必须同时保留分类、原因和用户可直接执行的恢复步骤。"""
        message = panel.format_process_event_message(
            {
                "event": "account_collection_failed",
                "account": "测试公众号",
                "category": "account_filter",
                "error": "未检测到文章窗口",
            }
        )

        self.assertEqual(
            message,
            "公众号采集失败：测试公众号；类别=account_filter；原因：未检测到文章窗口。"
            "建议：确认“搜一搜”已选中“账号”和二级“公众号”，保持窗口可见后重试。",
        )

    def test_failure_sample_persists_recovery_hint(self) -> None:
        """任务历史应保存建议，避免刷新控制台后只剩难以处理的错误码。"""
        state = panel.ControlState()
        state._record_progress_event(
            {
                "event": "account_collection_failed",
                "account": "测试公众号",
                "category": "account_filter",
                "error": "二级公众号筛选未确认选中：下划线未显示",
            }
        )

        summary = state._run_summary_locked()
        self.assertEqual(summary["accounts_failed"], 1)
        self.assertEqual(
            summary["failure_samples"][0]["recovery_hint"],
            "确认“搜一搜”已选中“账号”和二级“公众号”，保持窗口可见后重试。",
        )

    def test_process_event_summary_marks_partial_metrics_for_follow_up(self) -> None:
        """低置信互动数据被保留时，控制台应提示补采而不是展示原始 JSON。"""
        self.assertEqual(
            panel.format_process_event_message({"event": "article_metrics_partial"}),
            "文章互动指标待补齐：已保留可识别的指标，后续可重试补采。",
        )

    def test_process_event_summary_makes_tab_cleanup_warning_actionable(self) -> None:
        """标签清理保护动作必须在控制台中给出恢复步骤，而不是暴露原始 JSON。"""
        message = panel.format_process_event_message(
            {
                "event": "article_tab_cleanup_failed",
                "account": "测试公众号",
                "title": "测试文章",
                "error": "首个标签不是搜一搜页面，为保护搜索页拒绝自动关闭任何标签",
            }
        )

        self.assertEqual(
            message,
            "浏览器标签待确认：测试公众号；为保护“搜一搜”页，系统未继续关闭标签；"
            "原因：首个标签不是搜一搜页面，为保护搜索页拒绝自动关闭任何标签。"
            "任务结束后请确认微信回到“搜一搜”页，再重试受影响账号。",
        )

    def test_process_reader_logs_event_summary_instead_of_raw_json(self) -> None:
        """结构化事件进入控制台后必须是一行摘要，完整 JSON 仅留在任务原始日志。"""

        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = iter(
                    [
                        'INFO {"event":"account_collection_finished","account":"测试公众号",'
                        '"detected_articles":0,"stop_reason":"遇到更早时间边界",'
                        '"scan":{"observed_cards":8,"outside_range_cards":8}}\n'
                    ]
                )

            @staticmethod
            def wait() -> int:
                return 0

        state = panel.ControlState()
        state.history = panel.RunHistory(self.temp_path / "run_history.json")
        process = FakeProcess()
        state.process = process  # type: ignore[assignment]
        state.active_run_id = "summary-test"
        with patch.object(panel, "PANEL_LOG_PATH", self.temp_path / "control-panel.log"):
            state._read_process(process, "summary-test")  # type: ignore[arg-type]

        messages = [item["message"] for item in state.logs]
        self.assertIn("公众号无更新：测试公众号；已检查 8 张卡片，范围外 8 张；原因：遇到更早时间边界。", messages)
        self.assertFalse(any('"event"' in message for message in messages))


if __name__ == "__main__":
    unittest.main()
