"""项目 .env 加载器的离线测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from env_config import load_project_env


class EnvConfigTests(unittest.TestCase):
    def test_loads_values_comments_quotes_and_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "\ufeff# 测试配置\n"
                "QWEN_VL_API_KEY=secret-value\n"
                'QWEN_VL_MODEL="dashscope/qwen3-vl-plus"\n'
                "MONGO_URI=mongodb://host/db # 本地说明\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                loaded = load_project_env(env_path)
                self.assertEqual(os.environ["QWEN_VL_API_KEY"], "secret-value")
                self.assertEqual(os.environ["QWEN_VL_MODEL"], "dashscope/qwen3-vl-plus")
                self.assertEqual(os.environ["MONGO_URI"], "mongodb://host/db")
                self.assertEqual(set(loaded), {"QWEN_VL_API_KEY", "QWEN_VL_MODEL", "MONGO_URI"})

    def test_system_environment_has_priority_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("QWEN_VL_API_KEY=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"QWEN_VL_API_KEY": "from-system"}, clear=True):
                loaded = load_project_env(env_path)
                self.assertEqual(os.environ["QWEN_VL_API_KEY"], "from-system")
                self.assertNotIn("QWEN_VL_API_KEY", loaded)

    def test_invalid_line_reports_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("QWEN_VL_API_KEY\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "第 1 行"):
                load_project_env(env_path)


if __name__ == "__main__":
    unittest.main()
