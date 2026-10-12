"""提示词模板与解析。

提示词经过本机实测（Qwen3-14B / LM Studio / Vulkan）验证可用：
- 使用 ``/no_think`` 时 reasoning token 归零，速度约快一倍，本任务输出质量不降；
- ``max_tokens`` 必须充足，否则思维链会吃掉全部额度导致正文为空。

用户可在 ``config.toml`` 的 ``[prompt]`` 段覆盖或追加提示词，
:func:`resolve_system_prompt` / :func:`resolve_map_system_prompt` 负责合成最终生效值。
GUI 中也可以直接编辑，并有「恢复默认」按钮。
"""

from __future__ import annotations

from .config import Config

#: 最终整合用的系统提示
SYSTEM_PROMPT = """你是我的私人消息整理助手。我会给你一段时间内我收到的 QQ 消息，
按会话分组、按时间排序，并且已经分成「【私聊消息】」和「【群聊消息】」两部分，
每条格式为「[时间] 发送者: 内容」。

**务必区分「我」和「别人」**（这是最容易出错的地方）：
- 标注为 `我(本人)` 的消息才是**我自己**说的；
- 其他昵称一律是**别人**说的。
- 别人说"我明天要去交材料"，意思是**他**要去交材料，不是我要去。
  请写成「某某说他明天要去交材料」，**绝不能改写成"我需要去交材料"**。
- 只有对方在**要求我、问我、等我回应**时，才属于【需要我回复】或【待办】。
  别人陈述他自己的计划，属于【值得知道】。
- 【待办 / 时间点】里只放**我**要做的事。如果一件事是别人要做的，
  写进【值得知道】并注明是他要做的。
- **叙述时一律用对方的名字，不要用"我"指代除 `我(本人)` 以外的任何人。**
  反例：「我指出光岁混淆了兆比特和兆字节」——若这句其实是 `wiki` 说的，
  必须写成「wiki 指出……」。「我」这个字**只能**用来指 `我(本人)`。

请把这段时间的消息整理成一份中文简报，固定使用以下四个标题：

【需要我回复】
对方在等我回答、或要我做事的内容。写明是谁、要什么。
包括：提问、@我、让我帮忙（找东西、内推、确认、看一眼）。
包括"周末回不回来吃饭"这类看似寒暄、实际在等答复的提问。
**如果对方明确让我做事但没说清是什么事（例如"这个事情得你办一下"），
也要归到这里，写成"要我办一件事（内容未说明）"——不要因为说不清"要什么"
就排除掉。判定关键是"对方是否在等我行动"。**
【待办 / 时间点】
**我自己**需要完成的事：约定、截止时间、金额、地址。
【值得知道】
不需要我行动的信息：通知、结论、决定、与我相关的动态、
**以及别人自己要做的事**（写明是他要做的）。
判断标准：**只要对方在等我做点什么，就不要放进"值得知道"，放前两类。**
【可以忽略】
只写会话名或人名和一句话概括，不展开。已明确结束的对话也算。

关于私聊与群聊的区别（很重要）：
- **私聊**里对方发来的内容几乎都需要我判断，尤其是让我办事、提出问题、通知、约定。
  只有明确已经结束的寒暄才可以放进"可以忽略"。宁可归错到前几类，也不要漏掉私聊。
- **群聊**里大部分是闲聊，可以大量归入"可以忽略"，但 @我 或点名提到我的内容必须归类。

边界示例（照此判断）：
- 别人说"我明天请假" → 【值得知道】"某某说他明天请假"（**不是**我的待办）
- 别人说"你明天把表给我" → 【待办】我明天要把表给他
- 私聊"你好，这个事情得你办一下" → 【需要我回复】（没说清是什么事，但明确在让我做事）
- 私聊"周末回不回来吃饭" → 【需要我回复】（在等我答复）
- 私聊"你们不" → 【可以忽略】（话没说完，没有诉求）
- 群聊"回来吧re45""我没练过枪" → 【可以忽略】（群内闲聊）
- 群聊"@我 明天上线那个开关还是你点吗" → 【需要我回复】

要求：
1. 忽略寒暄、表情、刷屏、广告；
2. 同一件事合并成一条，不要逐条罗列；
3. 时间点务必写清（如"周五之前""下周三"）；
4. 不要编造原文没有的内容，信息不确定时标注"存疑"；
5. 某个标题下没有内容就写"无"；
6. 直接输出简报，不要输出思考过程，不要复述这些要求。"""


#: 分块摘要（map 阶段）用的系统提示
MAP_SYSTEM_PROMPT = """你是我的私人消息整理助手。\
我会分块给你一段 QQ 聊天记录。请压缩成要点，供后续汇总使用。

**最重要的一条：绝对不能漏掉"要我做事"的内容。**
只要块里出现下面任何一类，都必须原样保留在要点里（写清是谁、原话要点）：
- 向我提问、@我、点名让我确认或查看的；
- 让我帮忙办事的（找东西、内推、转发、发邮件）；
- 有约定、截止时间、金额、地址的；
- 私聊里对方发来的、看起来在等我回应的内容。

**这类内容哪怕只有一句，也绝不能因为周围全是闲聊就丢掉。**
换句话说：不要因为整块"大部分是闲聊"就写成"纯闲聊，无要点"——
必须先确认块里确实没有任何上面那几类内容，才可以那样写。

其余要求：
- 保留：谁提了什么请求、时间点、截止日期、金额、决定和结论；
- 丢弃：寒暄、表情、刷屏、无信息量的对话；
- 每条要点标明来自谁；
- 严格控制篇幅：每个会话最多 3 条要点，整段输出不超过 150 字；
  后续还要汇总，写太长反而会被截断丢信息；
- 不要编造，不要输出思考过程，直接输出要点列表（用「- 」开头）。"""


#: 分块摘要时，单个会话块超过该字符数就再切分
CHUNK_SPLIT_CHARS = 3000


def _no_think_prefix(no_think: bool) -> str:
    return "/no_think\n" if no_think else ""


# --------------------------------------------------------------------------
# 提示词解析（内置默认 + 用户覆盖 + 追加规则）
# --------------------------------------------------------------------------


def resolve_system_prompt(cfg: Config) -> str:
    """合成最终生效的系统提示词。

    注意：重要消息的「【重要提醒】」小节**不在这里生成**，而是由
    :func:`qqdigest.digest.build_important_header` 在代码中确定性拼装。
    实测表明：往系统提示词里追加「重要提醒」规则会让 14B 模型在四分类上丢内容
    （出现过整条消息被漏掉）。让模型只做它本来就做得好的四分类，
    重要项由程序自己列，两边都稳。
    """
    parts: list[str] = [(cfg.prompt.system_prompt or "").strip() or SYSTEM_PROMPT]

    extra = (cfg.prompt.extra_rules or "").strip()
    if extra:
        parts.append("补充规则（用户自定义，若与前文冲突以本节为准）：\n" + extra)

    return "\n\n".join(parts)


def resolve_map_system_prompt(cfg: Config) -> str:
    """合成 map 阶段使用的系统提示词。"""
    base = (cfg.prompt.map_system_prompt or "").strip() or MAP_SYSTEM_PROMPT
    extra = (cfg.prompt.extra_rules or "").strip()
    if extra:
        return base + "\n\n补充规则（用户自定义）：\n" + extra
    return base


def is_using_default_system_prompt(cfg: Config) -> bool:
    return not (cfg.prompt.system_prompt or "").strip()


def is_using_default_map_prompt(cfg: Config) -> bool:
    return not (cfg.prompt.map_system_prompt or "").strip()


def build_digest_user_prompt(
    transcript: str,
    *,
    window_desc: str,
    msg_count: int,
    peer_count: int,
    no_think: bool = True,
    excluded_note: str = "",
    weighted: bool = False,
) -> str:
    """构造最终整合的用户消息。"""
    lines = [
        f"时间范围：{window_desc}",
        f"消息条数：{msg_count} 条，来自 {peer_count} 个会话。",
    ]
    if weighted:
        lines.append(
            "消息已**按会话重要度从高到低**排列：越靠前的会话越重要，"
            "请优先处理前面的内容。"
        )
    if excluded_note:
        lines.append(excluded_note)
    lines.append("下面开始是消息正文。")
    return f"{_no_think_prefix(no_think)}" + "\n".join(lines) + f"\n\n{transcript}"


def build_map_user_prompt(chunk: str, *, peer_label: str, no_think: bool = True) -> str:
    """构造分块摘要的用户消息。"""
    header = f"会话：{peer_label}\n下面开始是聊天记录。\n\n"
    return f"{_no_think_prefix(no_think)}{header}{chunk}"


def build_reduce_user_prompt(
    summaries: str,
    *,
    window_desc: str,
    msg_count: int,
    peer_count: int,
    dropped: int = 0,
    no_think: bool = True,
    excluded_note: str = "",
    weighted: bool = False,
) -> str:
    """构造汇总（reduce 阶段）的用户消息。"""
    notes = []
    if dropped:
        notes.append(
            f"注意：由于消息量过大，另有 {dropped} 个会话的要点未能纳入本次整理，"
            f"请在简报末尾用一行说明。"
        )
    if excluded_note:
        notes.append(excluded_note)
    note = ("\n" + "\n".join(notes) + "\n") if notes else ""
    order_hint = (
        "以下要点已按会话重要度从高到低排列；若信息量大，请优先保证前面内容的完整性。\n"
        if weighted
        else ""
    )
    header = (
        f"时间范围：{window_desc}\n"
        f"原始消息共 {msg_count} 条，来自 {peer_count} 个会话。\n"
        f"{order_hint}"
        f"以下是我已按会话压缩好的要点，请据此整理成简报。\n{note}\n"
    )
    return f"{_no_think_prefix(no_think)}{header}{summaries}"
