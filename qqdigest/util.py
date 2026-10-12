"""通用小工具：剪贴板、文件清理、时间格式化。"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, tzinfo
from pathlib import Path

log = logging.getLogger("qqdigest.util")


def clipboard_set(text: str) -> bool:
    """把文本写入系统剪贴板，失败返回 False（不影响主流程）。"""
    try:
        if sys.platform == "win32":
            proc = subprocess.run(
                ["clip"],
                input=text.encode("utf-16-le"),
                check=False,
                timeout=10,
                shell=False,
            )
            return proc.returncode == 0
        if sys.platform == "darwin":
            proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False, timeout=10)
            return proc.returncode == 0
        if shutil.which("xclip"):
            proc = subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode("utf-8"),
                check=False,
                timeout=10,
            )
            return proc.returncode == 0
    except Exception as exc:
        log.debug("写剪贴板失败：%s", exc)
    return False


def prune_runs(runs_dir: Path, keep_days: int) -> int:
    """删除超过保留期的留档文件，返回删除数量。``keep_days <= 0`` 表示永久保留。"""
    if keep_days <= 0 or not runs_dir.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for f in runs_dir.glob("*.md"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("清理过期留档 %d 个", removed)
    return removed


def fmt_ts(ts: int | float, tz: tzinfo | None = None) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")


def fmt_hm(ts: int | float, tz: tzinfo | None = None) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def fmt_window(start: int, end: int, tz: tzinfo | None = None) -> str:
    """人类可读的时间窗描述。"""
    same_day = datetime.fromtimestamp(start, tz).date() == datetime.fromtimestamp(end, tz).date()
    if same_day:
        return (
            f"{datetime.fromtimestamp(start, tz).strftime('%Y-%m-%d %H:%M')}"
            f" ~ {datetime.fromtimestamp(end, tz).strftime('%H:%M')}"
        )
    return f"{fmt_ts(start, tz)} ~ {fmt_ts(end, tz)}"
