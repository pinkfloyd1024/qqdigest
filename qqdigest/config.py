"""配置加载与校验。

设计要点：
- 使用标准库 ``tomllib`` 读取 TOML，不引入第三方依赖。
- Windows 自带的 Python 不含 IANA 时区数据库，``zoneinfo.ZoneInfo`` 会失败；
  因此 :func:`get_tz` 提供了针对常见时区的固定偏移回退，保证程序在裸环境下也能跑。
- 所有相对路径都相对于「项目根目录」解析，便于整体搬迁。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 时区
# --------------------------------------------------------------------------

#: Windows 无 IANA 数据库时的回退表（中国无夏令时，固定偏移即可精确等价）
_FALLBACK_OFFSETS: dict[str, int] = {
    "Asia/Shanghai": 8,
    "Asia/Chongqing": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "UTC": 0,
}


def get_tz(name: str) -> tzinfo:
    """返回时区对象，优先使用 zoneinfo，失败则回退到固定偏移。"""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        offset = _FALLBACK_OFFSETS.get(name)
        if offset is None:
            raise ValueError(
                f"无法解析时区 {name!r}：系统缺少 IANA 时区库（Windows 常见），"
                f"且该时区不在内置回退表中。请在 config.toml 中改用 "
                f"{sorted(_FALLBACK_OFFSETS)} 之一，或 pip install tzdata。"
            ) from None
        return timezone(timedelta(hours=offset), name)


# --------------------------------------------------------------------------
# 各配置段
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneralCfg:
    timezone: str = "Asia/Shanghai"
    dry_run: bool = True
    schedule: tuple[str, ...] = ("12:00", "18:00", "22:00")
    precompute_lead_min: int = 5
    first_run_lookback_hours: int = 12
    poll_interval_sec: int = 15
    catch_up_max_min: int = 30
    max_window_hours: int = 24
    send_retry_window_min: int = 60
    #: 调度模式：explicit = 用 schedule 列表；hourly = 工作时间内每小时一次
    schedule_mode: str = "explicit"
    work_hours_start: str = "08:00"
    work_hours_end: str = "23:00"
    #: 高权重会话（权重 >= 此值）不做每小时压缩，保留原文
    no_compress_weight: int = 8
    #: 是否在每次整合前，先把低权重会话逐群压缩存本地
    compress_low_weight: bool = True


@dataclass(frozen=True)
class OneBotCfg:
    self_id: str = ""
    report_host: str = "127.0.0.1"
    report_port: int = 5700
    report_path: str = "/"
    report_token: str = ""
    api_base: str = "http://127.0.0.1:3000"
    api_token: str = ""
    api_timeout_sec: int = 15


@dataclass(frozen=True)
class TargetCfg:
    send_to: str = ""
    max_chars_per_message: int = 1500
    send_gap_sec: float = 1.5


@dataclass(frozen=True)
class LlmCfg:
    base_url: str = "http://127.0.0.1:1234/v1"
    model: str = "qwen/qwen3-14b"
    max_tokens: int = 3000
    temperature: float = 0.3
    input_budget_tokens: int = 13000
    no_think: bool = True
    timeout_sec: int = 600
    max_retries: int = 1


@dataclass(frozen=True)
class FilterCfg:
    collect_private: bool = True
    collect_group: bool = False
    group_whitelist: tuple[str, ...] = ()
    only_incoming: bool = False
    peer_blacklist: frozenset[str] = frozenset()
    #: 自动剔除 [图片]/[表情]/纯语气词等无信息量内容
    strip_placeholders: bool = True


@dataclass(frozen=True)
class ImportantCfg:
    """重要消息规则：命中的消息会被标记并在简报中单独提示。"""

    keywords: tuple[str, ...] = ()
    peers: frozenset[str] = frozenset()
    mark_prefix: str = "⭐"
    notify_immediately: bool = False
    notify_toast: bool = True
    notify_clipboard: bool = True


@dataclass(frozen=True)
class PromptCfg:
    """提示词覆盖。留空则使用 :mod:`qqdigest.prompts` 中的内置默认值。"""

    system_prompt: str = ""
    map_system_prompt: str = ""
    extra_rules: str = ""


@dataclass(frozen=True)
class TagsCfg:
    """会话标签与权重。

    思路：把每个会话归到一个**固定标签**（课程/工作/学校/家人/技术/同学/聊天群/营销…），
    每个标签带一个权重；整合时**按权重从高到低依次处理**，权重 0 的直接不纳入整合
    （只统计条数），最后再汇总成一份简报。

    这样做的目的是解决"信号稀释"：实测中 30 条游戏群闲聊会把 2 条真正要办的私聊
    淹掉。按权重排序 + 排除 0 权重的噪音群，能让重要内容优先进入上下文。
    """

    enabled: bool = True
    auto_classify: bool = True
    labels: tuple[str, ...] = (
        "课程",
        "工作",
        "学校",
        "家人",
        "技术",
        "同学",
        "聊天群",
        "营销",
    )
    #: 标签 -> 权重（越大越优先；0 = 不纳入整合）
    #: 这里必须有实际默认值，不能留空——否则任何不经过 config.toml 的调用
    #: （测试、库调用）都会让所有标签权重退化成 1，"权重 0 排除"直接失效。
    weights: dict[str, int] = field(
        default_factory=lambda: {
            "课程": 10,
            "工作": 10,
            "家人": 9,
            "学校": 8,
            "技术": 6,
            "同学": 4,
            "聊天群": 1,
            "营销": 0,
        }
    )
    #: 标签 -> 关键词列表；命中即直接定标签，不调用模型
    keywords: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "课程": ("课程", "作业", "实验", "课设", "老师", "助教"),
            "学校": ("学院", "大学", "新生", "级", "班", "教务处", "辅导员"),
            "工作": ("项目", "需求", "会议", "部门", "周报", "上线", "排期", "客户"),
            "技术": ("技术", "开发", "编程", "开源", "算法", "ACM", "ICPC", "刷题"),
            "营销": ("优惠", "推广", "代购", "秒杀", "拼单", "返利", "广告"),
        }
    )
    #: 会话号 -> 标签；手工指定，优先级最高
    manual: dict[str, str] = field(default_factory=dict)
    default_label: str = "聊天群"
    #: 私聊会话的权重（私聊默认最重要）
    private_weight: int = 100
    #: 分类时是否附带采样消息内容（仅凭群名对"小海豹群"这类名字无解）
    use_sample_messages: bool = True
    sample_count: int = 12
    #: 是否把权重为 0 的会话排除出整合（只统计条数）
    exclude_weight_zero: bool = True
    classify_timeout_sec: int = 90

    def weight_of(self, label: str) -> int:
        """取标签权重；未配置的标签回落到 default_label 的权重。"""
        if label in self.weights:
            return self.weights[label]
        return self.weights.get(self.default_label, 1)

    def weight_for_peer(self, chat_type: str, label: str | None) -> int:
        if chat_type == "private":
            return self.private_weight
        if not label:
            return self.weight_of(self.default_label)
        return self.weight_of(label)


@dataclass(frozen=True)
class StorageCfg:
    data_dir: Path = Path("data")
    runs_dir: Path = Path("runs")
    logs_dir: Path = Path("logs")
    runs_keep_days: int = 180
    clipboard_on_dry_run: bool = True
    #: 定时发送成功后，删除该窗口内已投递的消息（手动"立即发送"永远不删）
    delete_after_scheduled_send: bool = False
    #: 权重 0（muted）消息的保留天数，0 = 永久保留。用它代替"干脆不入库"
    muted_keep_days: int = 7


@dataclass(frozen=True)
class Config:
    root: Path
    general: GeneralCfg
    onebot: OneBotCfg
    target: TargetCfg
    llm: LlmCfg
    filter: FilterCfg
    storage: StorageCfg
    important: ImportantCfg = ImportantCfg()
    prompt: PromptCfg = PromptCfg()
    tags: TagsCfg = TagsCfg()

    @property
    def tz(self) -> tzinfo:
        return get_tz(self.general.timezone)

    @property
    def db_path(self) -> Path:
        return self.storage.data_dir / "store.db"


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------

_TIME_RE = None  # 延迟初始化，避免模块级 import re


def _validate_schedule(items: tuple[str, ...]) -> None:
    import re

    global _TIME_RE
    if _TIME_RE is None:
        _TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
    if not items:
        raise ValueError("general.schedule 不能为空")
    for it in items:
        if not _TIME_RE.match(it):
            raise ValueError(f"general.schedule 中的 {it!r} 不是合法的 HH:MM 时间")


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"配置段 [{name}] 必须是一个表")
    return value


def _pick(d: dict[str, Any], key: str, default: Any, cast: type | None = None) -> Any:
    if key not in d:
        return default
    value = d[key]
    if cast is bool:
        return bool(value)
    if cast is not None and not isinstance(value, cast):
        try:
            return cast(value)
        except Exception as exc:  # pragma: no cover - 配置错误路径
            raise ValueError(f"配置项 {key}={value!r} 无法转换为 {cast.__name__}") from exc
    return value


def load_config(path: str | Path | None = None) -> Config:
    """加载并校验配置。

    :param path: config.toml 路径；为 None 时取项目根目录下的 config.toml。
    """
    if path is None:
        root = Path(__file__).resolve().parent.parent
        path = root / "config.toml"
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    root = path.parent

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    g = _section(raw, "general")
    general = GeneralCfg(
        timezone=_pick(g, "timezone", "Asia/Shanghai", str),
        dry_run=_pick(g, "dry_run", True, bool),
        schedule=tuple(_pick(g, "schedule", ["12:00", "18:00", "22:00"], list)),
        precompute_lead_min=_pick(g, "precompute_lead_min", 5, int),
        first_run_lookback_hours=_pick(g, "first_run_lookback_hours", 12, int),
        poll_interval_sec=_pick(g, "poll_interval_sec", 15, int),
        catch_up_max_min=_pick(g, "catch_up_max_min", 30, int),
        max_window_hours=_pick(g, "max_window_hours", 24, int),
        send_retry_window_min=_pick(g, "send_retry_window_min", 60, int),
        schedule_mode=_pick(g, "schedule_mode", "explicit", str),
        work_hours_start=_pick(g, "work_hours_start", "08:00", str),
        work_hours_end=_pick(g, "work_hours_end", "23:00", str),
        no_compress_weight=_pick(g, "no_compress_weight", 8, int),
        compress_low_weight=_pick(g, "compress_low_weight", True, bool),
    )
    _validate_schedule(general.schedule)
    if general.precompute_lead_min < 0:
        raise ValueError("general.precompute_lead_min 不能为负")
    if general.precompute_lead_min >= 60:
        raise ValueError("general.precompute_lead_min 建议小于 60，否则窗口会跨档错乱")

    o = _section(raw, "onebot")
    onebot = OneBotCfg(
        self_id=str(_pick(o, "self_id", "", str)).strip(),
        report_host=_pick(o, "report_host", "127.0.0.1", str),
        report_port=_pick(o, "report_port", 5700, int),
        report_path=_pick(o, "report_path", "/", str),
        report_token=_pick(o, "report_token", "", str),
        api_base=str(_pick(o, "api_base", "http://127.0.0.1:3000", str)).rstrip("/"),
        api_token=_pick(o, "api_token", "", str),
        api_timeout_sec=_pick(o, "api_timeout_sec", 15, int),
    )
    if not onebot.self_id:
        raise ValueError("onebot.self_id 不能为空（填入大号 QQ 号）")
    if not onebot.self_id.isdigit():
        raise ValueError(f"onebot.self_id 必须是纯数字 QQ 号，当前为 {onebot.self_id!r}")

    t = _section(raw, "target")
    target = TargetCfg(
        send_to=str(_pick(t, "send_to", "", str)).strip(),
        max_chars_per_message=_pick(t, "max_chars_per_message", 1500, int),
        send_gap_sec=float(_pick(t, "send_gap_sec", 1.5, float)),
    )
    if not target.send_to:
        raise ValueError("target.send_to 不能为空（填入接收简报的小号）")
    if target.send_to == onebot.self_id:
        raise ValueError(
            "target.send_to 与 onebot.self_id 相同：大号给自己发消息会形成回环，"
            "请填入另一个账号（小号）"
        )

    llm_raw = _section(raw, "llm")
    llm = LlmCfg(
        base_url=str(_pick(llm_raw, "base_url", "http://127.0.0.1:1234/v1", str)).rstrip("/"),
        model=_pick(llm_raw, "model", "qwen/qwen3-14b", str),
        max_tokens=_pick(llm_raw, "max_tokens", 3000, int),
        temperature=float(_pick(llm_raw, "temperature", 0.3, float)),
        input_budget_tokens=_pick(llm_raw, "input_budget_tokens", 13000, int),
        no_think=_pick(llm_raw, "no_think", True, bool),
        timeout_sec=_pick(llm_raw, "timeout_sec", 600, int),
        max_retries=_pick(llm_raw, "max_retries", 1, int),
    )
    if llm.max_tokens < 2000:
        raise ValueError(
            f"llm.max_tokens={llm.max_tokens} 过小。思维链 token 与正文共享该上限，"
            "实测小于 2000 会出现正文为空，请设为 2000 以上（推荐 3000）"
        )

    f = _section(raw, "filter")
    filt = FilterCfg(
        collect_private=_pick(f, "collect_private", True, bool),
        collect_group=_pick(f, "collect_group", False, bool),
        group_whitelist=tuple(str(x) for x in _pick(f, "group_whitelist", [], list)),
        only_incoming=_pick(f, "only_incoming", False, bool),
        strip_placeholders=_pick(f, "strip_placeholders", True, bool),
        peer_blacklist=frozenset(str(x) for x in _pick(f, "peer_blacklist", [], list)),
    )

    s = _section(raw, "storage")
    storage = StorageCfg(
        data_dir=root / _pick(s, "data_dir", "data", str),
        runs_dir=root / _pick(s, "runs_dir", "runs", str),
        logs_dir=root / _pick(s, "logs_dir", "logs", str),
        runs_keep_days=_pick(s, "runs_keep_days", 180, int),
        clipboard_on_dry_run=_pick(s, "clipboard_on_dry_run", True, bool),
        delete_after_scheduled_send=_pick(s, "delete_after_scheduled_send", False, bool),
        muted_keep_days=_pick(s, "muted_keep_days", 7, int),
    )

    imp = _section(raw, "important")
    important = ImportantCfg(
        keywords=tuple(
            str(x).strip() for x in _pick(imp, "keywords", [], list) if str(x).strip()
        ),
        peers=frozenset(
            str(x).strip() for x in _pick(imp, "peers", [], list) if str(x).strip()
        ),
        mark_prefix=_pick(imp, "mark_prefix", "⭐", str),
        notify_immediately=_pick(imp, "notify_immediately", False, bool),
        notify_toast=_pick(imp, "notify_toast", True, bool),
        notify_clipboard=_pick(imp, "notify_clipboard", True, bool),
    )

    pr = _section(raw, "prompt")
    prompt = PromptCfg(
        system_prompt=str(_pick(pr, "system_prompt", "", str)),
        map_system_prompt=str(_pick(pr, "map_system_prompt", "", str)),
        extra_rules=str(_pick(pr, "extra_rules", "", str)),
    )

    tg = _section(raw, "tags")
    raw_labels = [str(x).strip() for x in _pick(tg, "labels", list(TagsCfg.labels), list)]
    labels = tuple(x for x in raw_labels if x) or TagsCfg.labels
    raw_weights = _pick(tg, "weights", {}, dict)
    weights = {str(k).strip(): int(v) for k, v in raw_weights.items() if str(k).strip()}
    for label in labels:  # 未显式配置的标签给一个默认权重
        weights.setdefault(label, 1)
    raw_keywords = _pick(tg, "keywords", {}, dict)
    keywords = {
        str(k).strip(): tuple(str(x) for x in (v if isinstance(v, list) else [v]))
        for k, v in raw_keywords.items()
    }
    raw_manual = _pick(tg, "manual", {}, dict)
    manual = {str(k).strip(): str(v).strip() for k, v in raw_manual.items()}

    default_label = _pick(tg, "default_label", "聊天群", str)
    if default_label not in weights:
        weights[default_label] = weights.get(default_label, 1)
        labels = tuple(dict.fromkeys((*labels, default_label)))

    unknown = [k for k in weights if k not in labels]
    if unknown:
        raise ValueError(
            f"[tags].weights 里出现了未在 labels 中定义的标签：{unknown}。"
            f"请把标签加进 labels，或从 weights 中删掉。"
        )
    bad_kw = [k for k in keywords if k not in labels]
    if bad_kw:
        raise ValueError(f"[tags].keywords 的标签 {bad_kw} 不在 labels 中")

    tags = TagsCfg(
        enabled=_pick(tg, "enabled", True, bool),
        auto_classify=_pick(tg, "auto_classify", True, bool),
        labels=labels,
        weights=weights,
        keywords=keywords,
        manual=manual,
        default_label=default_label,
        private_weight=_pick(tg, "private_weight", 100, int),
        use_sample_messages=_pick(tg, "use_sample_messages", True, bool),
        sample_count=_pick(tg, "sample_count", 12, int),
        exclude_weight_zero=_pick(tg, "exclude_weight_zero", True, bool),
        classify_timeout_sec=_pick(tg, "classify_timeout_sec", 90, int),
    )

    cfg = Config(
        root=root,
        general=general,
        onebot=onebot,
        target=target,
        llm=llm,
        filter=filt,
        storage=storage,
        important=important,
        prompt=prompt,
        tags=tags,
    )
    ensure_dirs(cfg)
    return cfg


def ensure_dirs(cfg: Config) -> None:
    """创建运行所需目录（幂等）。"""
    for d in (cfg.storage.data_dir, cfg.storage.runs_dir, cfg.storage.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
