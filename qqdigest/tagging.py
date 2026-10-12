"""会话标签与权重。

把每个会话归到一个**固定标签**（课程/工作/学校/家人/技术/同学/聊天群/营销…），
每个标签带一个权重。整合时按权重从高到低依次处理，权重 0 的会话不进入整合
（只统计条数），最后再汇总成一份简报。

三级判定，逐级回退（**优先级从高到低**）：

1. **手工指定**（``[tags].manual``）：你比模型更清楚哪个群是什么，永远优先；
2. **关键词规则**（``[tags].keywords``）：命中群名/会话名里的关键词，直接定标签，
   既快又可控，不消耗模型调用；
3. **本地模型分类**：把「会话名 + 采样若干条消息」交给本地大模型判断。

第 3 步之所以要带消息采样：仅凭群名对"小海豹群""某闲聊群"这类名字是无解的，
带上几条消息内容后准确率高得多。

判定结果写入 ``peer_tags`` 表缓存，之后不再重复调用模型。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import Config
from .llm import LlmClient, LlmError
from .store import Store

log = logging.getLogger("qqdigest.tagging")


@dataclass
class TagDecision:
    peer_id: str
    peer_name: str
    chat_type: str
    label: str
    source: str  # manual | keyword | model | default
    detail: str = ""


# --------------------------------------------------------------------------
# 规则判定
# --------------------------------------------------------------------------


def match_keywords(cfg: Config, name: str) -> tuple[str, str] | None:
    """按关键词规则判定。返回 ``(标签, 命中的词)``。

    支持两种写法：

    - ``"学院"`` —— 普通子串匹配；
    - ``"&-级-&"`` —— **精确匹配**：`级` 的前后必须是非中文/非字母数字的边界。

    为什么要精确匹配：实测 ``级`` 这种单字关键词会命中"某某学院2026**级**新生群"
    这种正常群名，把不该命中的群也划走。用 ``&-级-&`` 就只匹配 `级` 独立成段的场合。
    """
    if not name:
        return None
    for label, words in cfg.tags.keywords.items():
        for word in words:
            word = str(word)
            if not word:
                continue
            if word.startswith("&-") and word.endswith("-&"):
                target = word[2:-2]
                if target and _match_exact(name, target):
                    return label, word
            elif word in name:
                return label, word
    return None


#: 「词边界」判定用的字符类：中文、字母、数字都算"会粘连"的字符
_GLUE_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


def _match_exact(text: str, target: str) -> bool:
    """在 ``text`` 中查找 ``target``，且其前后不能是中文/字母/数字。"""
    start = 0
    while True:
        idx = text.find(target, start)
        if idx < 0:
            return False
        before_ok = idx == 0 or not _GLUE_RE.match(text[idx - 1])
        end = idx + len(target)
        after_ok = end >= len(text) or not _GLUE_RE.match(text[end])
        if before_ok and after_ok:
            return True
        start = idx + 1


def decide_by_rules(cfg: Config, peer_id: str, peer_name: str, chat_type: str) -> TagDecision | None:
    """只用手工指定与关键词规则判断，不调用模型。"""
    if chat_type == "private":
        return TagDecision(peer_id, peer_name, chat_type, "私聊", "manual", "私聊会话")

    manual = cfg.tags.manual.get(peer_id)
    if manual:
        return TagDecision(peer_id, peer_name, chat_type, manual, "manual", "手工指定")

    hit = match_keywords(cfg, peer_name or "")
    if hit:
        label, word = hit
        return TagDecision(peer_id, peer_name, chat_type, label, "keyword", f"命中关键词 {word!r}")
    return None


# --------------------------------------------------------------------------
# 模型判定
# --------------------------------------------------------------------------


CLASSIFY_SYSTEM = """你是一个 QQ 群分类助手。我会给你一个群的名字，\
以及从该群里采样的一些消息内容。请判断这个群属于下面哪一个标签。

可选标签（只能选一个，必须原样输出标签名）：
{labels}

判断依据（按重要性排序）：
1. 群名里的关键词（如"XX学院""XX课程""XX项目组"）；
2. 群里的消息内容在聊什么（学习/课程/工作/技术 vs 游戏/闲聊/娱乐）；
3. 如果名字看不出、内容也主要是闲聊娱乐，就选最普通的那个标签。

只输出标签名本身，不要输出任何解释、标点或额外文字。"""


def classify_by_model(
    cfg: Config,
    llm: LlmClient,
    peer_name: str,
    samples: list[str],
) -> tuple[str | None, str]:
    """调用本地模型判定标签。返回 ``(标签或None, 说明)``。"""
    labels = cfg.tags.labels
    system = CLASSIFY_SYSTEM.format(labels="\n".join(f"- {x}" for x in labels))

    parts = [f"群名：{peer_name or '(无群名)'}"]
    if cfg.tags.use_sample_messages and samples:
        parts.append("最近的消息内容：")
        for s in samples[: cfg.tags.sample_count]:
            parts.append(f"  - {s[:80]}")
    else:
        parts.append("（没有可用的消息内容，只能凭群名判断，不确定时选最普通的标签）")

    user = ("/no_think\n" if cfg.llm.no_think else "") + "\n".join(parts)
    res = llm.chat(system, user, max_tokens=64, temperature=0.0)
    text = (res.content or "").strip()

    # 容错解析：模型可能带标点、加粗、或输出整句话
    cleaned = re.sub(r"[`*\"'。，,．\s]", "", text)
    for label in labels:
        if label and label in cleaned:
            return label, f"模型判断（原输出 {text[:30]!r}）"
    for label in labels:
        if label and label in text:
            return label, f"模型判断（原输出 {text[:30]!r}）"
    return None, f"模型输出无法解析：{text[:60]!r}"


# --------------------------------------------------------------------------
# 批量分类
# --------------------------------------------------------------------------


def classify_peers(
    store: Store,
    cfg: Config,
    llm: LlmClient | None,
    *,
    force: bool = False,
    use_model: bool | None = None,
    progress=None,
) -> dict[str, Any]:
    """为数据库中已知的会话打标签（有缓存则跳过）。

    :param force: True 时忽略缓存重新判定
    :param use_model: None 表示按配置决定；False 时只用规则
    """
    if not cfg.tags.enabled:
        return {"total": 0, "skipped": 0, "manual": 0, "keyword": 0, "model": 0, "unknown": 0}

    if use_model is None:
        use_model = cfg.tags.auto_classify and llm is not None

    peers = store.list_peers()  # [(peer_id, peer_name, chat_type, count)]
    # current 始终读取；existing 用于"跳过已缓存"的判断
    current = store.get_peer_tags()
    existing = {} if force else current

    stats = {"total": len(peers), "skipped": 0, "manual": 0, "keyword": 0, "model": 0,
             "unknown": 0, "kept": 0}

    for idx, (peer_id, peer_name, chat_type, count) in enumerate(peers, 1):
        if peer_id in existing:
            stats["skipped"] += 1
            continue

        decision = decide_by_rules(cfg, peer_id, peer_name, chat_type)

        if decision is None and use_model and chat_type != "private":
            samples = store.sample_messages(peer_id, cfg.tags.sample_count)
            try:
                label, detail = classify_by_model(cfg, llm, peer_name, samples)  # type: ignore[arg-type]
            except LlmError as exc:
                log.warning("会话 %s(%s) 分类失败：%s", peer_name, peer_id, exc)
                label, detail = None, f"模型调用失败：{exc}"
            if label:
                decision = TagDecision(peer_id, peer_name, chat_type, label, "model", detail)
            else:
                stats["unknown"] += 1
                decision = TagDecision(
                    peer_id, peer_name, chat_type, cfg.tags.default_label, "default", detail
                )

        if decision is None:
            # 没有模型可用、规则也没命中：**保留原有标签**，不要用默认值覆盖。
            # 踩过的坑：`classify --force --no-model` 会把之前模型判好的标签
            # （例如识别为"营销"的群）统统刷成默认标签，等于毁掉已有数据。
            prev = current.get(peer_id)
            if prev:
                stats["kept"] += 1
                log.debug(
                    "会话 %s 保留原有标签 %s（本次未用模型，不做覆盖）",
                    peer_name or peer_id,
                    prev.get("label"),
                )
                continue
            decision = TagDecision(
                peer_id, peer_name, chat_type, cfg.tags.default_label, "default", "未判定"
            )

        weight = cfg.tags.weight_for_peer(chat_type, decision.label)
        store.upsert_peer_tag(
            peer_id=peer_id,
            peer_name=peer_name,
            chat_type=chat_type,
            label=decision.label,
            weight=weight,
            source=decision.source,
            detail=decision.detail,
            msg_count=count,
        )
        stats[decision.source] = stats.get(decision.source, 0) + 1
        log.info(
            "标签 %s（%s）-> %s [%s] 权重 %d%s",
            peer_name or peer_id,
            peer_id,
            decision.label,
            decision.source,
            weight,
            f"  {decision.detail}" if decision.detail else "",
        )
        if progress is not None:
            try:
                progress(idx, len(peers), decision)
            except Exception:
                pass

    # 标签变了，静音标记要跟着重算——否则视图和统计仍然把权重 0 的群算进去
    if cfg.tags.exclude_weight_zero:
        try:
            weights = {pid: int(r.get("weight") or 0) for pid, r in store.get_peer_tags().items()}
            muted = store.apply_mute_flags(weights, mute_weight=0)
            stats["muted"] = muted
            log.info("已标记 %d 条静音消息（权重 0 的会话）", muted)
        except Exception:
            log.exception("重算静音标记失败")

    return stats


def load_tag_weights(store: Store) -> dict[str, int]:
    """从缓存表读出 ``peer_id -> weight``。"""
    return {pid: int(row.get("weight") or 0) for pid, row in store.get_peer_tags().items()}


def load_tag_labels(store: Store) -> dict[str, str]:
    return {pid: str(row.get("label") or "") for pid, row in store.get_peer_tags().items()}


def describe(store: Store) -> str:
    """生成一份可读的标签汇总（用于命令行输出）。"""
    rows = store.get_peer_tags()
    if not rows:
        return "（还没有任何会话标签，运行 `python run.py classify` 生成）"
    by_label: dict[str, list[tuple[str, str, int]]] = {}
    for pid, row in rows.items():
        by_label.setdefault(str(row.get("label") or "未分类"), []).append(
            (pid, str(row.get("peer_name") or ""), int(row.get("weight") or 0))
        )
    lines = []
    for label, items in sorted(
        by_label.items(), key=lambda kv: -max(w for _, _, w in kv[1])
    ):
        weight = items[0][2] if items else 0
        lines.append(f"【{label}】权重 {weight}　{len(items)} 个会话")
        for pid, name, _ in items[:12]:
            lines.append(f"    - {name or '(无名)'} ({pid})")
        if len(items) > 12:
            lines.append(f"    … 另有 {len(items) - 12} 个")
    return "\n".join(lines)


__all__ = [
    "TagDecision",
    "classify_by_model",
    "classify_peers",
    "decide_by_rules",
    "describe",
    "load_tag_labels",
    "load_tag_weights",
    "match_keywords",
]
