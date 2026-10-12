"""低权重会话的逐群预压缩。

**解决什么问题**：一个 4 小时的窗口里，一个游戏群可能有 2000 条消息，而真正重要的
私聊只有 3 条。把这些原始消息一股脑塞给模型，既慢又容易被闲聊稀释。

**怎么做**：
- **高权重会话**（权重 >= ``general.no_compress_weight``）**不压缩**，原文照进上下文
  ——重要内容不能被摘要损失掉；
- **低权重会话逐群压缩**：每个群单独交给本地模型压成要点，存进 ``compressed_blocks``；
- 整合时低权重会话用摘要、高权重会话用原文。

**什么时候跑**：不另起定时器，而是在每次**预计算之前**跑一次。每小时一档的调度下，
这就天然是"工作时间内每小时压缩一次"，而且摘要不会被放到过期——生成完立刻就用。

权重 0 的会话不参与压缩（它们根本不进上下文）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from . import prompts
from .config import Config
from .digest import group_messages, render_peer_block
from .llm import LlmClient, LlmError
from .store import Store

log = logging.getLogger("qqdigest.compress")


@dataclass
class CompressResult:
    peers: int = 0
    msg_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    skipped_high: int = 0
    skipped_muted: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"压缩 {self.peers} 个会话 / {self.msg_count} 条消息"
            f"（跳过高权重 {self.skipped_high}、静音 {self.skipped_muted}、失败 {self.failed}）"
        )


def compress_window(
    store: Store,
    cfg: Config,
    llm: LlmClient,
    *,
    window_start: int,
    window_end: int,
    progress: Callable[[int, int, str], None] | None = None,
) -> CompressResult:
    """把窗口内**低权重会话**的消息逐群压缩存库。

    高权重会话直接跳过（保留原文），权重 0 的会话也跳过（它们不进上下文）。
    """
    result = CompressResult()
    if not cfg.tags.enabled or not cfg.general.compress_low_weight:
        return result

    messages = store.fetch_messages(window_start, window_end, include_muted=False)
    if not messages:
        return result

    tags = store.get_peer_tags()
    grouped = group_messages(messages)

    # 按权重升序处理：低权重的先压，预算不够时先牺牲它们
    def peer_weight(pid: str) -> int:
        row = tags.get(pid)
        if not row:
            return cfg.tags.weight_of(cfg.tags.default_label)
        return int(row.get("weight") or 0)

    targets: list[tuple[str, list[dict[str, Any]]]] = []
    for peer_id, items in grouped.items():
        weight = peer_weight(peer_id)
        if weight <= 0:
            result.skipped_muted += 1
            continue
        if weight >= cfg.general.no_compress_weight:
            # 高权重：不压缩，原文进上下文
            result.skipped_high += 1
            continue
        targets.append((peer_id, items))

    if not targets:
        return result

    map_system = prompts.resolve_map_system_prompt(cfg)
    total = len(targets)
    for idx, (peer_id, items) in enumerate(targets, 1):
        peer_name = str(items[0].get("peer_name") or peer_id)
        block = render_peer_block(items, cfg.tz, cfg.important.mark_prefix)
        if not block:
            continue
        user = prompts.build_map_user_prompt(
            block, peer_label=peer_name, no_think=cfg.llm.no_think
        )
        try:
            res = llm.chat(map_system, user, max_tokens=min(cfg.llm.max_tokens, 900))
        except LlmError as exc:
            result.failed += 1
            log.warning("压缩会话 %s 失败：%s", peer_name, exc)
            continue
        summary = (res.content or "").strip()
        if not summary:
            result.failed += 1
            continue
        store.save_compressed_block(
            peer_id=peer_id,
            peer_name=peer_name,
            window_start=window_start,
            window_end=window_end,
            summary=summary,
            msg_count=len(items),
        )
        result.peers += 1
        result.msg_count += len(items)
        result.prompt_tokens += res.prompt_tokens
        result.completion_tokens += res.completion_tokens
        log.info(
            "压缩 [%d/%d] %s：%d 条 -> %d 字",
            idx, total, peer_name, len(items), len(summary),
        )
        if progress is not None:
            try:
                progress(idx, total, peer_name)
            except Exception:
                pass

    return result


__all__ = ["CompressResult", "compress_window"]
