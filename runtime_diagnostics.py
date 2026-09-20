"""记录实际启动目录和版本；不读取环境变量、远端地址或认证信息。"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys


def runtime_identity(entry_file: str) -> dict:
    entry = Path(entry_file).resolve()
    root = entry.parent

    def git(*args):
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    files = [entry.name, "browser_navigation.py", "wechat_window_probe.py"]
    hashes = {}
    for name in files:
        try:
            hashes[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        except OSError:
            hashes[name] = None
    changes = git("status", "--porcelain", "--untracked-files=no", "--", *files)
    return dict(diagnostics_version=1, pid=os.getpid(), entry_file=str(entry),
                code_directory=str(root), working_directory=str(Path.cwd()),
                python_executable=sys.executable, git_commit=git("rev-parse", "HEAD"),
                git_branch=git("branch", "--show-current"),
                relevant_files_modified=None if changes is None else bool(changes),
                startup_file_sha256=hashes)

