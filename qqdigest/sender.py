"""发送层：把简报发给小号。

两种模式：
- ``dry_run = true``：不碰 QQ，只写日志 + 复制到剪贴板（留档文件已由 digest 层写出）；
- ``dry_run = false``：按长度分段，通过 OneBot API 逐段发送，段间留出间隔。

失败一律不抛异常到调用方之外，改为返回 ``(成功与否, 说明)``，避免一次发送失败影响调度循环。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .config import Config
from .formatting import format_digest, format_failure
from .onebot import OneBotClient, OneBotError
from .util import clipboard_set

log = logging.getLogger("qqdigest.sender")


def split_text(text: str, limit: int) -> list[str]:
    """按段落/行切分文本，尽量不切断句子。"""
    if limit <= 0:
        return [text]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    # 先按段落切，段落过长再按行切，行仍过长则硬切
    for para in text.split("\n"):
        pieces = [para]
        if len(para) > limit:
            pieces = []
            line_buf = ""
            for line in para.splitlines(keepends=False) or [para]:
                if len(line) > limit:
                    if line_buf:
                        pieces.append(line_buf)
                        line_buf = ""
                    for i in range(0, len(line), limit):
                        pieces.append(line[i : i + limit])
                else:
                    if len(line_buf) + len(line) + 1 > limit:
                        pieces.append(line_buf)
                        line_buf = line
                    else:
                        line_buf = f"{line_buf}\n{line}" if line_buf else line
            if line_buf:
                pieces.append(line_buf)

        for piece in pieces:
            candidate = f"{buf}\n{piece}" if buf else piece
            if len(candidate) > limit:
                if buf:
                    chunks.append(buf)
                buf = piece
            else:
                buf = candidate
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def send_text(cfg: Config, text: str, *, label: str = "简报", force: bool = False) -> tuple[bool, str]:
    """发送一段文本。返回 ``(是否成功, 说明)``。

    :param force: 即使配置里开着干跑模式，也真的发出去（用于界面上的
        「立即整合并发送」以及 ``digest-now --force-send``）。
        **没有这个参数时，干跑模式就是硬拦截**——早期实现只在 CLI 层判了
        dry_run，函数内部又拦了一道，导致 --force-send 完全不生效。
    """
    if cfg.general.dry_run and not force:
        if cfg.storage.clipboard_on_dry_run:
            ok = clipboard_set(text)
            clip = "已复制到剪贴板" if ok else "复制剪贴板失败"
        else:
            clip = "未启用剪贴板"
        log.info("[干跑] %s 未发送到 QQ（%s），长度 %d 字", label, clip, len(text))
        return True, f"dry-run（{clip}）"

    chunks = split_text(text, cfg.target.max_chars_per_message)
    client = OneBotClient(cfg.onebot)
    sent = 0
    try:
        for idx, chunk in enumerate(chunks, 1):
            prefix = f"（{idx}/{len(chunks)}）\n" if len(chunks) > 1 else ""
            client.send_private_msg(cfg.target.send_to, prefix + chunk)
            sent += 1
            if idx < len(chunks):
                import time as _t

                _t.sleep(cfg.target.send_gap_sec)
    except OneBotError as exc:
        return False, f"发送到 {cfg.target.send_to} 失败（已发出 {sent}/{len(chunks)} 段）：{exc}"
    return True, f"已发送 {sent} 段到 {cfg.target.send_to}"


def _manual_window_label(cfg: Config, digest: dict[str, Any]) -> str:
    """手动发送时的时段标签：用**实际数据范围**，而不是裸露的档位 ID。

    档位 ID 形如 ``manual-20261013-001034``，发到 QQ 上又长又看不懂。
    """
    start = int(digest.get("window_start") or 0)
    end = int(digest.get("window_end") or 0)
    if not start or not end:
        return "手动整合"
    fmt = "%m-%d %H:%M"
    a = datetime.fromtimestamp(start, cfg.tz).strftime(fmt)
    b = datetime.fromtimestamp(end, cfg.tz).strftime(fmt)
    return f"手动 {a} → {b}"


def send_digest(cfg: Config, digest: dict[str, Any], *, force: bool = False) -> tuple[bool, str]:
    """发送一条简报记录。

    正文会经 :func:`qqdigest.formatting.format_digest` 排版：加 emoji 分类图标、
    条目编号、把「可以忽略」压成一行。留档文件里保存的仍是模型原始输出。
    """
    content = str(digest.get("content") or "").strip()
    slot = str(digest.get("slot") or "")
    if not content:
        return False, "简报内容为空，跳过发送"

    window_label = str(digest.get("window_label") or "")
    if not window_label:
        window_label = _manual_window_label(cfg, digest)

    text = format_digest(
        content,
        window_label=window_label,
        msg_count=int(digest.get("msg_count") or 0),
        peer_count=int(digest.get("peer_count") or 0),
        important_count=int(digest.get("important_count") or 0),
        model=str(digest.get("model") or ""),
    )
    return send_text(cfg, text, label=f"简报 {window_label}", force=force)


def send_failure_notice(cfg: Config, slot: str, reason: str) -> tuple[bool, str]:
    """整合失败时发一条简短通知，避免你什么都不收到。"""
    return send_text(
        cfg, format_failure(slot, reason), label=f"失败通知 {slot}"
    )
