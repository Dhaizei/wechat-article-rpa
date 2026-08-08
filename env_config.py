"""加载项目根目录的 ``.env`` 配置。

项目不额外依赖 python-dotenv，便携包在新电脑上解压后也能直接读取配置。
系统环境变量优先于 ``.env``，便于生产环境通过操作系统安全覆盖本地文件。
"""

from __future__ import annotations

import os
import re
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_value(raw_value: str) -> str:
    """解析常见的 KEY=value、引号值和行末注释。"""
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
        if raw_value.strip().startswith('"'):
            value = (
                value.replace(r"\n", "\n")
                .replace(r"\r", "\r")
                .replace(r"\t", "\t")
                .replace(r'\"', '"')
                .replace(r"\\", "\\")
            )
        return value

    # 只有空白后的 # 才视为注释，避免误伤 URL 或密码中的 #。
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def load_project_env(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """读取项目 ``.env`` 并写入当前进程环境变量。

    返回本次实际写入的键值，方便测试和启动诊断；不存在 ``.env`` 时安静返回空字典。
    """
    env_path = Path(path) if path is not None else ENV_PATH
    if not env_path.is_file():
        return {}

    loaded: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{env_path} 第 {line_number} 行缺少等号：{raw_line}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f"{env_path} 第 {line_number} 行变量名无效：{key}")
        value = _parse_value(raw_value)
        if override or key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded
