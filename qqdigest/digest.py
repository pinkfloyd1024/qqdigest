"""整合编排：把暂存消息渲染成提示词，调用本地模型，产出简报并落档。

上下文策略（两级）：
1. 渲染后的文本在预算内 -> 一次调用直接出简报；
2. 超出预算 -> 按会话分块做 map（逐块压缩要点），再 reduce 汇总成简报。
   若压缩后仍然超预算，按会话重要度截断（重要 > @我 > 活跃度），
   并在简报末尾注明未纳入的条数。

提示词可由 ``config.toml`` 的 ``[prompt]`` 段覆盖，见 :mod:`qqdigest.prompts`。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from . import prompts
from .config import Config
from .llm import LlmClient, estimate_tokens
from .store import Store
from .util import fmt_hm, fmt_window

log = logging.getLogger("qqdigest.digest")

#: 渲染正文之外的开销预留（表头、格式说明等），单位 token
_EXTRA_OVERHEAD = 200


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def _peer_label(chat_type: str, peer_id: str, peer_name: str) -> str:
    if chat_type == "group":
        return f"群·{peer_name or peer_id}"
    return peer_name or peer_id


def group_messages(messages: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按会话分组，保持组内时间升序。"""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in messages:
        grouped[str(m.get("peer_id") or "")].append(m)
    for items in grouped.values():
        items.sort(key=lambda x: int(x.get("ts") or 0))
    return dict(grouped)


def render_peer_block(items: list[dict[str, Any]], tz, mark: str = "⭐") -> str:
    """把一个会话渲染成文本块。重要消息会加上标记前缀。

    **发言者标注是"分不清你我"这个 bug 的第一道防线。**

    实测问题：别人说"我明天去交材料"，模型会写成"我需要明天去交材料"——
    因为四个分类（需要我回复/待办/值得知道）天生是第一人称视角，
    模型会把**别人**的意图改写成**我**的意图。

    这里做两件事：
    1. 我的消息一律标注成 `我(本人)`，别人的消息用昵称，视觉上不可能混淆；
    2. 群聊里额外带上 QQ 号，避免同昵称多人。
    """
    if not items:
        return ""
    first = items[0]
    label = _peer_label(
        str(first.get("chat_type") or ""),
        str(first.get("peer_id") or ""),
        str(first.get("peer_name") or ""),
    )
    lines = [f"【会话】{label}"]
    for m in items:
        if int(m.get("direction") or 0) == 1:
            who = "我(本人)"
        else:
            name = str(m.get("sender_name") or "").strip()
            sid = str(m.get("sender_id") or "").strip()
            if not name:
                who = f"对方({sid})" if sid else "对方"
            elif sid and str(m.get("chat_type")) == "group":
                who = f"{name}({sid})"
            else:
                who = name
        text = str(m.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        prefix = f"{mark} " if int(m.get("important") or 0) else ""
        lines.append(f"[{fmt_hm(int(m.get('ts') or 0), tz)}] {who}: {prefix}{text}")
    # 一条可用内容都没有时，不要留下一个只有标题的孤立块
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def ordered_peer_blocks(
    grouped: dict[str, list[dict[str, Any]]],
    tz,
    mark: str = "⭐",
    weights: dict[str, int] | None = None,
    summaries: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """按重要度排序的会话块列表。

    排序键：**重要 > 标签权重 > 私聊优先于群聊 > @我/带问号 > 消息条数**。

    权重来自 :mod:`qqdigest.tagging`（课程/工作/家人 高，聊天群 低，营销 0）。
    这是解决"信号稀释"的关键：实测某窗口 32 条消息里 30 条来自游戏群，
    唯一需要行动的两条私聊被排到最后，模型读完整屏闲聊后把它们也丢进了
    「可以忽略」。**排序不能只按音量，要按信号强度。**

    ``weights`` 为空时（尚未做标签分类）自动回退到后面的键，行为与旧版一致。
    这个顺序同时决定了两件事：简报里的呈现顺序，以及超预算时截断的优先级。
    """
    weights = weights or {}
    summaries = summaries or {}
    blocks: list[dict[str, Any]] = []
    for items in grouped.values():
        chat_type = str(items[0].get("chat_type") or "")
        peer_id = str(items[0].get("peer_id") or "")
        label = _peer_label(
            chat_type, peer_id, str(items[0].get("peer_name") or "")
        )
        # 低权重会话用预压缩摘要替代原文（见 qqdigest.compress）
        summary = summaries.get(peer_id)
        if summary:
            block = f"【会话】{label}（预压缩摘要，原文 {len(items)} 条）\n{summary}"
        else:
            block = render_peer_block(items, tz, mark)
        if not block:
            continue
        blocks.append(
            {
                "label": label,
                "block": block,
                "compressed": bool(summary),
                "chat_type": chat_type,
                "peer_id": peer_id,
                "weight": int(weights.get(peer_id, 0)),
                "important": sum(int(m.get("important") or 0) for m in items),
                "highlight": sum(int(m.get("highlight") or 0) for m in items),
                "count": len(items),
                "items": items,
            }
        )
    blocks.sort(
        key=lambda b: (
            -b["important"],
            -b["weight"],
            0 if b["chat_type"] == "private" else 1,
            -b["highlight"],
            -b["count"],
        )
    )
    return blocks


def render_transcript_ex(
    grouped: dict[str, list[dict[str, Any]]],
    tz,
    mark: str = "⭐",
    weights: dict[str, int] | None = None,
    exclude_weight_zero: bool = False,
    summaries: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """渲染正文，并返回被排除会话的统计。

    :return: ``(文本, {"excluded_peers": n, "excluded_msgs": m, "excluded_labels": [...]})``

    权重为 0 的会话（营销群、无关闲聊群）默认整段排除，只统计条数并在简报末尾
    用一行说明——这比"排到最后再被截断"更干净，预算全留给有用的内容。
    """
    blocks = ordered_peer_blocks(grouped, tz, mark, weights, summaries)

    excluded: list[dict[str, Any]] = []
    if exclude_weight_zero:
        kept: list[dict[str, Any]] = []
        for b in blocks:
            (excluded if b["weight"] <= 0 else kept).append(b)
    else:
        kept = blocks

    private = [b for b in kept if b["chat_type"] == "private"]
    groups = [b for b in kept if b["chat_type"] != "private"]

    parts: list[str] = []
    if private:
        parts.append(f"===== 【私聊消息】（{len(private)} 个会话）=====")
        parts.extend(b["block"] for b in private)
    if groups:
        parts.append(f"===== 【群聊消息】（{len(groups)} 个会话，按重要度从高到低）=====")
        parts.extend(b["block"] for b in groups)

    info = {
        "excluded_peers": len(excluded),
        "excluded_msgs": sum(b["count"] for b in excluded),
        "excluded_labels": sorted({b["label"] for b in excluded}),
    }
    return "\n\n".join(parts), info


def render_transcript(
    grouped: dict[str, list[dict[str, Any]]],
    tz,
    mark: str = "⭐",
    weights: dict[str, int] | None = None,
    exclude_weight_zero: bool = False,
    summaries: dict[str, str] | None = None,
) -> str:
    """渲染完整正文，并把**私聊与群聊分成两段**。

    分段不只是好看：它让模型看到结构化的输入，而不是一锅粥。
    实测在既有 30 条群聊又有 2 条私聊的窗口里，"私聊段"这一层显式分隔
    能显著降低"私聊被当成闲聊丢掉"的概率。
    """
    text, _ = render_transcript_ex(grouped, tz, mark, weights, exclude_weight_zero, summaries)
    return text


def build_important_header(
    messages: list[dict[str, Any]], tz, mark: str = "⭐", limit: int = 20
) -> str:
    """由**代码**生成「【重要提醒】」小节。

    为什么不用模型生成：实测发现把「重要提醒」规则写进系统提示词后，
    14B 模型在四分类上会丢内容（出现过整条消息被漏掉）。这里直接在程序里
    依据 ``important`` 标记拼装，结果确定、不会漏、也不会编造。

    超过 ``limit`` 条时只列前若干条，并注明其余已并入下方分类。
    """
    flagged = [m for m in messages if int(m.get("important") or 0)]
    if not flagged:
        return ""
    flagged.sort(key=lambda m: int(m.get("ts") or 0))

    lines = [f"【重要提醒】（共 {len(flagged)} 条）"]
    seen: set[tuple[str, str]] = set()
    shown = 0
    for m in flagged:
        key = (str(m.get("peer_id") or ""), str(m.get("text") or ""))
        if key in seen:
            continue
        seen.add(key)
        if int(m.get("direction") or 0) == 1:
            who = "我"
        else:
            who = str(m.get("sender_name") or "").strip() or str(m.get("peer_name") or "").strip()
            who = who or str(m.get("peer_id") or "")
        text = str(m.get("text") or "").replace("\n", " ").strip()
        lines.append(f"{mark} [{fmt_hm(int(m.get('ts') or 0), tz)}] {who}: {text}")
        shown += 1
        if shown >= limit:
            rest = len(flagged) - shown
            if rest > 0:
                lines.append(f"（其余 {rest} 条重要消息已并入下方分类）")
            break
    return "\n".join(lines)


def _split_block(block: str, max_chars: int) -> list[str]:
    """把超长会话块按行切开，保留会话头。"""
    lines = block.split("\n")
    if len(lines) <= 1:
        return [block]
    header = lines[0]
    body = lines[1:]
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in body:
        if cur and cur_len + len(line) > max_chars:
            out.append(header + "\n" + "\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        out.append(header + "\n" + "\n".join(cur))
    return out


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _write_run_file(cfg: Config, slot: str, content: str, meta: dict[str, Any]) -> str:
    """把简报落档到 runs/，返回相对路径。"""
    stamp = slot.replace(":", "").replace("T", "_")
    cfg.storage.runs_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.storage.runs_dir / f"{stamp}.md"
    tokens_line = (
        f"- token：prompt {meta.get('prompt_tokens', 0)} / "
        f"completion {meta.get('completion_tokens', 0)}"
    )
    if meta.get("reasoning_tokens"):
        tokens_line += f"（其中 reasoning {meta['reasoning_tokens']}）"
    lines = [
        f"# 消息简报 {slot.replace('T', ' ')}",
        "",
        f"- 时间范围：{meta.get('window_desc', '')}",
        f"- 消息条数：{meta.get('msg_count', 0)}",
        f"- 会话数：{meta.get('peer_count', 0)}",
        f"- 其中重要：{meta.get('important_count', 0)}",
        f"- 模型：{meta.get('model', '')}",
        tokens_line,
        f"- 是否分块：{'是' if meta.get('chunked') else '否'}",
        f"- 提示词：{'自定义' if meta.get('custom_prompt') else '内置默认'}",
        "",
        "---",
        "",
        content,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    try:
        return str(path.relative_to(cfg.root))
    except ValueError:
        return str(path)


def run_digest(
    store: Store,
    cfg: Config,
    llm: LlmClient,
    *,
    slot: str,
    window_start: int,
    window_end: int,
    fire_at: int,
) -> int | None:
    """生成一档简报。``slot`` 已存在则直接返回 None（防重复）。"""
    digest_id = store.create_digest(slot, window_start, window_end, fire_at)
    if digest_id is None:
        log.info("档位 %s 已存在，跳过", slot)
        return None

    mark = cfg.important.mark_prefix or "⭐"
    system_prompt = prompts.resolve_system_prompt(cfg)
    window_desc = fmt_window(window_start, window_end, cfg.tz)
    messages = store.fetch_messages(
        window_start, window_end, only_incoming=cfg.filter.only_incoming
    )
    important_count = sum(int(m.get("important") or 0) for m in messages)
    log.info(
        "档位 %s：窗口 %s，取到 %d 条消息（其中重要 %d 条）",
        slot,
        window_desc,
        len(messages),
        important_count,
    )

    if not messages:
        _finalize(
            store,
            cfg,
            digest_id,
            slot,
            "本时段没有新消息。",
            meta={
                "window_desc": window_desc,
                "msg_count": 0,
                "peer_count": 0,
                "important_count": 0,
                "model": cfg.llm.model,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "chunked": False,
                "custom_prompt": not prompts.is_using_default_system_prompt(cfg),
            },
        )
        return digest_id

    grouped = group_messages(messages)

    # 会话标签权重：决定整合顺序，权重 0 的会话直接不进入上下文
    weights: dict[str, int] = {}
    if cfg.tags.enabled:
        from .tagging import load_tag_weights

        weights = load_tag_weights(store)
        if not weights:
            log.info(
                "尚未做会话标签分类，将按默认顺序整合。"
                "可在界面「群标签」页或运行 `python run.py classify` 生成标签。"
            )
    # 低权重会话的预压缩摘要（由 qqdigest.compress 在整合前生成）
    summaries: dict[str, str] = {}
    if cfg.general.compress_low_weight:
        try:
            summaries = store.get_compressed_summaries(window_start, window_end)
            if summaries:
                log.info("采用 %d 个会话的预压缩摘要", len(summaries))
        except Exception:
            log.exception("读取预压缩摘要失败，改用原文")

    transcript, excluded = render_transcript_ex(
        grouped, cfg.tz, mark, weights, cfg.tags.exclude_weight_zero, summaries
    )
    if excluded["excluded_peers"]:
        log.info(
            "按标签权重排除了 %d 个会话 / %d 条消息（标签：%s）",
            excluded["excluded_peers"],
            excluded["excluded_msgs"],
            "、".join(excluded["excluded_labels"]) or "未分类",
        )
    excluded_note = ""
    if excluded["excluded_peers"]:
        excluded_note = (
            f"另有 {excluded['excluded_peers']} 个低优先级会话"
            f"（{ '、'.join(excluded['excluded_labels']) or '未标注' }）"
            f"共 {excluded['excluded_msgs']} 条消息未纳入整理，"
            f"请在简报末尾用一行说明。"
        )

    # 预算 = 总上下文 - 输出预留 - 系统提示词与模板开销
    overhead = estimate_tokens(system_prompt) + _EXTRA_OVERHEAD
    budget = cfg.llm.input_budget_tokens - cfg.llm.max_tokens - overhead
    if budget < 1000:
        log.warning(
            "上下文预算过小（%d），已按最小值 1000 处理；"
            "请检查 llm.input_budget_tokens / max_tokens 与提示词长度",
            budget,
        )
        budget = 1000

    est = estimate_tokens(transcript)
    chunked = False
    prompt_tokens = completion_tokens = reasoning_tokens = 0
    model = cfg.llm.model

    try:
        if est <= budget:
            user = prompts.build_digest_user_prompt(
                transcript,
                window_desc=window_desc,
                msg_count=len(messages),
                peer_count=len(grouped),
                no_think=cfg.llm.no_think,
                excluded_note=excluded_note,
                weighted=bool(weights),
            )
            log.info("单次整合：估算 %d tokens（预算 %d）", est, budget)
            result = llm.chat(system_prompt, user)
            content = result.content
            prompt_tokens, completion_tokens = result.prompt_tokens, result.completion_tokens
            reasoning_tokens = result.reasoning_tokens
            model = result.model
        else:
            chunked = True
            log.info("分块整合：估算 %d tokens 超出预算 %d", est, budget)
            content, usage = _map_reduce(
                cfg,
                llm,
                grouped,
                window_desc,
                len(messages),
                budget,
                system_prompt,
                mark,
                weights,
                excluded_note,
                summaries,
            )
            prompt_tokens = usage["prompt_tokens"]
            completion_tokens = usage["completion_tokens"]
            reasoning_tokens = usage["reasoning_tokens"]
            model = usage["model"]
    except Exception as exc:
        store.mark_failed(digest_id, str(exc))
        log.error("档位 %s 整合失败：%s", slot, exc)
        raise

    # 「【重要提醒】」由代码确定性拼装，置于简报最前面
    header = build_important_header(messages, cfg.tz, mark)
    if header:
        content = f"{header}\n\n{content}"
        log.info("已在简报前插入重要提醒小节（%d 条）", important_count)

    meta = {
        "window_desc": window_desc,
        "msg_count": len(messages),
        "peer_count": len(grouped),
        "important_count": important_count,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "chunked": chunked,
        "custom_prompt": not prompts.is_using_default_system_prompt(cfg),
    }
    _finalize(store, cfg, digest_id, slot, content, meta=meta)
    return digest_id


def _finalize(
    store: Store,
    cfg: Config,
    digest_id: int,
    slot: str,
    content: str,
    *,
    meta: dict[str, Any],
) -> None:
    run_file = ""
    try:
        run_file = _write_run_file(cfg, slot, content, meta)
    except Exception as exc:
        log.warning("写留档文件失败：%s", exc)
    store.finish_digest(
        digest_id,
        content=content,
        model=str(meta.get("model") or ""),
        prompt_tokens=int(meta.get("prompt_tokens") or 0),
        completion_tokens=int(meta.get("completion_tokens") or 0),
        msg_count=int(meta.get("msg_count") or 0),
        run_file=run_file,
        state="pending",
    )
    log.info("档位 %s 简报已生成（%d 字），留档：%s", slot, len(content), run_file or "（未写）")


def _pack_blocks(
    entries: list[dict[str, Any]], map_budget: int
) -> list[tuple[list[str], str]]:
    """把多个会话块**打包**进同一次模型调用。

    每个会话一次调用会随着会话数线性变慢（实测 48 个会话要 226 秒）。
    打包后调用次数大幅下降，速度提升数倍，同时信息不丢。
    """
    packs: list[tuple[list[str], str]] = []
    labels: list[str] = []
    texts: list[str] = []
    used = 0

    for entry in entries:
        block = entry["block"]
        # 单个块本身就超预算 -> 先按行切开
        pieces = (
            _split_block(block, prompts.CHUNK_SPLIT_CHARS)
            if estimate_tokens(block) > map_budget
            else [block]
        )
        for piece in pieces:
            cost = estimate_tokens(piece)
            if texts and used + cost > map_budget:
                packs.append((labels, "\n\n".join(texts)))
                labels, texts, used = [], [], 0
            labels.append(entry["label"])
            texts.append(piece)
            used += cost

    if texts:
        packs.append((labels, "\n\n".join(texts)))
    return packs


def _merge_until_fits(
    cfg: Config,
    llm: LlmClient,
    summaries: list[str],
    budget: int,
    map_system: str,
    usage: dict[str, Any],
) -> tuple[list[str], int]:
    """多轮归并，把摘要总长度压进预算。

    **不能像早期实现那样直接丢弃超出的块**——那会让大部分会话的内容永久消失
    （实测 48 块里丢掉了 39 块）。这里改为逐轮把若干条摘要合并重写，
    信息被逐步浓缩而不是扔掉。返回 ``(摘要列表, 轮数)``。
    """
    rounds = 0
    max_rounds = 4
    while rounds < max_rounds and len(summaries) > 1:
        merged = "\n\n".join(summaries)
        total = estimate_tokens(merged)
        if total <= budget:
            break
        rounds += 1
        # 按超预算倍数决定归并分组大小，目标是砍掉一半以上
        ratio = max(2, (total + budget - 1) // budget)
        per_group = max(2, len(summaries) // ratio + 1)
        new_summaries: list[str] = []
        for start in range(0, len(summaries), per_group):
            chunk = summaries[start : start + per_group]
            if len(chunk) == 1:
                new_summaries.append(chunk[0])
                continue
            user = prompts.build_map_user_prompt(
                "\n\n".join(chunk),
                peer_label=f"第 {rounds} 轮归并（{len(chunk)} 个会话的要点）",
                no_think=cfg.llm.no_think,
            )
            res = llm.chat(map_system, user, max_tokens=min(cfg.llm.max_tokens, 1500))
            usage["prompt_tokens"] += res.prompt_tokens
            usage["completion_tokens"] += res.completion_tokens
            usage["reasoning_tokens"] += res.reasoning_tokens
            usage["model"] = res.model
            new_summaries.append(res.content)
        log.info(
            "归并第 %d 轮：%d 条摘要 -> %d 条（%d tokens，预算 %d）",
            rounds,
            len(summaries),
            len(new_summaries),
            total,
            budget,
        )
        summaries = new_summaries
    return summaries, rounds


def _map_reduce(
    cfg: Config,
    llm: LlmClient,
    grouped: dict[str, list[dict[str, Any]]],
    window_desc: str,
    msg_count: int,
    budget: int,
    system_prompt: str,
    mark: str,
    weights: dict[str, int] | None = None,
    excluded_note: str = "",
    summaries: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """分块压缩再汇总。返回 ``(简报正文, usage)``。"""
    usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "model": cfg.llm.model,
    }
    map_system = prompts.resolve_map_system_prompt(cfg)
    # 打包得越满，模型调用次数越少、总耗时越低（每次调用都要重发系统提示词）。
    # 取预算的 0.7，兼顾速度与"信号稀释"（块太大时闲聊会淹没个别要点）。
    map_budget = max(2000, int(budget * 0.7))
    # map 的输出被提示词限制在 150 字左右，这里给足余量避免被截断丢信息
    map_max_tokens = min(cfg.llm.max_tokens, 1200)

    # 按「重要 > 标签权重 > 私聊 > @我」排序后再切块
    # —— 归并时优先保留前面的；权重 0 的会话不进入上下文
    entries = ordered_peer_blocks(grouped, cfg.tz, mark, weights, summaries)
    if cfg.tags.enabled and cfg.tags.exclude_weight_zero:
        entries = [e for e in entries if e["weight"] > 0]
    packs = _pack_blocks(entries, map_budget)
    log.info(
        "分块整合：%d 个会话 -> %d 个打包块（map 预算 %d tokens）",
        len(entries),
        len(packs),
        map_budget,
    )

    summaries: list[str] = []
    for idx, (labels, text) in enumerate(packs, 1):
        label = "、".join(dict.fromkeys(labels))
        user = prompts.build_map_user_prompt(
            text, peer_label=label, no_think=cfg.llm.no_think
        )
        res = llm.chat(map_system, user, max_tokens=map_max_tokens)
        usage["prompt_tokens"] += res.prompt_tokens
        usage["completion_tokens"] += res.completion_tokens
        usage["reasoning_tokens"] += res.reasoning_tokens
        usage["model"] = res.model
        summaries.append(f"【{label}】\n{res.content}")
        log.debug("map %d/%d 完成：%s", idx, len(packs), label)

    # 多轮归并压缩（而不是丢弃）
    summaries, rounds = _merge_until_fits(cfg, llm, summaries, budget, map_system, usage)

    merged = "\n\n".join(summaries)
    dropped = 0
    if estimate_tokens(merged) > budget:
        # 兜底：归并到极限仍超预算，按顺序截断并如实统计
        kept: list[str] = []
        used = 0
        for block in summaries:
            t = estimate_tokens(block)
            if used + t > budget:
                break
            kept.append(block)
            used += t
        dropped = len(summaries) - len(kept)
        merged = "\n\n".join(kept)
        log.warning(
            "归并 %d 轮后仍超预算，截断保留 %d/%d 条摘要（丢弃 %d 条）",
            rounds,
            len(kept),
            len(summaries),
            dropped,
        )

    user = prompts.build_reduce_user_prompt(
        merged,
        window_desc=window_desc,
        msg_count=msg_count,
        peer_count=len(grouped),
        dropped=dropped,
        no_think=cfg.llm.no_think,
        excluded_note=excluded_note,
        weighted=bool(weights),
    )
    res = llm.chat(system_prompt, user)
    usage["prompt_tokens"] += res.prompt_tokens
    usage["completion_tokens"] += res.completion_tokens
    usage["reasoning_tokens"] += res.reasoning_tokens
    usage["model"] = res.model
    return res.content, usage
