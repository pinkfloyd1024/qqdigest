"""本轮新增能力的测试：内容清理、精确关键词、每小时调度、静音与保留策略、统计。"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqdigest import tagging  # noqa: E402
from qqdigest.config import TagsCfg  # noqa: E402
from qqdigest.onebot import clean_text, parse_event  # noqa: E402
from qqdigest.scheduler import slots_for_day  # noqa: E402
from qqdigest.store import Store  # noqa: E402
from tests.test_core import _msg, make_cfg  # noqa: E402

TZ = timezone.utc


# ==========================================================================
# 无效内容剔除
# ==========================================================================


class TestCleanText(unittest.TestCase):
    def test_pure_placeholder_dropped(self):
        for raw in ["[图片]", "[表情]", "[表情][表情]", "[图片][图片][表情]", "[语音]", "[卡片]"]:
            self.assertEqual(clean_text(raw), "", f"{raw} 应被整条剔除")

    def test_pure_filler_dropped(self):
        for raw in ["哈哈", "嗯", "1", "11", "。。。", "顶", "+1", "   "]:
            self.assertEqual(clean_text(raw), "", f"{raw!r} 应被剔除")

    def test_meaningful_short_message_kept(self):
        """⚠️ 回归：'在吗' 曾经被误杀。

        早期把「在吗/收到/好的」也列进噪音表，结果把**等我回复的提问**整条丢掉了。
        宁可多留一条噪音，也不能误杀一条需要我行动的消息。
        """
        for raw in ["在吗", "收到", "好的", "？", "ok"]:
            self.assertEqual(clean_text(raw), raw, f"{raw!r} 不应被剔除")

    def test_mixed_content_compacted(self):
        self.assertEqual(clean_text("看这个[图片]"), "看这个[图]")
        self.assertEqual(clean_text("[图片]这个很重要[表情]"), "[图]这个很重要[表情]")
        self.assertEqual(clean_text("明天[图片]交作业"), "明天[图]交作业")

    def test_disabled_keeps_raw(self):
        self.assertEqual(clean_text("[图片]", strip_placeholders=False), "[图片]")

    def test_parse_event_drops_noise_message(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = make_cfg(Path(tmp.name))
        ev = {
            "post_type": "message", "message_type": "private", "message_id": 1,
            "user_id": 1234, "self_id": 1000, "time": 1700000000,
            "message": [{"type": "image", "data": {"file": "a.png"}}],
            "sender": {"user_id": 1234, "nickname": "某人"},
        }
        rec, reason = parse_event(ev, cfg)
        self.assertIsNone(rec)
        self.assertIn("无有效内容", reason)

    def test_parse_event_compacts_mixed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = make_cfg(Path(tmp.name))
        ev = {
            "post_type": "message", "message_type": "private", "message_id": 2,
            "user_id": 1234, "self_id": 1000, "time": 1700000000,
            "message": [
                {"type": "text", "data": {"text": "报告在这"}},
                {"type": "image", "data": {"file": "a.png"}},
            ],
            "sender": {"user_id": 1234, "nickname": "某人"},
        }
        rec, _ = parse_event(ev, cfg)
        assert rec is not None
        self.assertEqual(rec["text"], "报告在这[图]")


# ==========================================================================
# 精确关键词 &-..-&
# ==========================================================================


class TestExactKeyword(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = make_cfg(Path(self.tmp.name))
        self.cfg = replace(
            base,
            tags=TagsCfg(
                labels=("学校", "聊天群"),
                weights={"学校": 8, "聊天群": 1},
                keywords={"学校": ("&-级-&",)},
                default_label="聊天群",
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_does_not_match_glued(self):
        """`级` 紧邻数字/中文时不算独立出现，不应命中。

        这就是为什么需要精确匹配：单字关键词 `级` 会把
        "2026级某某大学新生群" 这种正常群名也划走。
        """
        self.assertIsNone(tagging.match_keywords(self.cfg, "2026级某某大学新生群"))
        self.assertIsNone(tagging.match_keywords(self.cfg, "年级群"))

    def test_exact_matches_with_boundary(self):
        hit = tagging.match_keywords(self.cfg, "2026 级 新生群")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "学校")  # type: ignore[index]

    def test_exact_matches_at_edges(self):
        self.assertIsNotNone(tagging.match_keywords(self.cfg, "级"))
        self.assertIsNotNone(tagging.match_keywords(self.cfg, "级-新生"))

    def test_plain_keyword_still_substring(self):
        cfg = replace(
            self.cfg,
            tags=TagsCfg(
                labels=("学校", "聊天群"),
                weights={"学校": 8},
                keywords={"学校": ("学院",)},
                default_label="聊天群",
            ),
        )
        self.assertIsNotNone(tagging.match_keywords(cfg, "某某学院群"))

    def test_fence_stripped_in_match_report(self):
        hit = tagging.match_keywords(self.cfg, "2026 级 新生群")
        self.assertEqual(hit[1], "&-级-&")  # type: ignore[index]


# ==========================================================================
# 每小时调度
# ==========================================================================


class TestHourlySchedule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = make_cfg(Path(self.tmp.name))
        self.cfg = replace(
            base,
            general=replace(
                base.general, schedule_mode="hourly",
                work_hours_start="08:00", work_hours_end="23:00",
                precompute_lead_min=5,
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_hourly_slots_cover_work_hours(self):
        slots = slots_for_day(date(2026, 2, 5), self.cfg, TZ)
        self.assertEqual(len(slots), 16, "08:00~23:00 应是 16 档")
        self.assertEqual(slots[0].key, "2026-02-05T08:00")
        self.assertEqual(slots[-1].key, "2026-02-05T23:00")
        for s in slots:
            self.assertEqual(s.fire_at - s.precompute_at, 300)

    def test_explicit_mode_unchanged(self):
        cfg = replace(self.cfg, general=replace(self.cfg.general, schedule_mode="explicit"))
        slots = slots_for_day(date(2026, 2, 5), cfg, TZ)
        self.assertEqual([s.key for s in slots], ["2026-02-05T12:00", "2026-02-05T18:00"])

    def test_reversed_work_hours_rejected(self):
        cfg = replace(
            self.cfg, general=replace(self.cfg.general, work_hours_start="23:00", work_hours_end="08:00")
        )
        with self.assertRaises(ValueError):
            slots_for_day(date(2026, 2, 5), cfg, TZ)


# ==========================================================================
# 静音 / 视图 / 清理
# ==========================================================================


class TestMuteAndPurge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))
        self.store = Store(self.cfg.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _seed(self):
        self.store.insert_messages(
            [
                _msg("g1", 100, peer_id="1", peer_name="营销群", chat_type="group", text="广告"),
                _msg("g2", 110, peer_id="1", peer_name="营销群", chat_type="group", text="又来"),
                _msg("p1", 120, peer_id="2", peer_name="好友", chat_type="private", text="在吗"),
                _msg("h1", 130, peer_id="3", peer_name="课程群", chat_type="group", text="交作业"),
            ]
        )

    def test_mute_by_weight(self):
        self._seed()
        n = self.store.apply_mute_flags({"1": 0, "2": 100, "3": 8})
        self.assertEqual(n, 2, "权重 0 的营销群两条都应静音")
        self.assertEqual(self.store.muted_message_count(), 2)

    def test_views_exclude_muted(self):
        self._seed()
        self.store.apply_mute_flags({"1": 0, "2": 100, "3": 8})
        group_n = self.store._conn.execute("SELECT COUNT(*) FROM v_group_messages").fetchone()[0]
        priv_n = self.store._conn.execute("SELECT COUNT(*) FROM v_private_messages").fetchone()[0]
        self.assertEqual(group_n, 1, "群视图应排除静音的营销群")
        self.assertEqual(priv_n, 1)

    def test_view_columns_match_spec(self):
        """群视图=时间+群号+QQ号+网名；私聊视图=时间+QQ号+网名。"""
        cols = [
            r[1] for r in self.store._conn.execute("PRAGMA table_info(v_group_messages)").fetchall()
        ]
        for need in ("ts", "group_id", "sender_qq", "sender_name", "content"):
            self.assertIn(need, cols)
        cols2 = [
            r[1] for r in self.store._conn.execute("PRAGMA table_info(v_private_messages)").fetchall()
        ]
        for need in ("ts", "peer_qq", "sender_name", "content"):
            self.assertIn(need, cols2)

    def test_mute_is_reversible(self):
        """标记式静音的最大价值：改回标签就能恢复，不像直接丢弃那样不可逆。"""
        self._seed()
        self.store.apply_mute_flags({"1": 0, "2": 100, "3": 8})
        self.assertEqual(self.store.muted_message_count(), 2)
        self.store.apply_mute_flags({"1": 10, "2": 100, "3": 8})
        self.assertEqual(self.store.muted_message_count(), 0)

    def test_purge_range_only_touches_window(self):
        self._seed()
        # g1@100 与 g2@110 都落在 [100,115) 内；p1@120、h1@130 在窗口外
        removed = self.store.purge_range(100, 115)
        self.assertEqual(removed, 2, "只应删 [100,115) 内的 2 条")
        left = {m["msg_id"] for m in self.store.fetch_messages(0, 999)}
        self.assertEqual(left, {"p1", "h1"}, "窗口外的消息必须留着")

    def test_purge_muted_respects_keep_days(self):
        self._seed()
        self.store.apply_mute_flags({"1": 0, "2": 100, "3": 8})
        self.assertEqual(self.store.purge_muted(7, now_ts=1000), 0, "太新不该删")
        self.assertEqual(self.store.purge_muted(0), 0, "0 表示不清理")


# ==========================================================================
# 统计
# ==========================================================================


class TestStats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))
        self.store = Store(self.cfg.db_path)
        # 构造三个不同小时的消息
        base = int(datetime(2026, 2, 5, 0, 0, tzinfo=TZ).timestamp())
        rows = []
        for i, (hour, peer) in enumerate([(9, "1"), (9, "1"), (14, "2"), (21, "3")]):
            rows.append(
                _msg(f"m{i}", base + hour * 3600 + i, peer_id=peer, peer_name=f"会话{peer}",
                     chat_type="group" if peer != "2" else "private")
            )
        self.store.insert_messages(rows)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_hourly_distribution(self):
        base = int(datetime(2026, 2, 5, 0, 0, tzinfo=TZ).timestamp())
        # 注意：hourly_distribution 内部按 UTC+8 换算，这里用相对断言
        dist = self.store.hourly_distribution(base, base + 86400)
        self.assertEqual(sum(n for _, n in dist), 4)
        self.assertEqual(len(dist), 3, "应该有三个不同小时")

    def test_peer_totals_sorted(self):
        base = int(datetime(2026, 2, 5, 0, 0, tzinfo=TZ).timestamp())
        totals = self.store.peer_totals(base, base + 86400)
        self.assertEqual(totals[0][0], "1")
        self.assertEqual(totals[0][3], 2, "会话 1 有两条，应排最前")


if __name__ == "__main__":
    unittest.main(verbosity=2)
