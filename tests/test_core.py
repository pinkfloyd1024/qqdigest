"""核心逻辑单元测试。

运行： python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqdigest.config import (  # noqa: E402
    Config,
    FilterCfg,
    GeneralCfg,
    LlmCfg,
    OneBotCfg,
    StorageCfg,
    TargetCfg,
    get_tz,
)
from qqdigest.digest import group_messages, render_transcript  # noqa: E402
from qqdigest.llm import LlmResult, estimate_tokens  # noqa: E402
from qqdigest.onebot import parse_event, segment_text  # noqa: E402
from qqdigest.scheduler import Scheduler, slots_for_day  # noqa: E402
from qqdigest.sender import split_text  # noqa: E402
from qqdigest.store import Store  # noqa: E402

TZ = timezone.utc


def make_cfg(root: Path, **overrides) -> Config:
    general = GeneralCfg(timezone="UTC", dry_run=True, schedule=("12:00", "18:00"))
    onebot = OneBotCfg(self_id="1000")
    target = TargetCfg(send_to="2000")
    llm = LlmCfg(max_tokens=3000, input_budget_tokens=13000)
    filt = FilterCfg()
    storage = StorageCfg(
        data_dir=root / "data",
        runs_dir=root / "runs",
        logs_dir=root / "logs",
        clipboard_on_dry_run=False,
    )
    cfg = Config(
        root=root,
        general=general,
        onebot=onebot,
        target=target,
        llm=llm,
        filter=filt,
        storage=storage,
    )
    for key, value in overrides.items():
        cfg = replace(cfg, **{key: value})
    return cfg


class FakeLlm:
    """返回固定内容的假模型，避免测试依赖 LM Studio。"""

    def __init__(self, content: str = "【需要我回复】\n无") -> None:
        self.content = content
        self.calls = 0

    def chat(self, system: str, user: str, **kwargs) -> LlmResult:  # noqa: ARG002
        self.calls += 1
        return LlmResult(
            content=self.content,
            prompt_tokens=100,
            completion_tokens=50,
            reasoning_tokens=0,
            elapsed_sec=0.1,
            model="fake",
        )


# ==========================================================================
# onebot
# ==========================================================================


class TestSegmentText(unittest.TestCase):
    def test_text_and_at(self):
        text, others, _ = segment_text(
            [
                {"type": "at", "data": {"qq": "1000", "name": "我"}},
                {"type": "text", "data": {"text": " 你好"}},
            ]
        )
        self.assertEqual(text, "@我 你好")
        self.assertEqual(others, [])

    def test_image_and_file(self):
        text, others, _ = segment_text(
            [
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "image", "data": {"file": "a.png"}},
                {"type": "file", "data": {"file": "报告.pdf"}},
            ]
        )
        self.assertIn("[图片]", text)
        self.assertIn("[文件: 报告.pdf]", text)
        self.assertEqual(sorted(others), ["file", "image"])

    def test_at_all(self):
        text, _, _ = segment_text([{"type": "at", "data": {"qq": "all"}}])
        self.assertEqual(text, "@全体成员")

    def test_cq_string_compat(self):
        text, others, _ = segment_text("帮我看看[CQ:image,file=shot.png]是不是有问题")
        self.assertIn("[图片]", text)
        self.assertIn("是不是有问题", text)
        self.assertEqual(others, ["image"])

    def test_empty(self):
        self.assertEqual(segment_text([])[0], "")
        self.assertEqual(segment_text(None)[0], "")


class TestParseEvent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _ev(self, **kw):
        base = {
            "post_type": "message",
            "message_type": "private",
            "message_id": 1,
            "user_id": 1234,
            "self_id": 1000,
            "time": 1700000000,
            "message": [{"type": "text", "data": {"text": "在吗"}}],
            "sender": {"user_id": 1234, "nickname": "张三"},
        }
        base.update(kw)
        return base

    def test_private_accepted(self):
        rec, reason = parse_event(self._ev(), self.cfg)
        self.assertIsNotNone(rec, reason)
        assert rec is not None
        self.assertEqual(rec["chat_type"], "private")
        self.assertEqual(rec["peer_id"], "1234")
        self.assertEqual(rec["direction"], 0)
        self.assertEqual(rec["sender_name"], "张三")

    def test_group_filtered_by_default(self):
        rec, reason = parse_event(self._ev(message_type="group", group_id=777), self.cfg)
        self.assertIsNone(rec)
        self.assertIn("过滤", reason)

    def test_group_collected_when_enabled(self):
        cfg = replace(self.cfg, filter=FilterCfg(collect_group=True))
        rec, reason = parse_event(self._ev(message_type="group", group_id=777), cfg)
        self.assertIsNotNone(rec, reason)
        assert rec is not None
        self.assertEqual(rec["peer_id"], "777")
        self.assertEqual(rec["chat_type"], "group")

    def test_group_whitelist(self):
        cfg = replace(
            self.cfg, filter=FilterCfg(collect_group=True, group_whitelist=("888",))
        )
        rec, _ = parse_event(self._ev(message_type="group", group_id=777), cfg)
        self.assertIsNone(rec)
        rec, _ = parse_event(self._ev(message_type="group", group_id=888), cfg)
        self.assertIsNotNone(rec)

    def test_blacklist(self):
        cfg = replace(self.cfg, filter=FilterCfg(peer_blacklist=frozenset({"1234"})))
        rec, _ = parse_event(self._ev(), cfg)
        self.assertIsNone(rec)

    def test_message_sent_is_outgoing(self):
        rec, _ = parse_event(
            self._ev(post_type="message_sent", sender={"user_id": 1000, "nickname": "我"}),
            self.cfg,
        )
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec["direction"], 1)

    def test_message_sent_private_uses_peer_uin(self):
        """NapCat 的 message_sent 把发送者放在 user_id，对端在 peerUin。

        实测原始事件：
            "post_type":"message_sent","user_id":10000001,
            "sender":{"user_id":10000001},"peerUin":"10000002"
        若按标准 OneBot 取 user_id，会把"我发出的"错记成"自己发给自己"。
        """
        rec, reason = parse_event(
            {
                "post_type": "message_sent",
                "message_type": "private",
                "message_id": 96274190,
                "user_id": 1000,  # 发送者自己
                "peerUin": "2000",  # 真正的对端
                "self_id": 1000,
                "time": 1700000000,
                "message": [{"type": "text", "data": {"text": "我发出去的"}}],
                "sender": {"user_id": 1000, "nickname": "我"},
            },
            self.cfg,
        )
        self.assertIsNotNone(rec, reason)
        assert rec is not None
        self.assertEqual(rec["peer_id"], "2000")
        self.assertEqual(rec["direction"], 1)

    def test_message_sent_target_id_fallback(self):
        rec, _ = parse_event(
            {
                "post_type": "message_sent",
                "message_type": "private",
                "message_id": 2,
                "user_id": 1000,
                "target_id": 3000,
                "self_id": 1000,
                "time": 1700000000,
                "message": [{"type": "text", "data": {"text": "hi"}}],
                "sender": {"user_id": 1000, "nickname": "我"},
            },
            self.cfg,
        )
        assert rec is not None
        self.assertEqual(rec["peer_id"], "3000")

    def test_message_sent_without_peer_is_skipped(self):
        """对端标识完全缺失时宁可跳过，也不要污染成"自己给自己发"。"""
        rec, reason = parse_event(
            {
                "post_type": "message_sent",
                "message_type": "private",
                "message_id": 3,
                "user_id": 1000,
                "self_id": 1000,
                "time": 1700000000,
                "message": [{"type": "text", "data": {"text": "hi"}}],
                "sender": {"user_id": 1000, "nickname": "我"},
            },
            self.cfg,
        )
        self.assertIsNone(rec)
        self.assertIn("对端", reason)

    def test_message_sent_group_uses_group_id(self):
        cfg = replace(self.cfg, filter=FilterCfg(collect_group=True))
        rec, _ = parse_event(
            {
                "post_type": "message_sent",
                "message_type": "group",
                "message_id": 4,
                "group_id": 777,
                "user_id": 1000,
                "self_id": 1000,
                "time": 1700000000,
                "message": [{"type": "text", "data": {"text": "群里发的"}}],
                "sender": {"user_id": 1000, "nickname": "我"},
            },
            cfg,
        )
        assert rec is not None
        self.assertEqual(rec["peer_id"], "777")
        self.assertEqual(rec["direction"], 1)

    def test_meta_event_ignored(self):
        rec, reason = parse_event({"post_type": "meta_event", "meta_event_type": "heartbeat"}, self.cfg)
        self.assertIsNone(rec)
        self.assertIn("meta_event", reason)

    def test_highlight_without_question(self):
        rec, _ = parse_event(self._ev(), self.cfg)
        assert rec is not None
        self.assertEqual(rec["highlight"], 0)  # "在吗" 无问号，且非 @我

    def test_highlight_question_mark(self):
        rec, _ = parse_event(
            self._ev(message=[{"type": "text", "data": {"text": "这个怎么办?"}}]), self.cfg
        )
        assert rec is not None
        self.assertEqual(rec["highlight"], 1)

    def test_empty_message_ignored(self):
        rec, _ = parse_event(self._ev(message=[]), self.cfg)
        self.assertIsNone(rec)


# ==========================================================================
# store
# ==========================================================================


def _msg(msg_id: str, ts: int, **kw) -> dict:
    base = {
        "msg_id": msg_id,
        "ts": ts,
        "direction": 0,
        "chat_type": "private",
        "peer_id": "1234",
        "peer_name": "张三",
        "sender_id": "1234",
        "sender_name": "张三",
        "content_type": "text",
        "text": "内容",
        "highlight": 0,
        "important": 0,
        "muted": 0,
        "collected_at": ts,
    }
    base.update(kw)
    return base


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_insert_and_dedup(self):
        self.assertEqual(self.store.insert_messages([_msg("a", 100)]), 1)
        self.assertEqual(self.store.insert_messages([_msg("a", 100)]), 0)
        self.assertEqual(self.store.insert_messages([_msg("b", 200)]), 1)
        self.assertEqual(self.store.count_messages(), 2)

    def test_fetch_window_is_half_open(self):
        self.store.insert_messages([_msg("a", 100), _msg("b", 200), _msg("c", 300)])
        got = self.store.fetch_messages(100, 300)
        self.assertEqual([m["msg_id"] for m in got], ["a", "b"])

    def test_only_incoming(self):
        self.store.insert_messages([_msg("a", 100), _msg("b", 200, direction=1)])
        got = self.store.fetch_messages(0, 1000, only_incoming=True)
        self.assertEqual([m["msg_id"] for m in got], ["a"])

    def test_digest_slot_is_unique(self):
        d1 = self.store.create_digest("s1", 0, 10, 20)
        d2 = self.store.create_digest("s1", 0, 10, 20)
        self.assertIsNotNone(d1)
        self.assertIsNone(d2)

    def test_due_and_sent_flow(self):
        did = self.store.create_digest("s1", 0, 10, 100)
        assert did is not None
        self.store.finish_digest(did, content="正文", state="pending")
        self.assertEqual(len(self.store.due_digests(99)), 0)
        self.assertEqual(len(self.store.due_digests(100)), 1)
        self.store.mark_sent(did)
        self.assertEqual(len(self.store.due_digests(1000)), 0)
        row = self.store.get_digest(did)
        assert row is not None
        self.assertEqual(row["state"], "sent")

    def test_window_state(self):
        self.assertEqual(self.store.get_window_end(500), 500)
        self.store.set_window_end(1234)
        self.assertEqual(self.store.get_window_end(500), 1234)


# ==========================================================================
# 渲染
# ==========================================================================


class TestRender(unittest.TestCase):
    def test_grouping_and_order(self):
        msgs = [
            _msg("a", 200, peer_id="1", peer_name="李四"),
            _msg("b", 100, peer_id="2", peer_name="张三"),
            _msg("c", 150, peer_id="1", peer_name="李四"),
        ]
        grouped = group_messages(msgs)
        self.assertEqual(set(grouped), {"1", "2"})
        self.assertEqual([m["msg_id"] for m in grouped["1"]], ["c", "a"])
        text = render_transcript(grouped, TZ)
        self.assertIn("【会话】李四", text)
        self.assertIn("【会话】张三", text)

    def test_self_label(self):
        msgs = [_msg("a", 100, direction=1)]
        text = render_transcript(group_messages(msgs), TZ)
        self.assertIn("我(本人):", text)


# ==========================================================================
# 发送
# ==========================================================================


class TestSendForce(unittest.TestCase):
    """force=True 必须真的绕过干跑模式。

    踩过的坑：早期只在 CLI 层判断了 dry_run，send_text() 内部又拦了一道，
    结果 --force-send 完全不生效，界面上点"立即整合并发送"也只是复制到剪贴板。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_blocks_without_force(self):
        from qqdigest.sender import split_text, send_text

        ok, detail = send_text(self.cfg, "测试内容")
        self.assertTrue(ok)
        self.assertIn("dry-run", detail)

    def test_force_makes_it_attempt_real_send(self):
        from qqdigest.sender import send_text

        # NapCat 不可达，因此会真的尝试连接并失败——这正是"确实发了"的证据
        cfg = replace(self.cfg, onebot=replace(self.cfg.onebot, api_timeout_sec=2))
        ok, detail = send_text(cfg, "测试内容", force=True)
        self.assertFalse(ok, "force 应该真的尝试发送（此处因 NapCat 不可达而失败）")
        self.assertNotIn("dry-run", detail)
        self.assertIn("失败", detail)


class TestSplitText(unittest.TestCase):
    def test_short(self):
        self.assertEqual(split_text("你好", 100), ["你好"])

    def test_long_splits_without_loss(self):
        text = "\n".join(f"第{i}行内容" for i in range(200))
        chunks = split_text(text, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks), [len(c) for c in chunks])
        joined = "".join(chunks).replace("\n", "")
        self.assertEqual(joined, text.replace("\n", ""))

    def test_hard_split_single_long_line(self):
        chunks = split_text("x" * 250, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual("".join(chunks), "x" * 250)

    def test_zero_limit(self):
        self.assertEqual(split_text("abc", 0), ["abc"])


# ==========================================================================
# token 估算
# ==========================================================================


class TestEstimateTokens(unittest.TestCase):
    def test_monotonic(self):
        self.assertLess(estimate_tokens("短"), estimate_tokens("长" * 100))

    def test_chinese_denser_than_ascii(self):
        self.assertGreater(estimate_tokens("中" * 100), estimate_tokens("a" * 100))

    def test_empty(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_reasonable_magnitude(self):
        # 实测校准点：约 900 字的混合中文文本对应约 750 tokens
        sample = "张伟: 下午三点的评审你来吗 李工: 服务器扩到 32G 了" * 20
        est = estimate_tokens(sample)
        self.assertGreater(est, len(sample) * 0.5)
        self.assertLess(est, len(sample) * 1.3)


# ==========================================================================
# 调度
# ==========================================================================


class TestSlots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_slot_math(self):
        from datetime import date

        slots = slots_for_day(date(2026, 2, 5), self.cfg, TZ)
        self.assertEqual([s.key for s in slots], ["2026-02-05T12:00", "2026-02-05T18:00"])
        self.assertEqual(slots[0].fire_at - slots[0].precompute_at, 5 * 60)


class TestSchedulerFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))
        self.store = Store(self.cfg.db_path)
        self.llm = FakeLlm()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_window_uses_precompute_time(self):
        """窗口结束边界必须是预计算时刻，不是发送时刻。"""
        now = datetime(2026, 2, 5, 11, 56, tzinfo=TZ)  # 预计算 11:55 已过，发送 12:00 未到
        self.store.set_window_end(int(datetime(2026, 2, 5, 6, 0, tzinfo=TZ).timestamp()))
        self.store.insert_messages([_msg("m1", int(now.timestamp()) - 60)])

        sched = Scheduler(self.store, self.cfg, self.llm)  # type: ignore[arg-type]
        stats = sched.tick(now)
        self.assertEqual(stats["precomputed"], 1)
        self.assertEqual(stats["sent"], 0)

        row = self.store.recent_digests(1)[0]
        self.assertEqual(row["slot"], "2026-02-05T12:00")
        self.assertEqual(row["window_end"], int(now.timestamp()))
        self.assertEqual(row["fire_at"], int(datetime(2026, 2, 5, 12, 0, tzinfo=TZ).timestamp()))
        # 水位线应推进到预计算时刻
        self.assertEqual(self.store.get_window_end(0), int(now.timestamp()))

    def test_no_duplicate_precompute(self):
        now = datetime(2026, 2, 5, 11, 56, tzinfo=TZ)
        sched = Scheduler(self.store, self.cfg, self.llm)  # type: ignore[arg-type]
        self.assertEqual(sched.tick(now)["precomputed"], 1)
        self.assertEqual(sched.tick(now)["precomputed"], 0)

    def test_send_at_fire_time(self):
        pre = datetime(2026, 2, 5, 11, 56, tzinfo=TZ)
        fire = datetime(2026, 2, 5, 12, 0, 5, tzinfo=TZ)
        sched = Scheduler(self.store, self.cfg, self.llm)  # type: ignore[arg-type]
        sched.tick(pre)
        self.assertEqual(sched.tick(fire)["sent"], 1)
        self.assertEqual(self.store.recent_digests(1)[0]["state"], "sent")

    def test_before_precompute_does_nothing(self):
        now = datetime(2026, 2, 5, 11, 50, tzinfo=TZ)
        sched = Scheduler(self.store, self.cfg, self.llm)  # type: ignore[arg-type]
        stats = sched.tick(now)
        # 当天两个档位都不应生成简报（前一天的档位会被登记为跳过，属预期行为）
        self.assertFalse(self.store.has_digest_for_slot("2026-02-05T12:00"))
        self.assertFalse(self.store.has_digest_for_slot("2026-02-05T18:00"))

    def test_missed_slot_is_skipped(self):
        """错过太久（超过 catch_up_max_min）应登记跳过而不是补发。"""
        now = datetime(2026, 2, 5, 14, 0, tzinfo=TZ)  # 12:00 那档已过 2 小时
        sched = Scheduler(self.store, self.cfg, self.llm)  # type: ignore[arg-type]
        stats = sched.tick(now)
        self.assertEqual(stats["precomputed"], 0)
        self.assertGreaterEqual(stats["skipped"], 1)
        states = {d["slot"]: d["state"] for d in self.store.recent_digests(5)}
        self.assertEqual(states.get("2026-02-05T12:00"), "failed")

    def test_empty_window_message(self):
        now = datetime(2026, 2, 5, 11, 56, tzinfo=TZ)
        self.store.set_window_end(int(now.timestamp()) - 3600)
        sched = Scheduler(self.store, self.cfg, self.llm)  # type: ignore[arg-type]
        sched.tick(now)
        row = self.store.recent_digests(1)[0]
        self.assertEqual(row["content"], "本时段没有新消息。")
        self.assertEqual(row["msg_count"], 0)


# ==========================================================================
# 配置
# ==========================================================================


class TestConfigValidation(unittest.TestCase):
    def test_max_tokens_too_small_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "config.toml").write_text(
            "\n".join(
                [
                    "[onebot]",
                    'self_id = "1000"',
                    "[target]",
                    'send_to = "2000"',
                    "[llm]",
                    "max_tokens = 500",
                ]
            ),
            encoding="utf-8",
        )
        from qqdigest.config import load_config

        with self.assertRaises(ValueError) as ctx:
            load_config(root / "config.toml")
        self.assertIn("max_tokens", str(ctx.exception))

    def test_same_target_as_self_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "config.toml").write_text(
            '\n'.join(['[onebot]', 'self_id = "1000"', '[target]', 'send_to = "1000"']),
            encoding="utf-8",
        )
        from qqdigest.config import load_config

        with self.assertRaises(ValueError) as ctx:
            load_config(root / "config.toml")
        self.assertIn("回环", str(ctx.exception))

    def test_bad_schedule_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "config.toml").write_text(
            "\n".join(
                [
                    "[general]",
                    'schedule = ["25:00"]',
                    "[onebot]",
                    'self_id = "1000"',
                    "[target]",
                    'send_to = "2000"',
                ]
            ),
            encoding="utf-8",
        )
        from qqdigest.config import load_config

        with self.assertRaises(ValueError):
            load_config(root / "config.toml")

    def test_timezone_fallback(self):
        tz = get_tz("Asia/Shanghai")
        now = datetime(2026, 1, 1, 0, 0, tzinfo=tz)
        self.assertEqual(now.utcoffset().total_seconds(), 8 * 3600)  # type: ignore[union-attr]

    def test_real_project_config_is_valid(self):
        """真实 config.toml 必须能加载，且关键项合理。

        注意：**这里不校验任何具体账号**。早期版本断言了 self_id 等于某个
        写死的 QQ 号，那既让测试依赖个人环境，又把账号信息带进了仓库。
        配置的"正确性"应该是结构性的，不是"等于我的号"。
        """
        from qqdigest.config import load_config

        cfg = load_config(Path(__file__).resolve().parent.parent / "config.toml")
        self.assertTrue(cfg.onebot.self_id.strip(), "self_id 不能为空")
        self.assertTrue(cfg.onebot.self_id.isdigit(), "self_id 应该是数字 QQ 号")
        self.assertTrue(cfg.target.send_to.strip(), "send_to 不能为空")
        self.assertGreaterEqual(cfg.llm.max_tokens, 2000, "max_tokens 太小会导致正文为空")
        self.assertTrue(cfg.general.schedule, "至少要有一个发送档位")


if __name__ == "__main__":
    unittest.main(verbosity=2)
