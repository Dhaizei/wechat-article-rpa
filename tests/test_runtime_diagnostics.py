"""诊断在无 Git 环境也可用，并且不能改变导航操作。"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_diagnostics import runtime_identity
from browser_navigation import Navigator, Page, Checkpoint


class DiagnosticTests(unittest.TestCase):
    def test_identity_reports_actual_entry_and_hash_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "collector.py"
            entry.write_text("print('test')", encoding="utf-8")
            with patch("runtime_diagnostics.subprocess.run", side_effect=FileNotFoundError):
                result = runtime_identity(str(entry))
            self.assertEqual(result["entry_file"], str(entry.resolve()))
            self.assertEqual(result["code_directory"], str(entry.parent.resolve()))
            self.assertIsNone(result["git_commit"])
            self.assertIsNone(result["relevant_files_modified"])
            self.assertEqual(len(result["startup_file_sha256"]["collector.py"]), 64)

    def test_git_timeout_does_not_abort_startup(self):
        with patch("runtime_diagnostics.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 3)):
            self.assertIsNone(runtime_identity(__file__)["git_commit"])

    def test_fallback_reports_all_missing_evidence_without_actions(self):
        origin = Page(1, "p", "profile", "甲", "甲")
        current = Page(1, "a", "article", "正文", "甲")
        traces = []
        def forbidden(*args):
            self.fail("诊断不应发出操作或额外读取页面")
        nav = Navigator(forbidden, forbidden, forbidden, forbidden, trace=traces.append)
        nav.trace_fallback(Checkpoint(origin, (origin,)), current, "restore")
        self.assertEqual(traces[0]["phase"], "restore")
        for reason in ("baseline_tabs_missing", "current_tabs_missing", "origin_tab_unbound", "active_tab_unconfirmed"):
            self.assertIn(reason, traces[0]["reasons"])

    def test_reused_tab_is_distinguished_from_missing_probe(self):
        origin = Page(1, "p", "profile", "甲", "甲")
        current = Page(1, "a", "article", "正文", "甲")
        tabs = (((1,), "甲"), ((2,), "正文"))
        traces = []
        nav = Navigator(lambda: current, lambda: None, lambda: None, lambda: None,
                        signature=lambda: tabs, selected=lambda: (2,), trace=traces.append)
        nav.trace_fallback(Checkpoint(origin, (origin, current), "", tabs, (1,)), current, "track_created")
        self.assertIn("not_single_added_tab", traces[0]["reasons"])
        self.assertNotIn("current_tabs_missing", traces[0]["reasons"])

