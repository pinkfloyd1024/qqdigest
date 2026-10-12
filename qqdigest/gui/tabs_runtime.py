"""运行类标签页：状态与进程控制、日志与简报留档。"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from ..scheduler import slots_for_day
from ..store import Store
from .widgets import PAD, Hint

_STATE_TEXT = {
    "pending": "待发送",
    "sent": "已发送",
    "failed": "失败/跳过",
    "held": "已挂起",
}


def _state_text(raw: str | None, fire_at: int, now_ts: int) -> str:
    if not raw:
        return "未生成" if fire_at > now_ts else "未生成（已过时）"
    return _STATE_TEXT.get(raw, raw)


class StatusTab(ttk.Frame):
    """运行状态：进程控制、今日档位、统计、后端输出。"""

    title = "运行状态"

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master)
        self.app = app

        top = ttk.Frame(self, padding=(PAD, PAD, PAD, 0))
        top.pack(fill="x")
        self.status_var = tk.StringVar(value="后端：未运行")
        ttk.Label(top, textvariable=self.status_var, style="Title.TLabel").pack(side="left")

        btns = ttk.Frame(top)
        btns.pack(side="right")
        self.btn_start = ttk.Button(btns, text="启动后端", command=self._start)
        self.btn_start.grid(row=0, column=0, padx=3)
        self.btn_stop = ttk.Button(btns, text="停止后端", command=self._stop)
        self.btn_stop.grid(row=0, column=1, padx=3)
        ttk.Button(btns, text="体检", command=lambda: self._cli(["check"])).grid(row=0, column=2, padx=3)
        ttk.Button(btns, text="实时监听", command=self.app.open_watch_window).grid(row=0, column=3, padx=3)
        ttk.Button(btns, text="自检", command=lambda: self._cli(["selftest"])).grid(row=0, column=4, padx=3)
        ttk.Button(btns, text="生成简报", command=self._digest_now).grid(row=0, column=5, padx=3)
        self.btn_send = ttk.Button(btns, text="立即整合并发送", command=self._digest_and_send)
        self.btn_send.grid(row=0, column=6, padx=3)
        ttk.Button(btns, text="刷新", command=self.refresh).grid(row=0, column=7, padx=3)

        self.hint = Hint(
            self,
            "「启动后端」会在后台运行 python run.py run：实时采集消息并按时生成、发送简报。"
            "改了配置需要重启后端才会生效。\n"
            "「实时监听」会另开一个窗口显示每一条收到的消息——第一次配好 NapCat 后，"
            "用它发一条测试消息即可确认上报链路是否打通。\n"
            "「立即整合并发送」= 现在就把这一窗口的消息整合好，直接发到小号（不等定时）。",
            wraplength=980,
        )
        self.hint.pack(anchor="w", padx=PAD, pady=(4, 0))

        # ---------------- 今日档位 ----------------
        slots_box = ttk.LabelFrame(self, text="今日档位", padding=PAD)
        slots_box.pack(fill="x", padx=PAD, pady=(PAD, 0))
        cols = ("fire", "pre", "state", "msgs", "tokens", "file")
        self.slots = ttk.Treeview(slots_box, columns=cols, show="headings", height=4)
        for col, text, width in (
            ("fire", "发送时刻", 90),
            ("pre", "预计算", 90),
            ("state", "状态", 110),
            ("msgs", "消息数", 70),
            ("tokens", "token", 100),
            ("file", "留档", 320),
        ):
            self.slots.heading(col, text=text)
            self.slots.column(col, width=width, anchor="w")
        self.slots.pack(fill="x")

        # ---------------- 统计 ----------------
        stat_box = ttk.LabelFrame(self, text="统计", padding=PAD)
        stat_box.pack(fill="x", padx=PAD, pady=(PAD, 0))
        self.stat_var = tk.StringVar(value="—")
        ttk.Label(stat_box, textvariable=self.stat_var).pack(anchor="w")
        self.peers = ttk.Treeview(
            stat_box, columns=("n", "name", "peer"), show="headings", height=4
        )
        for col, text, width in (("n", "条数", 70), ("name", "会话", 220), ("peer", "QQ/群号", 160)):
            self.peers.heading(col, text=text)
            self.peers.column(col, width=width, anchor="w")
        self.peers.pack(fill="x", pady=(6, 0))

        # ---------------- 输出控制台 ----------------
        con_box = ttk.LabelFrame(self, text="后端输出", padding=PAD)
        con_box.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        bar = ttk.Frame(con_box)
        bar.pack(fill="x")
        ttk.Button(bar, text="清空", command=self._clear_console).pack(side="right")
        ttk.Button(bar, text="打开日志文件", command=self.app.open_log_file).pack(side="right", padx=(0, 6))
        wrap = ttk.Frame(con_box)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.console = tk.Text(wrap, wrap="none", height=10, state="disabled")
        yscroll = ttk.Scrollbar(wrap, orient="vertical", command=self.console.yview)
        self.console.configure(yscrollcommand=yscroll.set)
        self.console.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

    # ------------------------------------------------------------ 进程控制
    def _start(self) -> None:
        ok, msg = self.app.start_backend()
        self.append(f"[界面] {msg}\n")
        if not ok:
            messagebox.showwarning("启动后端", msg, parent=self)
        self.refresh()

    def _stop(self) -> None:
        ok, msg = self.app.stop_backend()
        self.append(f"[界面] {msg}\n")
        self.refresh()

    def _cli(self, args: list[str]) -> None:
        self.append(f"[界面] 执行 {' '.join(args)} …\n")
        self.app.run_cli(args, on_done=lambda code: self.append(f"[界面] {' '.join(args)} 退出码 {code}\n"))
        self.after(1500, self.refresh)

    def _digest_now(self) -> None:
        self.append("[界面] 立即生成简报（结果会被挂起，不会自动发送）…\n")
        self.app.run_cli(["digest-now", "--hold"], on_done=lambda _c: self.after(800, self.refresh))

    def _digest_and_send(self) -> None:
        """立即整合并把结果发到小号。干跑模式下会先征求确认。"""
        cfg = self.app.current_config
        if cfg is None:
            messagebox.showwarning("尚未加载", "配置未加载", parent=self)
            return

        args = ["digest-now", "--send"]
        if cfg.general.dry_run:
            answer = messagebox.askyesnocancel(
                "干跑模式",
                "当前是**干跑模式**，正常不会真的发送。\n\n"
                "「是」= 这一次强制真发到小号\n"
                "「否」= 只生成不发送（结果挂起）\n"
                "「取消」= 什么都不做",
                parent=self,
            )
            if answer is None:
                return
            if answer:
                args.append("--force-send")
            else:
                args = ["digest-now", "--hold"]
        else:
            if not messagebox.askyesno(
                "确认发送",
                f"现在立即整合并把简报发送到小号 {cfg.target.send_to}？",
                parent=self,
            ):
                return

        self.append(f"[界面] 执行 {' '.join(args)} …\n")
        self.app.run_cli(args, on_done=lambda _c: self.after(800, self.refresh))

    # ------------------------------------------------------------ 显示
    def append(self, text: str) -> None:
        self.console.configure(state="normal")
        self.console.insert(tk.END, text)
        # 控制台最多保留 2000 行
        line_count = int(self.console.index("end-1c").split(".")[0])
        if line_count > 2000:
            self.console.delete("1.0", f"{line_count - 2000}.0")
        self.console.see(tk.END)
        self.console.configure(state="disabled")

    def _clear_console(self) -> None:
        self.console.configure(state="normal")
        self.console.delete("1.0", tk.END)
        self.console.configure(state="disabled")

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        cfg = self.app.current_config
        running = self.app.runner.is_running()
        pid = self.app.runner.pid
        if running:
            self.status_var.set(f"后端：运行中（PID {pid}）")
            self.btn_start.state(["disabled"])
            self.btn_stop.state(["!disabled"])
        else:
            self.status_var.set("后端：未运行")
            self.btn_start.state(["!disabled"])
            self.btn_stop.state(["disabled"])

        if cfg is None:
            return
        self._refresh_slots(cfg)
        self._refresh_stats(cfg)

    def _refresh_slots(self, cfg) -> None:
        self.slots.delete(*self.slots.get_children())
        now = datetime.now(cfg.tz)
        now_ts = int(now.timestamp())
        try:
            store = self.app.read_store()
        except Exception:
            store = None
        try:
            for slot in slots_for_day(now.date(), cfg, cfg.tz):
                row = store.digest_by_slot(slot.key) if store else None
                state = _state_text(row.get("state") if row else None, slot.fire_at, now_ts)
                msgs = str(row.get("msg_count")) if row else "—"
                tokens = (
                    f"{row.get('prompt_tokens')}/{row.get('completion_tokens')}" if row else "—"
                )
                run_file = (row.get("run_file") if row else "") or "—"
                self.slots.insert(
                    "",
                    tk.END,
                    values=(
                        slot.key.split("T")[1],
                        datetime.fromtimestamp(slot.precompute_at, cfg.tz).strftime("%H:%M"),
                        state,
                        msgs,
                        tokens,
                        run_file,
                    ),
                )
        finally:
            if store:
                store.close()

    def _refresh_stats(self, cfg) -> None:
        self.peers.delete(*self.peers.get_children())
        now_ts = int(datetime.now(cfg.tz).timestamp())
        try:
            store = self.app.read_store()
        except Exception as exc:
            self.stat_var.set(f"无法读取数据库：{exc}")
            return
        if store is None:
            self.stat_var.set(
                f"数据库尚未创建（{cfg.db_path}）。启动后端并收到第一条消息后即会出现。"
            )
            return
        try:
            total = store.count_messages()
            rows = store.stats_by_peer(now_ts - 86400, now_ts + 1)
            recent = store.recent_digests(3)
            summary = (
                f"累计收录 {total} 条；最近 24 小时 {sum(r[2] for r in rows)} 条 / "
                f"{len(rows)} 个会话"
            )
            if recent:
                latest = recent[0]
                state = _STATE_TEXT.get(str(latest.get("state")), str(latest.get("state")))
                summary += (
                    f"；最近一档 {str(latest.get('slot', '')).replace('T', ' ')}（{state}）"
                )
            self.stat_var.set(summary + f"　数据库：{cfg.db_path}")
            for peer_id, peer_name, n in rows[:8]:
                self.peers.insert("", tk.END, values=(n, peer_name or "(未知)", peer_id))
        finally:
            store.close()


class StatsTab(ttk.Frame):
    """消息量统计与可视化。

    用 tkinter 的 Canvas 直接画条形图，不引入 matplotlib —— 保持零第三方依赖。
    """

    title = "统计"

    RANGES = {
        "最近 24 小时": 1,
        "最近 3 天": 3,
        "最近 7 天": 7,
        "最近 30 天": 30,
    }

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master)
        self.app = app

        bar = ttk.Frame(self, padding=(PAD, PAD, PAD, 0))
        bar.pack(fill="x")
        ttk.Label(bar, text="时间范围", style="Section.TLabel").pack(side="left", padx=(0, 8))
        self.range_var = tk.StringVar(value="最近 24 小时")
        combo = ttk.Combobox(
            bar, textvariable=self.range_var, values=list(self.RANGES), state="readonly", width=14
        )
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        self.include_muted = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="包含静音(权重0)消息", variable=self.include_muted,
                        command=self.refresh).pack(side="left", padx=12)
        ttk.Button(bar, text="刷新", command=self.refresh).pack(side="left")

        self.summary_var = tk.StringVar(value="—")
        ttk.Label(self, textvariable=self.summary_var, padding=(PAD, 6, PAD, 0)).pack(anchor="w")

        # 时段分布
        hour_box = ttk.LabelFrame(self, text="每个时段的消息数量分布（按小时，UTC+8）", padding=PAD)
        hour_box.pack(fill="x", padx=PAD, pady=(6, 0))
        self.hour_canvas = tk.Canvas(hour_box, height=170, highlightthickness=0, bg="#fbfbfb")
        self.hour_canvas.pack(fill="x")
        self.hour_canvas.bind("<Configure>", lambda _e: self._draw_hours())

        # 各群/会话总量
        peer_box = ttk.LabelFrame(self, text="各会话消息接收总数（前 15）", padding=PAD)
        peer_box.pack(fill="both", expand=True, padx=PAD, pady=(6, PAD))
        self.peer_canvas = tk.Canvas(peer_box, height=260, highlightthickness=0, bg="#fbfbfb")
        self.peer_canvas.pack(fill="both", expand=True)
        self.peer_canvas.bind("<Configure>", lambda _e: self._draw_peers())

        self._hours: list[tuple[int, int]] = []
        self._peers: list[tuple[str, int]] = []

    # ------------------------------------------------------------------ 数据
    def _window(self) -> tuple[int, int]:
        days = self.RANGES.get(self.range_var.get(), 1)
        now = int(datetime.now().timestamp())
        return now - days * 86400, now + 60

    def refresh(self) -> None:
        cfg = self.app.current_config
        if cfg is None:
            return
        try:
            store = self.app.read_store()
        except Exception as exc:
            self.summary_var.set(f"读取数据库失败：{exc}")
            return
        if store is None:
            self.summary_var.set("数据库还没建立。")
            return
        try:
            start, end = self._window()
            include = bool(self.include_muted.get())
            hours = store.hourly_distribution(start, end, include_muted=include)
            peers_raw = store.peer_totals(start, end, include_muted=include)
            total = sum(n for _, n in hours)
            muted = store.muted_message_count()
            size_mb = store.database_size_bytes() / 1024 / 1024

            self._hours = hours
            self._peers = [
                (f"{name or pid}", n) for pid, name, _ct, n in peers_raw[:15]
            ]
            self.summary_var.set(
                f"{self.range_var.get()}：共 {total} 条 / {len(peers_raw)} 个会话　|　"
                f"静音(权重0) {muted} 条　|　数据库 {size_mb:.1f} MB"
            )
            self._draw_hours()
            self._draw_peers()
        finally:
            store.close()

    # ------------------------------------------------------------------ 绘图
    def _draw_hours(self) -> None:
        c = self.hour_canvas
        c.delete("all")
        w = c.winfo_width() or 800
        h = c.winfo_height() or 170
        data = {hour: n for hour, n in self._hours}
        values = [data.get(hh, 0) for hh in range(24)]
        peak = max(values) if values else 0
        if peak == 0:
            c.create_text(w / 2, h / 2, text="该时间段没有消息", fill="#999")
            return

        left, right, bottom, top = 34, 10, 26, 18
        plot_w = max(50, w - left - right)
        plot_h = max(30, h - bottom - top)
        bw = plot_w / 24
        for hh in range(24):
            v = values[hh]
            x0 = left + hh * bw + 1
            x1 = left + (hh + 1) * bw - 1
            bh = plot_h * v / peak
            y0 = top + plot_h - bh
            # 工作时段用蓝色，其余用灰色，一眼看出作息
            work = 8 <= hh <= 23
            color = "#4a90d9" if work else "#b9c3cc"
            c.create_rectangle(x0, y0, x1, top + plot_h, fill=color, outline="")
            if v:
                c.create_text((x0 + x1) / 2, y0 - 6, text=str(v), font=("Consolas", 7))
            if hh % 2 == 0:
                c.create_text((x0 + x1) / 2, top + plot_h + 10, text=f"{hh}", font=("Consolas", 7))
        c.create_line(left, top + plot_h, left + plot_w, top + plot_h, fill="#ccc")

    def _draw_peers(self) -> None:
        c = self.peer_canvas
        c.delete("all")
        w = c.winfo_width() or 800
        h = c.winfo_height() or 260
        rows = self._peers
        if not rows:
            c.create_text(w / 2, h / 2, text="该时间段没有数据", fill="#999")
            return
        peak = max(n for _, n in rows) or 1
        label_w = min(260, max(120, w // 3))
        bar_area = max(60, w - label_w - 70)
        row_h = max(14, min(24, (h - 8) / len(rows)))
        for i, (name, n) in enumerate(rows):
            y = 6 + i * row_h
            text = name if len(name) <= 18 else name[:17] + "…"
            c.create_text(label_w - 6, y + row_h / 2, text=text, anchor="e", font=("Microsoft YaHei UI", 8))
            bw = bar_area * n / peak
            c.create_rectangle(label_w, y + 2, label_w + bw, y + row_h - 2,
                               fill="#4a90d9", outline="")
            c.create_text(label_w + bw + 6, y + row_h / 2, text=str(n), anchor="w",
                          font=("Consolas", 8), fill="#555")

    def on_show(self) -> None:
        self.refresh()


class LogsTab(ttk.Frame):
    """日志与简报留档查看。"""

    title = "日志简报"

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master)
        self.app = app
        self._current_run: Path | None = None

        bar = ttk.Frame(self, padding=(PAD, PAD, PAD, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="刷新", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="打开 runs 目录", command=lambda: self.app.open_dir("runs")).pack(
            side="left", padx=6
        )
        ttk.Button(bar, text="打开日志目录", command=lambda: self.app.open_dir("logs")).pack(side="left")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=PAD, pady=PAD)

        # --- 简报留档 ---
        runs_frame = ttk.Frame(self.notebook)
        self.notebook.add(runs_frame, text="简报留档")
        left = ttk.Frame(runs_frame, padding=PAD)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="runs/ 目录").pack(anchor="w")
        self.runs_list = tk.Listbox(left, width=26, height=22, exportselection=False)
        self.runs_list.pack(fill="y", expand=True)
        self.runs_list.bind("<<ListboxSelect>>", lambda _e: self._show_run())
        right = ttk.Frame(runs_frame, padding=(0, PAD, PAD, PAD))
        right.pack(side="left", fill="both", expand=True)
        self.run_text = tk.Text(right, wrap="word")
        rscroll = ttk.Scrollbar(right, orient="vertical", command=self.run_text.yview)
        self.run_text.configure(yscrollcommand=rscroll.set)
        self.run_text.grid(row=0, column=0, sticky="nsew")
        rscroll.grid(row=0, column=1, sticky="ns")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        # --- 运行日志 ---
        log_frame = ttk.Frame(self.notebook)
        self.notebook.add(log_frame, text="运行日志")
        self.log_text = tk.Text(log_frame, wrap="none")
        lscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lscroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(PAD, 0), pady=PAD)
        lscroll.pack(side="right", fill="y", pady=PAD, padx=(0, PAD))

    # ------------------------------------------------------------ 数据
    def refresh(self) -> None:
        cfg = self.app.current_config
        if cfg is None:
            return
        self.runs_list.delete(0, tk.END)
        files = sorted(cfg.storage.runs_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        self._runs = files
        for f in files[:200]:
            self.runs_list.insert(tk.END, f.name)
        if files and self._current_run is None:
            self.runs_list.selection_set(0)
            self._show_run()

        log_file = cfg.storage.logs_dir / "qqdigest.log"
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else "（暂无日志）"
        except Exception as exc:
            text = f"读取日志失败：{exc}"
        lines = text.splitlines()[-400:]
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert("1.0", "\n".join(lines))
        self.log_text.see(tk.END)

    def _show_run(self) -> None:
        selection = self.runs_list.curselection()
        if not selection:
            return
        files = getattr(self, "_runs", [])
        idx = selection[0]
        if idx >= len(files):
            return
        path = files[idx]
        self._current_run = path
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            content = f"读取失败：{exc}"
        self.run_text.delete("1.0", tk.END)
        self.run_text.insert("1.0", content)

    def on_show(self) -> None:
        self.refresh()
