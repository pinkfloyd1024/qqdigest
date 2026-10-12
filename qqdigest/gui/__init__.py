"""图形界面（tkinter，仅标准库）。

入口：项目根目录的 ``gui.py``，或双击 ``启动界面.bat``。
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from .app import main as _main

    return _main(argv)
