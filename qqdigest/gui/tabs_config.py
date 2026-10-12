"""配置类标签页：常规、采集过滤、重要通知、提示词、QQ 接入、大模型。

每个页面负责两件事：
- ``load(cfg)``：把配置显示到控件上；
- ``collect()``：把控件内容收集成 ``{(section, key): value}`` 供写回 config.toml。
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .. import prompts
from ..config import Config
from .widgets import FormGrid, Hint, ListEditor, ScrollableFrame

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigTab(ttk.Frame):
    """配置页基类。"""

    title = "页面"
    description = ""

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master)
        self.app = app
        self.scroll = ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True)
        self.form = FormGrid(self.scroll.body)
        self.form.pack(fill="both", expand=True)
        if self.description:
            Hint(self.form, self.description).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
            self.form._row = 1

    # 子类覆盖
    def load(self, cfg: Config) -> None:  # pragma: no cover - GUI
        pass

    def collect(self) -> dict[tuple[str, str], Any]:  # pragma: no cover - GUI
        return {}

    def validate(self) -> list[str]:  # pragma: no cover - GUI
        return []


# ==========================================================================
# 常规
# ==========================================================================


class GeneralTab(ConfigTab):
    title = "常规"
    description = (
        "发送时刻、预计算提前量、错过的档位怎么处理。"
        "「提前预计算」的含义：到点前 N 分钟就把消息交给模型整合并暂存，整点再发出去。"
        "采集窗口的结束边界取预计算时刻，所以不会丢消息。"
    )

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, app)
        f = self.form

        f.add_title("时间")
        self.timezone = f.add_combo(
            "时区", ["Asia/Shanghai", "Asia/Tokyo", "Asia/Seoul", "UTC"], hint="中国无夏令时，选 Asia/Shanghai 即可"
        )
        self.schedule = ListEditor(
            f,
            height=4,
            width=18,
            validator=lambda s: None if _TIME_RE.match(s) else "请用 24 小时制 HH:MM，例如 12:00",
        )
        f.add_widget("每日发送时刻", self.schedule, hint="每行一个，例如 12:00 / 18:00 / 22:00")

        f.add_title("窗口与容错")
        self.precompute_lead_min = f.add_spin(
            "提前预计算（分钟）", from_=0, to=59, hint="建议 3~10；设为 0 表示整点才开始算，会晚到"
        )
        self.first_run_lookback_hours = f.add_spin(
            "首次运行回溯（小时）", from_=1, to=168, hint="数据库为空时向前看多久的消息"
        )
        self.max_window_hours = f.add_spin(
            "最长窗口（小时）", from_=1, to=168, hint="程序停了很久时，单次最多整合多长时间的消息"
        )
        self.catch_up_max_min = f.add_spin(
            "补发窗口（分钟）", from_=0, to=1440, hint="错过的档位超过该分钟数就跳过不补发"
        )
        self.send_retry_window_min = f.add_spin(
            "发送重试窗口（分钟）", from_=1, to=1440, hint="发送失败后在这段时间内持续重试"
        )
        self.poll_interval_sec = f.add_spin(
            "检查间隔（秒）", from_=5, to=600, hint="调度器多久检查一次是否到点"
        )

        f.add_title("运行模式")
        self.dry_run = f.add_check(
            "干跑模式", hint="开启后不向 QQ 发送任何消息，只写留档 + 复制剪贴板（建议先跑一两周）"
        )

    def load(self, cfg: Config) -> None:
        self.timezone.set(cfg.general.timezone)
        if self.timezone.get() != cfg.general.timezone:
            # 配置里写了列表外的取值时，临时加入候选
            self.timezone.configure(values=[cfg.general.timezone, *self.timezone.cget("values")])
            self.timezone.set(cfg.general.timezone)
        self.schedule.set(cfg.general.schedule)
        self.precompute_lead_min.delete(0, tk.END)
        self.precompute_lead_min.insert(0, str(cfg.general.precompute_lead_min))
        self.first_run_lookback_hours.delete(0, tk.END)
        self.first_run_lookback_hours.insert(0, str(cfg.general.first_run_lookback_hours))
        self.max_window_hours.delete(0, tk.END)
        self.max_window_hours.insert(0, str(cfg.general.max_window_hours))
        self.catch_up_max_min.delete(0, tk.END)
        self.catch_up_max_min.insert(0, str(cfg.general.catch_up_max_min))
        self.send_retry_window_min.delete(0, tk.END)
        self.send_retry_window_min.insert(0, str(cfg.general.send_retry_window_min))
        self.poll_interval_sec.delete(0, tk.END)
        self.poll_interval_sec.insert(0, str(cfg.general.poll_interval_sec))
        self.dry_run.set(cfg.general.dry_run)

    def validate(self) -> list[str]:
        errors: list[str] = []
        times = self.schedule.get()
        if not times:
            errors.append("【常规】每日发送时刻不能为空")
        for t in times:
            if not _TIME_RE.match(t):
                errors.append(f"【常规】发送时刻 {t!r} 不是合法的 HH:MM")
        if len(set(times)) != len(times):
            errors.append("【常规】发送时刻有重复")
        return errors

    def collect(self) -> dict[tuple[str, str], Any]:
        return {
            ("general", "timezone"): self.timezone.get(),
            ("general", "schedule"): self.schedule.get(),
            ("general", "precompute_lead_min"): _int(self.precompute_lead_min, 5),
            ("general", "first_run_lookback_hours"): _int(self.first_run_lookback_hours, 12),
            ("general", "max_window_hours"): _int(self.max_window_hours, 24),
            ("general", "catch_up_max_min"): _int(self.catch_up_max_min, 30),
            ("general", "send_retry_window_min"): _int(self.send_retry_window_min, 60),
            ("general", "poll_interval_sec"): _int(self.poll_interval_sec, 15),
            ("general", "dry_run"): bool(self.dry_run.get()),
        }


# ==========================================================================
# 采集与过滤
# ==========================================================================


class FilterTab(ConfigTab):
    title = "采集过滤"
    description = (
        "决定哪些会话的消息会被采集。私聊默认全收；群聊默认不收——"
        "群消息量可能是私聊的几十倍，建议只把你真正关心的群加入白名单。"
    )

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, app)
        f = self.form

        f.add_title("采集范围")
        self.collect_private = f.add_check("采集私聊")
        self.collect_group = f.add_check("采集群聊", hint="关闭时下面的白名单不生效")
        self.only_incoming = f.add_check(
            "只整合收到的消息", hint="开启后不整合你自己发出的内容（推荐）"
        )

        f.add_title("群白名单")
        self.group_whitelist = ListEditor(
            f,
            height=8,
            validator=lambda s: None if s.isdigit() else "群号应为纯数字，例如 555000111",
        )
        f.add_widget("只采集这些群", self.group_whitelist, hint="留空 = 采集全部群（谨慎）")
        f.add_note("提示：群号可以在 QQ 群资料页看到，也可以在日志里看到（收到群消息时会打印 peer_id）。")

        f.add_title("会话黑名单")
        self.peer_blacklist = ListEditor(
            f,
            height=6,
            validator=lambda s: None if s.isdigit() else "应为纯数字的好友 QQ 号或群号",
        )
        f.add_widget("永不采集", self.peer_blacklist, hint="优先级最高，命中后一定不采集")

    def load(self, cfg: Config) -> None:
        self.collect_private.set(cfg.filter.collect_private)
        self.collect_group.set(cfg.filter.collect_group)
        self.only_incoming.set(cfg.filter.only_incoming)
        self.group_whitelist.set(cfg.filter.group_whitelist)
        self.peer_blacklist.set(sorted(cfg.filter.peer_blacklist))

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.collect_group.get() and not self.group_whitelist.get():
            # 只是提醒，不阻止保存
            pass
        return errors

    def collect(self) -> dict[tuple[str, str], Any]:
        return {
            ("filter", "collect_private"): bool(self.collect_private.get()),
            ("filter", "collect_group"): bool(self.collect_group.get()),
            ("filter", "only_incoming"): bool(self.only_incoming.get()),
            ("filter", "group_whitelist"): self.group_whitelist.get(),
            ("filter", "peer_blacklist"): self.peer_blacklist.get(),
        }


# ==========================================================================
# 重要通知
# ==========================================================================


class ImportantTab(ConfigTab):
    title = "重要通知"
    description = (
        "给消息打「重要」标记。命中后消息在简报里会加前缀、单独列一节，"
        "也可以选择命中时立刻弹窗提醒你，不用等定时简报。"
    )

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, app)
        f = self.form

        f.add_title("命中规则（满足任一即可）")
        self.keywords = ListEditor(f, height=8)
        f.add_widget("重要关键词", self.keywords, hint="正文包含任意一个词即命中，大小写不敏感")
        f.add_note("示例：紧急、马上、转账、合同、deadline、报销、体检、面试")

        self.peers = ListEditor(
            f,
            height=6,
            validator=lambda s: None if s.isdigit() else "应为纯数字的好友 QQ 号或群号",
        )
        f.add_widget("重要联系人/群", self.peers, hint="这些会话发来的消息一律算重要")
        f.add_note("把老板、家人、客服之类的 QQ 号填进来，就不会再漏掉他们的消息。")

        f.add_title("标记与提醒")
        self.mark_prefix = f.add_entry("标记前缀", width=6, hint="默认 ⭐，会在简报正文中出现在消息前面")
        self.notify_immediately = f.add_check(
            "命中后立即提醒", hint="不等 12:00/18:00/22:00 的定时简报"
        )
        self.notify_toast = f.add_check("使用桌面弹窗", hint="Windows 通知中心气泡")
        self.notify_clipboard = f.add_check("同时复制到剪贴板")

        f.add_title("测试")
        f.add_widget(
            "发送一条测试提醒",
            ttk.Button(f, text="立即测试提醒", command=self._test_notify),
            hint="验证桌面弹窗在你的系统上是否可用",
        )

    def _test_notify(self) -> None:
        from ..notify import notify

        cfg = self.app.current_config
        if cfg is None:
            messagebox.showwarning("尚未加载配置", "请先重新加载配置", parent=self)
            return
        title = "qqdigest 测试提醒"
        body = "如果你看到这条通知，说明桌面弹窗可用。这条是界面上的测试消息。"

        def task():
            return notify(
                title,
                body,
                use_toast=bool(self.notify_toast.get()),
                use_clipboard=bool(self.notify_clipboard.get()),
                log_file=cfg.storage.data_dir / "notices.log",
            )

        def done(result, error):
            if error:
                messagebox.showerror("测试失败", str(error), parent=self)
                return
            ok = getattr(result, "any_ok", False)
            detail = result.describe() if result else "无结果"
            if ok:
                messagebox.showinfo("测试提醒", f"已通过：{detail}", parent=self)
            else:
                messagebox.showwarning(
                    "测试提醒",
                    f"所有渠道都失败了（{detail}）。\n"
                    "如果系统禁用了通知，请在 Windows 设置里允许 PowerShell 发送通知。",
                    parent=self,
                )

        self.app.run_async(task, done)

    def load(self, cfg: Config) -> None:
        self.keywords.set(cfg.important.keywords)
        self.peers.set(sorted(cfg.important.peers))
        self.mark_prefix.delete(0, tk.END)
        self.mark_prefix.insert(0, cfg.important.mark_prefix)
        self.notify_immediately.set(cfg.important.notify_immediately)
        self.notify_toast.set(cfg.important.notify_toast)
        self.notify_clipboard.set(cfg.important.notify_clipboard)

    def collect(self) -> dict[tuple[str, str], Any]:
        return {
            ("important", "keywords"): self.keywords.get(),
            ("important", "peers"): self.peers.get(),
            ("important", "mark_prefix"): self.mark_prefix.get().strip() or "⭐",
            ("important", "notify_immediately"): bool(self.notify_immediately.get()),
            ("important", "notify_toast"): bool(self.notify_toast.get()),
            ("important", "notify_clipboard"): bool(self.notify_clipboard.get()),
        }


# ==========================================================================
# 提示词
# ==========================================================================


class PromptTab(ConfigTab):
    title = "提示词"
    description = (
        "留空 = 使用程序内置的默认提示词（推荐先跑一段时间再改）。"
        "改完点「预览最终生效提示词」可以看到实际发给模型的内容。"
    )

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, app)
        f = self.form

        f.add_title("整理简报的提示词（主提示词）")
        self.system_prompt = f.add_text_area(
            "系统提示词", height=14, hint="空 = 用内置默认。描述你希望简报长什么样、分哪几类。"
        )
        row = ttk.Frame(f)
        ttk.Button(row, text="载入内置默认", command=self._load_default_system).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(row, text="清空（用默认）", command=lambda: self._clear(self.system_prompt)).grid(
            row=0, column=1, padx=(0, 6)
        )
        ttk.Button(row, text="预览最终生效提示词", command=self._preview).grid(row=0, column=2)
        f.add_widget("", row)

        f.add_title("分块摘要的提示词")
        self.map_system_prompt = f.add_text_area(
            "摘要提示词", height=8, hint="仅在消息量超过上下文预算、需要分块处理时才会用到"
        )
        row2 = ttk.Frame(f)
        ttk.Button(row2, text="载入内置默认", command=self._load_default_map).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(row2, text="清空（用默认）", command=lambda: self._clear(self.map_system_prompt)).grid(
            row=0, column=1
        )
        f.add_widget("", row2)

        f.add_title("追加规则")
        self.extra_rules = f.add_text_area(
            "补充要求",
            height=6,
            hint="会拼到主提示词末尾。适合写个人偏好，例如「股票群只报涨跌」「金额一律标元」",
        )

    # ------------------------------------------------------------ 辅助
    @staticmethod
    def _clear(widget: tk.Text) -> None:
        widget.delete("1.0", tk.END)

    @staticmethod
    def _get(widget: tk.Text) -> str:
        return widget.get("1.0", tk.END).strip()

    def _load_default_system(self) -> None:
        self._clear(self.system_prompt)
        self.system_prompt.insert("1.0", prompts.SYSTEM_PROMPT)

    def _load_default_map(self) -> None:
        self._clear(self.map_system_prompt)
        self.map_system_prompt.insert("1.0", prompts.MAP_SYSTEM_PROMPT)

    def _preview(self) -> None:
        cfg = self.app.current_config
        if cfg is None:
            messagebox.showwarning("尚未加载配置", "请先重新加载配置", parent=self)
            return
        from dataclasses import replace

        from .. import prompts as p

        prompt_cfg = replace(
            cfg.prompt,
            system_prompt=self._get(self.system_prompt),
            extra_rules=self._get(self.extra_rules),
        )
        preview_cfg = replace(cfg, prompt=prompt_cfg)
        text = p.resolve_system_prompt(preview_cfg)

        top = tk.Toplevel(self)
        top.title("最终生效的系统提示词")
        top.geometry("760x560")
        ttk.Label(
            top,
            text=f"共 {len(text)} 字（未保存的修改也已包含在内）",
            padding=(8, 8, 8, 0),
        ).pack(anchor="w")
        box = tk.Text(top, wrap="word")
        box.pack(fill="both", expand=True, padx=8, pady=8)
        box.insert("1.0", text)
        box.configure(state="disabled")

    def load(self, cfg: Config) -> None:
        self._clear(self.system_prompt)
        self._clear(self.map_system_prompt)
        self._clear(self.extra_rules)
        self.system_prompt.insert("1.0", cfg.prompt.system_prompt)
        self.map_system_prompt.insert("1.0", cfg.prompt.map_system_prompt)
        self.extra_rules.insert("1.0", cfg.prompt.extra_rules)

    def collect(self) -> dict[tuple[str, str], Any]:
        return {
            ("prompt", "system_prompt"): self._get(self.system_prompt),
            ("prompt", "map_system_prompt"): self._get(self.map_system_prompt),
            ("prompt", "extra_rules"): self._get(self.extra_rules),
        }


# ==========================================================================
# QQ / NapCat
# ==========================================================================


class QQTab(ConfigTab):
    title = "QQ 接入"
    description = (
        "采集与发送都通过 NapCat 完成。两个地址都必须只监听 127.0.0.1，绝不能暴露到公网。"
    )

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, app)
        f = self.form

        f.add_title("账号")
        self.self_id = f.add_entry("大号 QQ（采集来源）", width=18)
        self.send_to = f.add_entry("小号 QQ（接收简报）", width=18, hint="大号和小号不能相同")

        f.add_title("事件上报（本程序作为服务端）")
        self.report_host = f.add_entry("监听地址", width=18, hint="保持 127.0.0.1")
        self.report_port = f.add_entry("监听端口", width=10)
        self.report_path = f.add_entry("路径", width=10, hint="与 NapCat「HTTP 上报」URL 的路径一致")
        self.report_token = f.add_entry("上报鉴权 Token", width=28, hint="留空 = 不校验（仅本机时可接受）")
        f.add_note("在 NapCat 里把「HTTP 上报」指向：http://127.0.0.1:<上面端口><上面路径>")

        f.add_title("接口调用（本程序作为客户端）")
        self.api_base = f.add_entry("NapCat API 地址", width=32, hint="NapCat「HTTP 服务端」地址")
        self.api_token = f.add_entry("API Token", width=28)
        self.api_timeout_sec = f.add_spin("接口超时（秒）", from_=1, to=120)

        f.add_title("发送行为")
        self.max_chars_per_message = f.add_spin(
            "单条最大字数", from_=200, to=5000, hint="超出自动分段并标注（1/N）"
        )
        self.send_gap_sec = f.add_spin("分段间隔（秒）", from_=0, to=60, hint="降低触发频率风控的可能")

        f.add_title("测试")
        f.add_widget(
            "连接 NapCat",
            ttk.Button(f, text="测试连接", command=self._test_connection),
            hint="会调用 get_login_info，确认登录账号与配置一致",
        )

    def _test_connection(self) -> None:
        from dataclasses import replace

        from ..onebot import OneBotClient, OneBotError

        cfg = self.app.current_config
        if cfg is None:
            messagebox.showwarning("尚未加载配置", "请先重新加载配置", parent=self)
            return
        onebot = replace(
            cfg.onebot,
            api_base=self.api_base.get().strip().rstrip("/"),
            api_token=self.api_token.get().strip(),
            api_timeout_sec=_int(self.api_timeout_sec, 15),
        )

        def task():
            return OneBotClient(onebot).get_login_info()

        def done(result, error):
            if error:
                messagebox.showerror(
                    "连接失败",
                    f"{error}\n\n请确认：\n"
                    "1. NapCat 已启动并登录大号\n"
                    "2. 已在 NapCat 里开启「HTTP 服务端」\n"
                    f"3. 地址填写正确：{onebot.api_base}",
                    parent=self,
                )
                return
            data = (result or {}).get("data") or {}
            got = str(data.get("user_id", ""))
            want = self.self_id.get().strip()
            msg = f"连接成功。\n\nNapCat 当前登录：{got}（{data.get('nickname', '')}）"
            if want and got != want:
                msg += f"\n\n⚠ 与配置里的大号 {want} 不一致，请检查。"
                messagebox.showwarning("连接成功但有差异", msg, parent=self)
            else:
                messagebox.showinfo("连接成功", msg, parent=self)

        self.app.run_async(task, done)

    def load(self, cfg: Config) -> None:
        pairs = [
            (self.self_id, cfg.onebot.self_id),
            (self.send_to, cfg.target.send_to),
            (self.report_host, cfg.onebot.report_host),
            (self.report_port, str(cfg.onebot.report_port)),
            (self.report_path, cfg.onebot.report_path),
            (self.report_token, cfg.onebot.report_token),
            (self.api_base, cfg.onebot.api_base),
            (self.api_token, cfg.onebot.api_token),
            (self.api_timeout_sec, str(cfg.onebot.api_timeout_sec)),
            (self.max_chars_per_message, str(cfg.target.max_chars_per_message)),
            (self.send_gap_sec, str(cfg.target.send_gap_sec)),
        ]
        for widget, value in pairs:
            widget.delete(0, tk.END)
            widget.insert(0, value)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.self_id.get().strip().isdigit():
            errors.append("【QQ 接入】大号 QQ 必须是纯数字")
        if not self.send_to.get().strip().isdigit():
            errors.append("【QQ 接入】小号 QQ 必须是纯数字")
        if self.self_id.get().strip() == self.send_to.get().strip():
            errors.append("【QQ 接入】大号与小号不能相同（会形成回环）")
        port = _int(self.report_port, 0)
        if not (1 <= port <= 65535):
            errors.append("【QQ 接入】上报端口必须在 1~65535 之间")
        if not self.report_path.get().strip().startswith("/"):
            errors.append("【QQ 接入】上报路径必须以 / 开头，例如 /")
        return errors

    def collect(self) -> dict[tuple[str, str], Any]:
        return {
            ("onebot", "self_id"): self.self_id.get().strip(),
            ("onebot", "report_host"): self.report_host.get().strip() or "127.0.0.1",
            ("onebot", "report_port"): _int(self.report_port, 5700),
            ("onebot", "report_path"): self.report_path.get().strip() or "/",
            ("onebot", "report_token"): self.report_token.get().strip(),
            ("onebot", "api_base"): self.api_base.get().strip() or "http://127.0.0.1:3000",
            ("onebot", "api_token"): self.api_token.get().strip(),
            ("onebot", "api_timeout_sec"): _int(self.api_timeout_sec, 15),
            ("target", "send_to"): self.send_to.get().strip(),
            ("target", "max_chars_per_message"): _int(self.max_chars_per_message, 1500),
            ("target", "send_gap_sec"): _float(self.send_gap_sec, 1.5),
        }


# ==========================================================================
# 大模型
# ==========================================================================


class LlmTab(ConfigTab):
    title = "大模型"
    description = (
        "本地 LM Studio 的 OpenAI 兼容接口。全程只访问本机地址，不会有任何数据出境。"
    )

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, app)
        f = self.form

        f.add_title("服务")
        self.base_url = f.add_entry("服务地址", width=34, hint="LM Studio 本地服务器地址")
        self.model = f.add_entry("模型标识", width=28, hint="例如 qwen/qwen3-14b")

        f.add_title("生成参数")
        self.max_tokens = f.add_spin(
            "最大输出 token", from_=2000, to=16384, hint="不得小于 2000：思维链与正文共享该上限"
        )
        self.temperature = f.add_spin("温度", from_=0, to=2, hint="归类任务建议 0.2，输出更稳定")
        self.input_budget_tokens = f.add_spin(
            "输入预算 token", from_=1000, to=60000, hint="超出则自动分块 map-reduce"
        )
        self.timeout_sec = f.add_spin("超时（秒）", from_=30, to=3600)
        self.max_retries = f.add_spin("失败重试次数", from_=0, to=5)
        self.no_think = f.add_check(
            "关闭思维链（/no_think）", hint="实测提速约一倍，本任务输出质量不降"
        )

        f.add_title("测试")
        row = ttk.Frame(f)
        ttk.Button(row, text="测试连接", command=self._test_connection).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(row, text="跑一次小样整合", command=self._test_digest).grid(row=0, column=1)
        f.add_widget("", row, hint="「小样整合」会用一小段模拟消息真的跑一次")

    def _client(self):
        from dataclasses import replace

        from ..llm import LlmClient

        cfg = self.app.current_config
        if cfg is None:
            return None, None
        llm_cfg = replace(
            cfg.llm,
            base_url=self.base_url.get().strip().rstrip("/"),
            model=self.model.get().strip(),
            max_tokens=_int(self.max_tokens, 3000),
            temperature=_float(self.temperature, 0.2),
            timeout_sec=_int(self.timeout_sec, 600),
            max_retries=_int(self.max_retries, 1),
            no_think=bool(self.no_think.get()),
        )
        return LlmClient(llm_cfg), llm_cfg

    def _test_connection(self) -> None:
        client, llm_cfg = self._client()
        if client is None:
            messagebox.showwarning("尚未加载配置", "请先重新加载配置", parent=self)
            return

        def task():
            return client.list_models()

        def done(result, error):
            if error:
                messagebox.showerror(
                    "连接失败",
                    f"{error}\n\n请确认 LM Studio 已启动、已加载模型、并开启了本地服务器。",
                    parent=self,
                )
                return
            models = result or []
            hit = any(llm_cfg.model in m or m in llm_cfg.model for m in models)
            msg = "可用模型：\n" + "\n".join(f"  • {m}" for m in models)
            if hit:
                messagebox.showinfo("连接成功", f"已找到配置的模型 {llm_cfg.model}\n\n{msg}", parent=self)
            else:
                messagebox.showwarning(
                    "连接成功但模型不匹配",
                    f"配置的模型：{llm_cfg.model}\n\n{msg}\n\n请把「模型标识」改成上面之一。",
                    parent=self,
                )

        self.app.run_async(task, done)

    def _test_digest(self) -> None:
        client, llm_cfg = self._client()
        if client is None:
            messagebox.showwarning("尚未加载配置", "请先重新加载配置", parent=self)
            return

        sample = (
            "【会话】张伟\n"
            "[11:02] 张伟: 下午三点的评审你来吗\n"
            "[11:40] 张伟: 对了改到 4 点，会议室换成 A 座 12 楼\n\n"
            "【会话】陈总\n"
            "[11:30] 陈总: 周五之前把季度预算表发我邮箱\n"
        )

        def task():
            import time as _t

            system = "你是我的私人消息整理助手。请把消息整理成简报，" \
                     "分【需要我回复】【待办/时间点】【值得知道】【可以忽略】四类。"
            user = ("/no_think\n" if llm_cfg.no_think else "") + sample
            started = _t.time()
            result = client.chat(system, user, max_tokens=800)
            return result, _t.time() - started

        def done(result, error):
            if error:
                messagebox.showerror("整合失败", str(error), parent=self)
                return
            res, elapsed = result
            top = tk.Toplevel(self)
            top.title("小样整合结果")
            top.geometry("700x520")
            info = (
                f"耗时 {elapsed:.1f}s ｜ prompt {res.prompt_tokens} ｜ "
                f"completion {res.completion_tokens}（reasoning {res.reasoning_tokens}）"
            )
            ttk.Label(top, text=info, padding=(8, 8, 8, 0)).pack(anchor="w")
            box = tk.Text(top, wrap="word")
            box.pack(fill="both", expand=True, padx=8, pady=8)
            box.insert("1.0", res.content or "(空)")
            box.configure(state="disabled")

        self.app.run_async(task, done)

    def load(self, cfg: Config) -> None:
        pairs = [
            (self.base_url, cfg.llm.base_url),
            (self.model, cfg.llm.model),
            (self.max_tokens, str(cfg.llm.max_tokens)),
            (self.temperature, str(cfg.llm.temperature)),
            (self.input_budget_tokens, str(cfg.llm.input_budget_tokens)),
            (self.timeout_sec, str(cfg.llm.timeout_sec)),
            (self.max_retries, str(cfg.llm.max_retries)),
        ]
        for widget, value in pairs:
            widget.delete(0, tk.END)
            widget.insert(0, value)
        self.no_think.set(cfg.llm.no_think)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _int(self.max_tokens, 0) < 2000:
            errors.append("【大模型】最大输出 token 不得小于 2000（否则正文可能为空）")
        if not self.base_url.get().strip().startswith("http"):
            errors.append("【大模型】服务地址应以 http:// 开头")
        if not self.model.get().strip():
            errors.append("【大模型】模型标识不能为空")
        return errors

    def collect(self) -> dict[tuple[str, str], Any]:
        return {
            ("llm", "base_url"): self.base_url.get().strip().rstrip("/"),
            ("llm", "model"): self.model.get().strip(),
            ("llm", "max_tokens"): _int(self.max_tokens, 3000),
            ("llm", "temperature"): _float(self.temperature, 0.2),
            ("llm", "input_budget_tokens"): _int(self.input_budget_tokens, 13000),
            ("llm", "timeout_sec"): _int(self.timeout_sec, 600),
            ("llm", "max_retries"): _int(self.max_retries, 1),
            ("llm", "no_think"): bool(self.no_think.get()),
        }


# ==========================================================================
# 会话标签与权重
# ==========================================================================


class TagsTab(ConfigTab):
    title = "群标签"
    description = (
        "给每个群打一个固定标签并赋权重，整合时按权重从高到低依次处理。"
        "权重 0 的群（例如营销群）完全不进入整合，只统计条数——这是对付"
        "「一堆闲聊把重要消息淹掉」最有效的办法。\n"
        "判定顺序：手工指定 > 关键词命中 > 本地模型识别（会带几条采样消息，比只看群名准）。"
    )

    def __init__(self, master: tk.Misc, app: Any) -> None:
        super().__init__(master, app)
        f = self.form
        self._weight_vars: dict[str, tk.StringVar] = {}

        f.add_title("总开关")
        self.enabled = f.add_check("启用标签与权重", hint="关闭后按默认顺序整合")
        self.auto_classify = f.add_check(
            "自动识别新会话", hint="用本地模型判断；关键词能命中的不调用模型"
        )
        self.exclude_zero = f.add_check(
            "权重 0 的会话不纳入整合", hint="只统计条数，在简报末尾说明"
        )
        self.use_samples = f.add_check(
            "分类时附带采样消息", hint="对「小海豹群」这类看不出名字的群很关键"
        )
        self.sample_count = f.add_spin("采样条数", from_=0, to=50)

        f.add_title("基本参数")
        self.default_label = f.add_entry(
            "默认标签", width=14, hint="判断不出来的会话落到这个标签"
        )
        self.private_weight = f.add_spin(
            "私聊权重", from_=0, to=1000, hint="私聊默认最重要，建议保持高于所有群标签"
        )

        f.add_title("标签权重")
        f.add_note("数值越大越优先整合；0 = 完全不纳入。改完点「保存配置」生效。")
        self.weights_frame = ttk.Frame(f)
        f.add_widget("", self.weights_frame)

        f.add_title("会话标签")
        row = ttk.Frame(f)
        ttk.Button(row, text="自动识别", command=self._classify).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(row, text="刷新", command=self.refresh_peers).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(row, text="重新分类全部", command=lambda: self._classify(force=True)).grid(
            row=0, column=2
        )
        f.add_widget("", row, hint="「自动识别」只处理还没有标签的会话，输出会打到「运行状态」页")

        self.peer_tree = ttk.Treeview(
            f,
            columns=("name", "kind", "msgs", "label", "weight", "source"),
            show="headings",
            height=12,
        )
        for col, text, width in (
            ("name", "会话", 220),
            ("kind", "类型", 60),
            ("msgs", "消息数", 70),
            ("label", "标签", 90),
            ("weight", "权重", 60),
            ("source", "来源", 90),
        ):
            self.peer_tree.heading(col, text=text)
            self.peer_tree.column(col, width=width, anchor="w")
        self.peer_tree.bind("<Double-1>", self._edit_peer)
        f.add_widget("", self.peer_tree, hint="双击一行可手工改标签（优先级最高）")

        self.peer_hint = ttk.Label(f, text="", style="Hint.TLabel", wraplength=760, justify="left")
        f.add_widget("", self.peer_hint)

    # ------------------------------------------------------------ 权重控件
    def _build_weight_rows(self, labels: tuple[str, ...]) -> None:
        for child in self.weights_frame.winfo_children():
            child.destroy()
        self._weight_vars.clear()
        cols = 3
        for i, label in enumerate(labels):
            r, c = divmod(i, cols)
            cell = ttk.Frame(self.weights_frame)
            ttk.Label(cell, text=label, width=8, anchor="w").grid(row=0, column=0)
            var = tk.StringVar(value="1")
            tk.Spinbox(cell, from_=0, to=1000, width=6, textvariable=var).grid(row=0, column=1)
            cell.grid(row=r, column=c, sticky="w", padx=(0, 18), pady=2)
            self._weight_vars[label] = var

    # ------------------------------------------------------------------ 数据
    def load(self, cfg: Config) -> None:
        self.enabled.set(cfg.tags.enabled)
        self.auto_classify.set(cfg.tags.auto_classify)
        self.exclude_zero.set(cfg.tags.exclude_weight_zero)
        self.use_samples.set(cfg.tags.use_sample_messages)
        for widget, value in (
            (self.sample_count, cfg.tags.sample_count),
            (self.private_weight, cfg.tags.private_weight),
        ):
            widget.delete(0, tk.END)
            widget.insert(0, str(value))
        self.default_label.delete(0, tk.END)
        self.default_label.insert(0, cfg.tags.default_label)

        self._build_weight_rows(cfg.tags.labels)
        for label, var in self._weight_vars.items():
            var.set(str(cfg.tags.weight_of(label)))
        self.refresh_peers()

    def refresh_peers(self) -> None:
        """从数据库读取会话标签（只读）。"""
        self.peer_tree.delete(*self.peer_tree.get_children())
        try:
            store = self.app.read_store()
        except Exception as exc:
            self.peer_hint.configure(text=f"读取数据库失败：{exc}")
            return
        if store is None:
            self.peer_hint.configure(text="数据库还没建立，先启动后端采集一段时间。")
            return
        try:
            rows = store.get_peer_tags()
            peers = store.list_peers()
            count = {p[0]: p[3] for p in peers}
            if not rows:
                self.peer_hint.configure(
                    text="还没有标签。点「自动识别」让本地模型给已有群打标签。"
                )
                return
            ordered = sorted(
                rows.values(),
                key=lambda r: (-int(r.get("weight") or 0), -count.get(str(r.get("peer_id")), 0)),
            )
            for r in ordered:
                pid = str(r.get("peer_id") or "")
                self.peer_tree.insert(
                    "",
                    tk.END,
                    iid=pid,
                    values=(
                        str(r.get("peer_name") or pid),
                        "私聊" if r.get("chat_type") == "private" else "群",
                        count.get(pid, 0),
                        str(r.get("label") or ""),
                        int(r.get("weight") or 0),
                        str(r.get("source") or ""),
                    ),
                )
            self.peer_hint.configure(
                text=f"共 {len(rows)} 个会话已打标签。双击可手工修改（最高优先级）。"
            )
        finally:
            store.close()

    # ------------------------------------------------------------------ 操作
    def _classify(self, force: bool = False) -> None:
        args = ["classify"] + (["--force"] if force else [])
        self.peer_hint.configure(text="正在分类…结果会输出到「运行状态」页，完成后本表自动刷新。")
        self.app.run_cli(args, on_done=lambda _c: self.after(600, self.refresh_peers))

    def _edit_peer(self, _event=None) -> None:
        selection = self.peer_tree.selection()
        if not selection:
            return
        pid = selection[0]
        current = self.peer_tree.item(pid, "values")
        cfg = self.app.current_config
        if cfg is None:
            return

        top = tk.Toplevel(self)
        top.title(f"修改标签 - {current[0]}")
        top.transient(self.winfo_toplevel())
        top.grab_set()
        ttk.Label(top, text=f"会话：{current[0]}（{pid}）", padding=(12, 12, 12, 4)).pack(anchor="w")
        ttk.Label(top, text="选择新标签（权重由「标签权重」决定）：", padding=(12, 0, 12, 4)).pack(
            anchor="w"
        )
        combo = ttk.Combobox(top, values=list(cfg.tags.labels), width=20, state="readonly")
        combo.set(str(current[3]) if current[3] in cfg.tags.labels else cfg.tags.default_label)
        combo.pack(padx=12, pady=6, anchor="w")

        def apply() -> None:
            label = combo.get()
            weight = cfg.tags.weight_of(label)
            try:
                store = self.app.write_store()
            except Exception as exc:
                messagebox.showerror("写入失败", str(exc), parent=top)
                return
            if store is None:
                messagebox.showerror("失败", "数据库不存在", parent=top)
                return
            try:
                ok = store.set_peer_label(pid, label, weight)
            finally:
                store.close()
            top.destroy()
            if ok:
                self.refresh_peers()
            else:
                messagebox.showwarning("未修改", "该会话还没有标签记录，先点「自动识别」。", parent=self)

        bar = ttk.Frame(top, padding=(12, 0, 12, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="确定", command=apply).pack(side="right")
        ttk.Button(bar, text="取消", command=top.destroy).pack(side="right", padx=(0, 6))

    # ------------------------------------------------------------------ 收集
    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.default_label.get().strip():
            errors.append("【群标签】默认标签不能为空")
        for label, var in self._weight_vars.items():
            try:
                int(var.get())
            except ValueError:
                errors.append(f"【群标签】标签「{label}」的权重不是整数：{var.get()!r}")
        return errors

    def collect(self) -> dict[tuple[str, str], Any]:
        values: dict[tuple[str, str], Any] = {
            ("tags", "enabled"): bool(self.enabled.get()),
            ("tags", "auto_classify"): bool(self.auto_classify.get()),
            ("tags", "exclude_weight_zero"): bool(self.exclude_zero.get()),
            ("tags", "use_sample_messages"): bool(self.use_samples.get()),
            ("tags", "sample_count"): _int(self.sample_count, 12),
            ("tags", "default_label"): self.default_label.get().strip() or "聊天群",
            ("tags", "private_weight"): _int(self.private_weight, 100),
        }
        for label, var in self._weight_vars.items():
            values[("tags.weights", label)] = _int(var, 1)
        return values


# ==========================================================================
# 小工具
# ==========================================================================


def _int(widget: tk.Widget, default: int) -> int:
    try:
        return int(float(str(widget.get()).strip()))
    except (ValueError, AttributeError):
        return default


def _float(widget: tk.Widget, default: float) -> float:
    try:
        return float(str(widget.get()).strip())
    except (ValueError, AttributeError):
        return default


ALL_TABS = [GeneralTab, FilterTab, ImportantTab, TagsTab, PromptTab, QQTab, LlmTab]
