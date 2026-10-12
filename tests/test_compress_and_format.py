"""本轮新增能力的测试：预压缩、QQ 排版、滚轮路由。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqdigest import formatting  # noqa: E402
from qqdigest.compress import compress_window  # noqa: E402
from qqdigest.digest import ordered_peer_blocks, render_transcript_ex  # noqa: E402
from qqdigest.llm import LlmResult  # noqa: E402
from qqdigest.store import Store  # noqa: E402
from tests.test_core import _msg, make_cfg  # noqa: E402

TZ = __import__("datetime").timezone.utc


class FakeLlm:
    def __init__(self, text: str = "- 要点一\n- 要点二") -> None:
        self.text = text
        self.calls = 0
        self.prompts: list[str] = []

    def chat(self, system: str, user: str, **kwargs) -> LlmResult:
        self.calls += 1
        self.prompts.append(user)
        return LlmResult(
            content=self.text, prompt_tokens=100, completion_tokens=20,
            reasoning_tokens=0, elapsed_sec=0.01, model="fake",
        )


# ==========================================================================
# 预压缩
# ==========================================================================


class TestCompress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))
        self.store = Store(self.cfg.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _seed(self):
        rows = []
        # 低权重群（聊天群 权重1）
        for i in range(6):
            rows.append(_msg(f"low{i}", 100 + i, peer_id="10", peer_name="闲聊群",
                             chat_type="group", text=f"闲聊{i}"))
        # 高权重群（学校 权重8 >= no_compress_weight）
        rows.append(_msg("high1", 130, peer_id="20", peer_name="学院群",
                         chat_type="group", text="明天交作业"))
        # 私聊（权重 100）
        rows.append(_msg("priv1", 140, peer_id="30", peer_name="好友",
                         chat_type="private", text="在吗"))
        self.store.insert_messages(rows)
        self.store.upsert_peer_tag(peer_id="10", peer_name="闲聊群", chat_type="group",
                                   label="聊天群", weight=1, source="keyword")
        self.store.upsert_peer_tag(peer_id="20", peer_name="学院群", chat_type="group",
                                   label="学校", weight=8, source="keyword")
        self.store.upsert_peer_tag(peer_id="30", peer_name="好友", chat_type="private",
                                   label="私聊", weight=100, source="manual")

    def test_only_low_weight_is_compressed(self):
        self._seed()
        llm = FakeLlm()
        stats = compress_window(self.store, self.cfg, llm, window_start=0, window_end=999)
        self.assertEqual(stats.peers, 1, "只有低权重的闲聊群该被压缩")
        self.assertEqual(stats.msg_count, 6)
        self.assertEqual(stats.skipped_high, 2, "学院群与私聊都算高权重")
        self.assertEqual(llm.calls, 1)

    def test_summary_stored_and_reused(self):
        self._seed()
        compress_window(self.store, self.cfg, FakeLlm("- 都是闲聊"), window_start=0, window_end=999)
        got = self.store.get_compressed_summaries(0, 999)
        self.assertIn("10", got)
        self.assertIn("都是闲聊", got["10"])

    def test_partial_window_summary_not_used(self):
        """摘要没覆盖整个窗口时不能采用——否则块外的消息会被替掉、内容缺一截。

        语义是"包含"：只有 ``block_start <= start 且 block_end >= end`` 才能用。
        """
        self._seed()
        self.store.save_compressed_block(peer_id="10", peer_name="闲聊群",
                                         window_start=100, window_end=150,
                                         summary="半截摘要", msg_count=5)
        # 查询窗口比块大 -> 块覆盖不住 -> 不能用
        self.assertEqual(self.store.get_compressed_summaries(0, 999), {})
        self.assertEqual(self.store.get_compressed_summaries(100, 999), {})
        # 查询窗口正好等于块 -> 可以用
        self.assertIn("10", self.store.get_compressed_summaries(100, 150))
        # 查询窗口比块小 -> 块完全覆盖 -> 可以用
        self.assertIn("10", self.store.get_compressed_summaries(110, 140))

    def test_muted_peer_skipped(self):
        self._seed()
        self.store.upsert_peer_tag(peer_id="10", peer_name="闲聊群", chat_type="group",
                                   label="营销", weight=0, source="model")
        stats = compress_window(self.store, self.cfg, FakeLlm(), window_start=0, window_end=999)
        self.assertEqual(stats.skipped_muted, 1)
        self.assertEqual(stats.peers, 0)

    def test_disabled_by_config(self):
        from dataclasses import replace

        self._seed()
        cfg = replace(self.cfg, general=replace(self.cfg.general, compress_low_weight=False))
        stats = compress_window(self.store, cfg, FakeLlm(), window_start=0, window_end=999)
        self.assertEqual(stats.peers, 0)

    def test_transcript_uses_summary(self):
        self._seed()
        from qqdigest.digest import group_messages

        msgs = self.store.fetch_messages(0, 999)
        grouped = group_messages(msgs)
        text, _ = render_transcript_ex(
            grouped, TZ, weights={"10": 1, "20": 8, "30": 100},
            summaries={"10": "- 全是闲聊，无要点"},
        )
        self.assertIn("预压缩摘要", text)
        self.assertIn("全是闲聊", text)
        self.assertNotIn("闲聊3", text, "低权重会话的原文应被摘要替代")
        self.assertIn("明天交作业", text, "高权重会话必须保留原文")


# ==========================================================================
# QQ 排版
# ==========================================================================


SAMPLE = """【重要提醒】（共 1 条）
⭐ [16:47] 陈总: 周五之前把季度预算表发我邮箱

【需要我回复】
- 陈总：要求周五之前把预算表发到邮箱

【待办 / 时间点】
- 王芳：补上 12 号的发票

【值得知道】
- 张工说他明天请假

【可以忽略】
- 群·摸鱼群：闲聊
- 群·广告群：刷屏"""


class TestFormatting(unittest.TestCase):
    def test_split_sections(self):
        got = formatting.split_sections(SAMPLE)
        self.assertEqual(got["需要我回复"], ["陈总：要求周五之前把预算表发到邮箱"])
        self.assertEqual(len(got["可以忽略"]), 2)

    def test_split_tolerates_missing_dash(self):
        """模型有时不写 '- ' 前缀，不能因此漏掉条目。"""
        got = formatting.split_sections("【可以忽略】\n群·A：闲聊\n群·B：刷屏")
        self.assertEqual(len(got["可以忽略"]), 2)

    def test_split_handles_slash_title(self):
        got = formatting.split_sections("【待办 / 时间点】\n- 交周报")
        self.assertEqual(got["待办"], ["交周报"])

    def test_empty_section_marked(self):
        got = formatting.split_sections("【需要我回复】\n无\n【值得知道】\n- 某事")
        self.assertEqual(got["需要我回复"], [])

    def test_format_has_icons_and_counts(self):
        out = formatting.format_digest(
            SAMPLE, window_label="10-12 12:00 → 13:00", msg_count=236, peer_count=25
        )
        self.assertIn("📮 消息简报", out)
        self.assertIn("10-12 12:00 → 13:00", out)
        self.assertIn("236 条", out)
        self.assertIn("🔴 需要我回复", out)
        self.assertIn("⭐ 重要提醒", out)
        self.assertNotIn("【需要我回复】", out, "旧式方括号标题应被替换")

    def test_important_not_duplicated_as_section(self):
        out = formatting.format_digest(SAMPLE, msg_count=10)
        self.assertEqual(out.count("⭐ 重要提醒"), 1)
        self.assertEqual(out.count("周五之前把季度预算表发我邮箱"), 1)

    def test_ignore_section_collapsed(self):
        many = "【可以忽略】\n" + "\n".join(f"- 群·群{i}：闲聊" for i in range(30))
        out = formatting.format_digest(many, msg_count=10)
        ignore = out.split("⚪ 可以忽略", 1)[1]
        self.assertLessEqual(len(ignore.splitlines()), 4, "可以忽略应被压成两三行")
        self.assertIn("详情见本地留档", out)

    def test_empty_content_is_safe(self):
        out = formatting.format_digest("", msg_count=0)
        self.assertIn("📮 消息简报", out)
        self.assertIn("无", out)

    def test_failure_notice(self):
        text = formatting.format_failure("2026-02-05T12:00", "模型连不上")
        self.assertIn("⚠️", text)
        self.assertIn("模型连不上", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
