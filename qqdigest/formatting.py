"""把简报正文排版成适合在 QQ 里阅读的样子。

**为什么用代码排版而不是让模型直接输出漂亮格式**：实测给 14B 模型加格式约束会
连带掉内容质量（曾经出现过整条消息消失）。所以让模型只管"分四类、写清楚"，
排版这件确定性的事交给代码做。

QQ 聊天窗口是纯文本、没有 Markdown 渲染，所以只能用字符本身做层次：
emoji 当分类图标（QQ 显示彩色 emoji），全角竖线分隔头部信息，每条一行。
"""

from __future__ import annotations

import re
from typing import Any

#: 分类图标与显示名。键是提示词里要求模型输出的标题词。
SECTION_STYLE: tuple[tuple[str, str, str], ...] = (
    ("需要我回复", "🔴", "需要我回复"),
    ("待办", "🟡", "待办 · 时间点"),
    ("值得知道", "🔵", "值得知道"),
    ("可以忽略", "⚪", "可以忽略"),
)

_TITLE_RE = re.compile(r"^【\s*([^】]+?)\s*】\s*$")
_ITEM_RE = re.compile(r"^\s*[-*·•]\s*(.+?)\s*$")
_IMPORTANT_RE = re.compile(r"^【\s*重要提醒\s*】.*$")
_SEP = "┈┈┈┈┈┈┈┈┈┈┈┈"


def _section_key(title: str) -> str | None:
    """把模型输出的标题映射到内置分类（容忍"待办 / 时间点"这类写法）。"""
    for key, _icon, _name in SECTION_STYLE:
        if key in title:
            return key
    return None


def _clean_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = _ITEM_RE.match(line)
        items.append(m.group(1) if m else line)
    return [x for x in items if x and x not in {"无", "无。", "（无）", "(无)", "暂无"}]


def split_sections(content: str) -> dict[str, list[str]]:
    """把模型输出的四段正文拆成 ``{分类: [条目]}``。"""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in (content or "").splitlines():
        m = _TITLE_RE.match(line.strip())
        if m:
            current = _section_key(m.group(1))
            if current:
                out.setdefault(current, [])
            continue
        if current:
            out[current].append(line)
    return {k: _clean_items(v) for k, v in out.items()}


def build_header(
    *,
    window_label: str = "",
    msg_count: int = 0,
    peer_count: int = 0,
    important_count: int = 0,
    kind: str = "简报",
) -> str:
    """生成头部两行。"""
    first = f"📮 消息{kind}"
    if window_label:
        first += f"　{window_label}"
    bits: list[str] = []
    if msg_count:
        bits.append(f"{msg_count} 条")
    if peer_count:
        bits.append(f"{peer_count} 个会话")
    if important_count:
        bits.append(f"⭐ {important_count} 条重要")
    second = "　".join(bits)
    return f"{first}\n{second}" if second else first


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_digest(
    content: str,
    *,
    window_label: str = "",
    msg_count: int = 0,
    peer_count: int = 0,
    important_count: int = 0,
    model: str = "",
    max_items: int = 0,
) -> str:
    """把简报正文排版成最终发送文本。

    :param max_items: 单段最多列几条，0 = 不限制
    """
    raw = content or ""
    lines_in = raw.splitlines()

    # 代码生成的【重要提醒】小节单独抽出来，不参与四分类解析
    important_lines: list[str] = []
    body_lines: list[str] = []
    in_important = False
    for line in lines_in:
        stripped = line.strip()
        if _IMPORTANT_RE.match(stripped):
            in_important = True
            continue
        if in_important:
            if not stripped:
                in_important = False
                continue
            important_lines.append(stripped)
            continue
        body_lines.append(line)

    sections = split_sections("\n".join(body_lines))
    out: list[str] = [
        build_header(
            window_label=window_label,
            msg_count=msg_count,
            peer_count=peer_count,
            important_count=important_count,
        )
    ]

    if important_lines:
        items = [re.sub(r"^[⭐\s]+", "", x) for x in important_lines if x.strip()]
        out.append("")
        out.append(_SEP)
        out.append(f"⭐ 重要提醒　{len(items)} 条")
        out.append(_SEP)
        out.extend(f"{i}. {x}" for i, x in enumerate(items, 1))

    for key, icon, name in SECTION_STYLE:
        items = sections.get(key, [])
        out.append("")
        out.append(_SEP)
        if not items:
            out.append(f"{icon} {name}　无")
            continue
        out.append(f"{icon} {name}　{len(items)} 条")
        out.append(_SEP)

        if key == "可以忽略":
            # 「可以忽略」经常几十条，压成两三行免得刷屏
            names = [x.split("：")[0].split(":")[0].strip() for x in items]
            line = "、".join(names)
            if len(line) > 90:
                head = []
                used = 0
                for n in names:
                    if used + len(n) + 1 > 80:
                        break
                    head.append(n)
                    used += len(n) + 1
                line = "、".join(head) + f" 等 {len(names)} 个会话"
            out.append(f"· {line}")
            out.append("· 详情见本地留档")
            continue

        shown = items[:max_items] if max_items else items
        for i, item in enumerate(shown, 1):
            out.append(f"{i}. {_shorten(item, 120)}")
        if len(items) > len(shown):
            out.append(f"… 另有 {len(items) - len(shown)} 条")

    if model:
        out.append("")
        out.append(f"— 共 {msg_count} 条 · {model}" if msg_count else f"— {model}")
    return "\n".join(out).strip()


def format_failure(slot: str, reason: str) -> str:
    """整合失败时发的简短通知。"""
    return (
        "⚠️ 消息简报未生成\n"
        f"档位：{slot.replace('T', ' ')}\n"
        f"原因：{reason[:180]}\n"
        "原始消息已保存在本地，下次运行会一并处理。"
    )


__all__ = [
    "SECTION_STYLE",
    "build_header",
    "format_digest",
    "format_failure",
    "split_sections",
]
