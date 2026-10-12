"""主窗口：标签页组织、配置读写、后端进程控制。"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from ..config import Config, ensure_dirs, load_config
from ..toml_writer import TomlEditError, TomlEditor
from .runner import BackendRunner
from .tabs_config import ALL_TABS as CONFIG_TABS
from .tabs_runtime import LogsTab, StatsTab, StatusTab
from .widgets import setup_styles

log = logging.getLogger("qqdigest.gui")

APP_TITLE = "qqdigest — QQ 消息定时整合"


class App(tk.Tk):
    """GUI 主窗口。"""

    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self.project_root = Path(__file__).resolve().parents[2]
        self.config_path = Path(config_path) if config_path else self.project_root / "config.toml"

        self.title(APP_TITLE)
        self.geometry("1080x780")
        self.minsize(940, 640)
        setup_styles(self)

        self.current_config: Config | None = None
        self.config_tabs: list[Any] = []
        self._out_queue: queue.Queue[str] = queue.Queue()

        self.runner = BackendRunner(
            self.project_root,
            on_output=self._on_backend_output,
            on_exit=self._on_backend_exit,
        )

        self._build_menu()
        self._build_toolbar()
        self._build_notebook()
        self._build_statusbar()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._closing = False
        self._after_id_drain: str | None = None
        self._after_id_refresh: str | None = None
        self._after_id_drain = self.after(120, self._drain_output)
        self._after_id_refresh = self.after(1000, self._periodic_refresh)
        self.reload_config(initial=True)

    # ================================================================== 构建
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="保存配置\tCtrl+S", command=self.save_config)
        m_file.add_command(label="重新加载配置", command=self.reload_config)
        m_file.add_separator()
        m_file.add_command(label="打开配置文件所在目录", command=lambda: self.open_dir("root"))
        m_file.add_command(label="另存配置副本…", command=self.save_config_as)
        m_file.add_separator()
        m_file.add_command(label="退出", command=self.on_close)
        menubar.add_cascade(label="文件", menu=m_file)

        m_run = tk.Menu(menubar, tearoff=0)
        m_run.add_command(label="启动后端", command=self._menu_start)
        m_run.add_command(label="停止后端", command=self._menu_stop)
        m_run.add_separator()
        m_run.add_command(label="体检（check）", command=lambda: self.run_cli(["check"]))
        m_run.add_command(label="端到端自检（selftest）", command=lambda: self.run_cli(["selftest"]))
        m_run.add_command(label="立即生成简报（挂起）", command=lambda: self.run_cli(["digest-now", "--hold"]))
        m_run.add_command(label="查看统计（stats）", command=lambda: self.run_cli(["stats"]))
        menubar.add_cascade(label="运行", menu=m_run)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="使用说明", command=self.show_help)
        m_help.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=m_help)

        self.configure(menu=menubar)
        self.bind_all("<Control-s>", lambda _e: self.save_config())

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 8, 8, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="保存配置", command=self.save_config).pack(side="left")
        ttk.Button(bar, text="重新加载", command=self.reload_config).pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(bar, text="启动后端", command=self._menu_start).pack(side="left")
        ttk.Button(bar, text="停止后端", command=self._menu_stop).pack(side="left", padx=(6, 0))
        self.dirty_label = ttk.Label(bar, text="", style="Warn.TLabel")
        self.dirty_label.pack(side="right")

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.status_tab = StatusTab(self.notebook, self)
        self.stats_tab = StatsTab(self.notebook, self)
        self.logs_tab = LogsTab(self.notebook, self)
        self.notebook.add(self.status_tab, text="运行状态")
        self.notebook.add(self.stats_tab, text="统计")
        self.notebook.add(self.logs_tab, text="日志简报")

        for cls in CONFIG_TABS:
            tab = cls(self.notebook, self)
            self.config_tabs.append(tab)
            self.notebook.add(tab, text=cls.title)

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 0, 8, 6))
        bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var, style="Hint.TLabel").pack(side="left")
        self.mode_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.mode_var).pack(side="right")

    # ============================================================== 配置读写
    def reload_config(self, *, initial: bool = False) -> None:
        try:
            cfg = load_config(self.config_path)
        except Exception as exc:
            if initial:
                messagebox.showerror(
                    "配置读取失败",
                    f"{exc}\n\n配置文件：{self.config_path}",
                    parent=self,
                )
            else:
                messagebox.showerror("配置读取失败", str(exc), parent=self)
            return

        ensure_dirs(cfg)
        self.current_config = cfg
        for tab in self.config_tabs:
            try:
                tab.load(cfg)
            except Exception as exc:  # pragma: no cover - 单个页面出错不应拖垮整窗
                log.exception("页面 %s 加载失败：%s", getattr(tab, "title", "?"), exc)
        self._update_mode_label()
        self.status_var.set(f"已加载配置：{self.config_path}")
        self.dirty_label.configure(text="")
        self.status_tab.refresh()
        self.logs_tab.refresh()

    def save_config(self, *, silent: bool = False) -> bool:
        cfg = self.current_config
        if cfg is None:
            messagebox.showwarning("尚未加载", "配置尚未加载成功，无法保存", parent=self)
            return False

        errors: list[str] = []
        for tab in self.config_tabs:
            try:
                errors.extend(tab.validate())
            except Exception as exc:
                errors.append(f"【{getattr(tab, 'title', '?')}】校验时出错：{exc}")
        if errors:
            messagebox.showerror("配置有问题，未保存", "\n".join(f"• {e}" for e in errors), parent=self)
            return False

        values: dict[tuple[str, str], Any] = {}
        for tab in self.config_tabs:
            values.update(tab.collect())

        try:
            editor = TomlEditor(self.config_path)
            editor.set_all(values)
            backup = editor.save()
        except TomlEditError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return False
        except Exception as exc:
            messagebox.showerror("保存失败", f"{exc}", parent=self)
            return False

        # 二次校验：写盘后必须仍能被程序解析，否则回滚
        try:
            new_cfg = load_config(self.config_path)
        except Exception as exc:
            if backup and Path(backup).is_file():
                try:
                    Path(backup).replace(self.config_path)
                except Exception:
                    pass
                messagebox.showerror(
                    "保存被回滚",
                    f"新配置无法解析，已还原上一版。\n\n原因：{exc}",
                    parent=self,
                )
            else:
                messagebox.showerror("保存失败", f"新配置无法解析：{exc}", parent=self)
            return False

        self.current_config = new_cfg
        self._update_mode_label()
        self.status_var.set(f"配置已保存：{self.config_path}")
        self.dirty_label.configure(text="")

        if not silent:
            msg = "配置已保存。"
            if backup:
                msg += f"\n\n已备份旧版本到：\n{backup}"
            if self.runner.is_running():
                msg += "\n\n后端正在运行，需要重启后端才能让新配置生效。"
            messagebox.showinfo("保存配置", msg, parent=self)
        self.status_tab.refresh()
        return True

    def save_config_as(self) -> None:
        target = filedialog.asksaveasfilename(
            parent=self,
            title="另存配置副本",
            defaultextension=".toml",
            initialdir=str(self.project_root),
            initialfile="config.copy.toml",
            filetypes=[("TOML 配置", "*.toml"), ("所有文件", "*.*")],
        )
        if not target:
            return
        try:
            values: dict[tuple[str, str], Any] = {}
            for tab in self.config_tabs:
                values.update(tab.collect())
            editor = TomlEditor(self.config_path)
            editor.set_all(values)
            Path(target).write_text(editor.render(), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("另存失败", str(exc), parent=self)
            return
        messagebox.showinfo("另存配置", f"已保存到：\n{target}", parent=self)

    def _update_mode_label(self) -> None:
        cfg = self.current_config
        if cfg is None:
            return
        mode = "干跑模式（不发送到 QQ）" if cfg.general.dry_run else "正式发送"
        self.mode_var.set(
            f"{mode} ｜ 大号 {cfg.onebot.self_id} → 小号 {cfg.target.send_to} ｜ "
            f"发送时刻 {', '.join(cfg.general.schedule)}"
        )

    # ============================================================== 后端控制
    def start_backend(self) -> tuple[bool, str]:
        if self.runner.is_running():
            return False, "后端已在运行"
        ok, msg = self.runner.start(["run"])
        if ok:
            self.status_var.set(msg)
        self.status_tab.refresh()
        return ok, msg

    def stop_backend(self) -> tuple[bool, str]:
        self._stopping = True
        try:
            ok, msg = self.runner.stop()
        finally:
            self._stopping = False
        self.status_var.set(msg)
        self.status_tab.refresh()
        return ok, msg

    def _menu_start(self) -> None:
        # 启动前静默落盘一次，避免界面上的修改没生效
        if not self.save_config(silent=True):
            return
        ok, msg = self.start_backend()
        if not ok:
            messagebox.showwarning("启动后端", msg, parent=self)

    def _menu_stop(self) -> None:
        ok, msg = self.stop_backend()
        if not ok:
            messagebox.showinfo("停止后端", msg, parent=self)

    def run_cli(self, args: list[str], on_done: Callable[[int], None] | None = None) -> None:
        def wrapped(code: int) -> None:
            if on_done is not None:
                self.after(0, lambda: on_done(code))

        self.status_var.set(f"正在执行：run.py {' '.join(args)}")
        self.runner.run_oneshot(args, on_done=wrapped)
        self.notebook.select(self.status_tab)

    def _on_backend_output(self, line: str) -> None:
        self._out_queue.put(line)

    def _on_backend_exit(self, code: int) -> None:
        self.after(0, lambda: self._handle_backend_exit(code))

    def _handle_backend_exit(self, code: int) -> None:
        self.status_tab.append(f"[界面] 后端进程已退出（退出码 {code}）\n")
        self.status_tab.refresh()
        if code == 0 or getattr(self, "_stopping", False):
            return
        messagebox.showwarning(
            "后端已退出",
            f"后端进程意外退出，退出码 {code}。\n"
            "请查看「运行状态」页的输出，或在「日志简报」页查看日志。",
            parent=self,
        )

    def _drain_output(self) -> None:
        if self._closing:
            return
        lines: list[str] = []
        for _ in range(600):
            try:
                lines.append(self._out_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.status_tab.append("".join(lines))
        self._after_id_drain = self.after(120, self._drain_output)

    def _cancel_timers(self) -> None:
        """取消所有 ``after`` 定时器。

        不做这件事的话，窗口关掉之后回调还会被触发，Tk 会报
        ``invalid command name ..._drain_output``——测试里能稳定复现。
        """
        self._closing = True
        for attr in ("_after_id_drain", "_after_id_refresh"):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)

    def destroy(self) -> None:
        """销毁窗口前先停掉所有定时器。

        重写 ``destroy`` 而不是只在 ``on_close`` 里处理：任何销毁路径
        （关窗口、测试里直接 destroy、异常退出）都必须取消 ``after`` 回调，
        否则回调会在窗口消失后触发，Tk 报 ``invalid command name ...``。
        """
        self._cancel_timers()
        super().destroy()

    # ============================================================== 其它
    def read_store(self):
        """以只读方式打开数据库（供界面显示统计）。

        使用 ``Store.open_readonly``：不写任何 PRAGMA、不建表、不迁移，
        避免界面每 5 秒刷新一次就和后端进程抢数据库。数据库尚不存在时返回 None。
        """
        from ..store import Store

        cfg = self.current_config
        if cfg is None:
            raise RuntimeError("配置未加载")
        return Store.open_readonly(cfg.db_path)

    def write_store(self):
        """以可写方式打开数据库（仅在用户主动改标签时用）。

        日常读取一律走只读的 :meth:
ead_store，避免界面和后端抢库；
        只有"手工改标签"这种明确的写操作才用这个。
        """
        from ..store import Store

        cfg = self.current_config
        if cfg is None:
            raise RuntimeError("配置未加载")
        if not cfg.db_path.is_file():
            return None
        return Store(cfg.db_path)

    def open_watch_window(self) -> None:
        """在新控制台窗口里跑 ``run.py watch``，方便实时确认上报链路是否打通。

        用独立窗口而不是内嵌到界面，是为了让用户随时 Ctrl+C 结束，并完整看到输出。
        """
        entry = self.project_root / "run.py"
        if not entry.is_file():
            messagebox.showerror("找不到入口", f"缺少 {entry}", parent=self)
            return
        try:
            import subprocess

            cmd = (
                f'start "qqdigest 实时监听" cmd /k '
                f'""{sys.executable}" "{entry}" watch"'
            )
            subprocess.Popen(cmd, shell=True, cwd=str(self.project_root))  # noqa: S602
            self.status_var.set("已在新窗口启动实时监听")
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc), parent=self)

    def run_async(self, task: Callable[[], Any], callback: Callable[[Any, Exception | None], None]) -> None:
        """在后台线程执行阻塞操作，结果回到主线程回调。"""

        def worker() -> None:
            result: Any = None
            error: Exception | None = None
            try:
                result = task()
            except Exception as exc:
                error = exc
            self.after(0, lambda: callback(result, error))

        threading.Thread(target=worker, daemon=True, name="gui-async").start()

    def open_dir(self, which: str) -> None:
        cfg = self.current_config
        mapping = {
            "root": self.project_root,
            "runs": cfg.storage.runs_dir if cfg else self.project_root / "runs",
            "logs": cfg.storage.logs_dir if cfg else self.project_root / "logs",
            "data": cfg.storage.data_dir if cfg else self.project_root / "data",
        }
        target = Path(mapping.get(which, self.project_root))
        target.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                import os

                os.startfile(str(target))  # noqa: S606
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(target)])  # noqa: S603,S607
        except Exception as exc:
            messagebox.showerror("打开目录失败", str(exc), parent=self)

    def open_log_file(self) -> None:
        cfg = self.current_config
        path = (cfg.storage.logs_dir if cfg else self.project_root / "logs") / "qqdigest.log"
        if not path.is_file():
            messagebox.showinfo("日志", f"日志文件还不存在：\n{path}", parent=self)
            return
        try:
            if sys.platform == "win32":
                import os

                os.startfile(str(path))  # noqa: S606
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
        except Exception as exc:
            messagebox.showerror("打开日志失败", str(exc), parent=self)

    def _on_tab_changed(self, _event: tk.Event) -> None:
        current = self.notebook.nametowidget(self.notebook.select())
        if hasattr(current, "on_show"):
            try:
                current.on_show()
            except Exception as exc:
                log.debug("页面刷新失败：%s", exc)

    def _periodic_refresh(self) -> None:
        try:
            current = self.notebook.nametowidget(self.notebook.select())
            if hasattr(current, "on_show"):
                current.on_show()
        except Exception:
            pass
        if self._closing:
            return
        self._after_id_refresh = self.after(5000, self._periodic_refresh)

    def show_help(self) -> None:
        messagebox.showinfo(
            "使用说明",
            "首次使用顺序：\n\n"
            "1. 在「QQ 接入」页确认大号/小号 QQ 号，并点「测试连接」确认 NapCat 已就绪；\n"
            "2. 在「大模型」页点「测试连接」，确认 LM Studio 可用；\n"
            "3. 在「采集过滤」页决定是否采集群消息（建议先加白名单）；\n"
            "4. 在「重要通知」页填写关键词和你关心的人；\n"
            "5. 点「保存配置」，再点「启动后端」；\n"
            "6. 保持「常规」页的干跑模式开启一段时间，在「日志简报」页检查简报质量；\n"
            "7. 满意后关闭干跑模式，重新保存并重启后端。\n\n"
            "完整文档见项目根目录的 README.md。",
            parent=self,
        )

    def show_about(self) -> None:
        from .. import __version__

        messagebox.showinfo(
            "关于",
            f"{APP_TITLE}\n版本 {__version__}\n\n"
            "全本地运行，不调用任何在线大模型 API。\n"
            "依赖：仅 Python 标准库（含 tkinter）。\n\n"
            f"项目目录：{self.project_root}",
            parent=self,
        )

    def on_close(self) -> None:
        self._cancel_timers()
        if self.runner.is_running():
            answer = messagebox.askyesnocancel(
                "退出",
                "后端正在运行。\n\n"
                "是 = 停止后端并退出\n"
                "否 = 保持后端运行，只关闭界面\n"
                "取消 = 不退出",
                parent=self,
            )
            if answer is None:
                return
            if answer:
                self.runner.stop()
        self.destroy()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config_path = None
    if argv:
        config_path = Path(argv[0])
    try:
        app = App(config_path)
    except Exception as exc:
        print(f"界面启动失败：{exc}", file=sys.stderr)
        return 1
    app.mainloop()
    return 0
