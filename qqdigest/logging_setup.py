"""日志初始化。

- 控制台输出（便于前台观察）
- 文件输出 logs/qqdigest.log，按大小轮转，保留 5 份
- 统一 UTF-8，避免 Windows 控制台中文乱码
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """初始化根日志器（幂等）。"""
    global _configured
    root = logging.getLogger()
    if _configured:
        return root

    logs_dir.mkdir(parents=True, exist_ok=True)
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    fh = RotatingFileHandler(
        logs_dir / "qqdigest.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Windows 控制台按 UTF-8 输出，避免中文乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

    _configured = True
    return root
