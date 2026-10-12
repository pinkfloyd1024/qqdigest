"""qqdigest 图形界面入口。

用法：
    python gui.py                 # 使用项目根目录下的 config.toml
    python gui.py D:\\path\\x.toml   # 指定配置文件
    python gui.py --preflight     # 启动前自检（供 启动界面.bat 调用）
    pythonw gui.py                # 不显示控制台窗口（推荐，见 启动界面.bat）

界面功能：
    - 可视化编辑全部配置（白名单、重要通知、提示词、模型参数……）
    - 一键启动/停止后端采集与调度进程
    - 体检、自检、立即生成简报
    - 查看后端实时输出、运行日志、历史简报留档
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


def _report_fatal(exc: BaseException) -> None:
    """启动失败时：写错误日志 + 弹一个系统消息框（不依赖 tkinter 能否用）。"""
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_path = PROJECT_ROOT / "logs" / "gui-error.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(detail, encoding="utf-8")
    except Exception:
        pass

    # 先弹框：pythonw 下没有控制台，弹框是唯一可见的反馈
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            f"界面启动失败：\n\n{exc}\n\n详细信息已写入：\n{log_path}",
            "qqdigest 启动失败",
            0x10,
        )
    except Exception:
        pass

    try:
        if sys.stderr is not None:
            print(detail, file=sys.stderr)
    except Exception:
        pass


def preflight() -> int:
    """启动前自检：确认依赖、配置、目录都正常。

    由 ``启动界面.bat`` 调用。成功时通常把输出丢弃（bat 里 >nul），
    失败时会把详细原因打印到控制台，让双击的用户能看到到底哪里不对。
    """
    ok = True
    print("qqdigest 启动前自检")
    print("-" * 60)
    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"项目根目录  : {PROJECT_ROOT}")

    # 1) tkinter
    try:
        import tkinter as tk

        print(f"tkinter     : OK (Tcl/Tk {tk.TkVersion})")
    except Exception as exc:
        ok = False
        print(f"tkinter     : 失败 —— {exc}")
        print("              Python 可能未安装 tcl/tk，请重装 Python 并勾选 tcl/tk。")

    # 2) 本项目模块
    try:
        import qqdigest.gui.app  # noqa: F401
        import qqdigest.toml_writer  # noqa: F401

        print("项目模块    : OK")
    except Exception as exc:
        ok = False
        print(f"项目模块    : 失败 —— {exc}")
        traceback.print_exc()

    # 3) 配置文件
    try:
        from qqdigest.config import load_config

        cfg = load_config(PROJECT_ROOT / "config.toml")
        print(f"配置文件    : OK（大号 {cfg.onebot.self_id} → 小号 {cfg.target.send_to}）")
        print(f"干跑模式    : {cfg.general.dry_run}")
    except Exception as exc:
        ok = False
        print(f"配置文件    : 失败 —— {exc}")

    # 4) 目录可写
    try:
        probe = PROJECT_ROOT / "logs" / ".write-probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print("目录可写    : OK")
    except Exception as exc:
        ok = False
        print(f"目录可写    : 失败 —— {exc}")

    print("-" * 60)
    print("自检通过。" if ok else "自检失败，请按上面提示处理。")
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    if "--preflight" in args:
        return preflight()

    try:
        from qqdigest.gui import main as gui_main
    except Exception as exc:  # 导入期错误
        _report_fatal(exc)
        return 1

    try:
        return gui_main(args)
    except Exception as exc:  # 运行期错误
        _report_fatal(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
