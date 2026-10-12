"""合成事件数据，用于 ``python run.py selftest`` 端到端自检。

这些是标准的 OneBot 11 事件结构，与 NapCat 实际上报的格式一致，
时间戳按「当前时间往前若干分钟」生成，保证落在自检窗口内。
"""

from __future__ import annotations

import time
from typing import Any

SELF_ID = 10000001

#: (分钟前, 事件)
_SPEC: list[tuple[int, dict[str, Any]]] = [
    (
        320,
        {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": 900001,
            "user_id": 10001,
            "message": [{"type": "text", "data": {"text": "下午三点的评审你来吗"}}],
            "sender": {"user_id": 10001, "nickname": "张伟"},
        },
    ),
    (
        318,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900002,
            "user_id": 10001,
            "message": [{"type": "text", "data": {"text": "就是那个网关重构的方案"}}],
            "sender": {"user_id": 10001, "nickname": "张伟"},
        },
    ),
    (
        300,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900003,
            "user_id": 10002,
            "message": [
                {"type": "text", "data": {"text": "服务器扩到 32G 了，你那边压测可以重跑，"}},
                {"type": "text", "data": {"text": "记得把 qps 打到 5000 再看看"}},
            ],
            "sender": {"user_id": 10002, "nickname": "李工"},
        },
    ),
    (
        285,
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 555000111,
            "group_name": "摸鱼群",
            "message_id": 900004,
            "user_id": 10003,
            "message": [{"type": "text", "data": {"text": "哈哈哈哈这也太离谱了"}}],
            "sender": {"user_id": 10003, "nickname": "小王", "card": "小王"},
        },
    ),
    (
        240,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900005,
            "user_id": 10004,
            "message": [{"type": "text", "data": {"text": "周五之前把季度预算表发我邮箱，别又拖到最后一天"}}],
            "sender": {"user_id": 10004, "nickname": "陈总"},
        },
    ),
    (
        210,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900006,
            "user_id": 10005,
            "message": [
                {"type": "text", "data": {"text": "上个月那笔报销财务说还差一张发票，"}},
                {"type": "text", "data": {"text": "你找一下 12 号那顿的"}},
            ],
            "sender": {"user_id": 10005, "nickname": "王芳"},
        },
    ),
    (
        190,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900007,
            "user_id": 10006,
            "message": [
                {"type": "text", "data": {"text": "这是设计稿"}},
                {"type": "image", "data": {"file": "design.png", "url": "http://example.invalid/x.png"}},
            ],
            "sender": {"user_id": 10006, "nickname": "设计-刘"},
        },
    ),
    (
        170,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900008,
            "user_id": 10007,
            "message": [{"type": "text", "data": {"text": "你这个接口超时设的多少？我这边看到有 5 秒的"}}],
            "sender": {"user_id": 10007, "nickname": "刘工"},
        },
    ),
    (
        150,
        {
            "post_type": "message_sent",
            "message_type": "private",
            "message_id": 900009,
            "user_id": 10007,
            "self_id": SELF_ID,
            "message": [{"type": "text", "data": {"text": "5 秒，我改成 3 秒"}}],
            "sender": {"user_id": SELF_ID, "nickname": "我"},
        },
    ),
    (
        130,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900010,
            "user_id": 10008,
            "message": [{"type": "text", "data": {"text": "下季度房租还是老价格，月底前转过来就行"}}],
            "sender": {"user_id": 10008, "nickname": "房东"},
        },
    ),
    (
        110,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900011,
            "user_id": 10009,
            "message": [{"type": "text", "data": {"text": "您的包裹已放丰巢，取件码 8823"}}],
            "sender": {"user_id": 10009, "nickname": "快递员"},
        },
    ),
    (
        95,
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 555000111,
            "group_name": "摸鱼群",
            "message_id": 900012,
            "user_id": 10010,
            "message": [
                {"type": "at", "data": {"qq": str(SELF_ID), "name": "我"}},
                {"type": "text", "data": {"text": " 明天上线那个开关还是你点吗"}},
            ],
            "sender": {"user_id": 10010, "nickname": "运维-赵", "card": "赵"},
        },
    ),
    (
        80,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900013,
            "user_id": 10011,
            "message": [
                {"type": "text", "data": {"text": "简历我发你邮箱了，帮忙内推一下，是后端，三年经验"}},
                {"type": "file", "data": {"file": "resume.pdf", "name": "resume.pdf"}},
            ],
            "sender": {"user_id": 10011, "nickname": "赵敏"},
        },
    ),
    (
        60,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900014,
            "user_id": 10012,
            "message": [{"type": "text", "data": {"text": "周末回不回来吃饭"}}],
            "sender": {"user_id": 10012, "nickname": "妈妈"},
        },
    ),
    (
        45,
        {
            "post_type": "message",
            "message_type": "private",
            "message_id": 900015,
            "user_id": 10012,
            "message": [{"type": "text", "data": {"text": "你爸买了好多螃蟹"}}],
            "sender": {"user_id": 10012, "nickname": "妈妈"},
        },
    ),
    (
        30,
        {
            # 非消息事件，应当被忽略
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "status": {"online": True},
        },
    ),
    (
        20,
        {
            # 黑名单外的群消息，collect_group=false 时应被过滤
            "post_type": "message",
            "message_type": "group",
            "group_id": 555000222,
            "group_name": "广告群",
            "message_id": 900016,
            "user_id": 10013,
            "message": [{"type": "text", "data": {"text": "【推广】点击领取优惠券"}}],
            "sender": {"user_id": 10013, "nickname": "推广号"},
        },
    ),
    (
        10,
        {
            # string 形式的 CQ 码消息，验证兼容性
            "post_type": "message",
            "message_type": "private",
            "message_id": 900017,
            "user_id": 10014,
            "message": "帮我看看这个[CQ:image,file=shot.png]是不是有问题",
            "sender": {"user_id": 10014, "nickname": "测试-孙"},
        },
    ),
]


def _build() -> list[dict[str, Any]]:
    now = int(time.time())
    events: list[dict[str, Any]] = []
    for minutes_ago, event in _SPEC:
        ev = dict(event)
        ev.setdefault("time", now - minutes_ago * 60)
        ev.setdefault("self_id", SELF_ID)
        ev.setdefault("font", 0)
        events.append(ev)
    return events


#: 可直接使用的事件列表
SYNTHETIC_EVENTS: list[dict[str, Any]] = _build()
