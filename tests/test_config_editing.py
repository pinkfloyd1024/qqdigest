"""新增能力测试：配置写回、重要规则、提示词覆盖、数据库迁移、简报渲染。

运行： python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqdigest.config import ImportantCfg, PromptCfg, load_config  # noqa: E402
from qqdigest.digest import ordered_peer_blocks, render_peer_block, render_transcript  # noqa: E402
from qqdigest.llm import estimate_tokens  # noqa: E402
from qqdigest.onebot import matches_important, parse_event  # noqa: E402
from qqdigest.prompts import (  # noqa: E402
    MAP_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    is_using_default_system_prompt,
    resolve_map_system_prompt,
    resolve_system_prompt,
)
from qqdigest.store import Store  # noqa: E402
from qqdigest.toml_writer import (  # noqa: E402
    TomlEditError,
    TomlEditor,
    format_string,
    format_value,
)
from tests.test_core import FakeLlm, make_cfg, _msg  # noqa: E402

SAMPLE_TOML = """\
# 顶部注释
[general]
# 时区注释
timezone = "Asia/Shanghai"
schedule = ["12:00", "18:00"]  # 行尾注释
dry_run = true

[filter]
collect_group = false   # 群开关
group_whitelist = []
peer_blacklist = [
    "111",
    "222",
]

[prompt]
# 提示词留空表示用内置默认
system_prompt = ""
extra_rules = ""
"""


# ==========================================================================
# TOML 写回
# ==========================================================================


class TestTomlFormat(unittest.TestCase):
    def test_simple_values(self):
        self.assertEqual(format_value(True), "true")
        self.assertEqual(format_value(False), "false")
        self.assertEqual(format_value(42), "42")
        self.assertEqual(format_value(0.2), "0.2")
        self.assertEqual(format_value(["a", "b"]), '["a", "b"]')
        self.assertEqual(format_value([]), "[]")

    def test_string_with_backslash_uses_literal(self):
        out = format_string(r"C:\path\to\x")
        self.assertTrue(out.startswith("'"), out)
        self.assertIn(r"C:\path\to\x", out)

    def test_string_with_newline(self):
        out = format_string("第一行\n第二行")
        self.assertTrue(out.startswith("'''"))
        parsed = tomllib.loads(f"k = {out}")
        self.assertEqual(parsed["k"], "第一行\n第二行")

    def test_string_ending_with_quote(self):
        out = format_string("结尾是单引号'")
        parsed = tomllib.loads(f"k = {out}")
        self.assertEqual(parsed["k"], "结尾是单引号'")

    def test_unsupported_type(self):
        with self.assertRaises(TomlEditError):
            format_value({"a": 1})


class TestTomlEditor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(SAMPLE_TOML, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _edit(self, values):
        ed = TomlEditor(self.path)
        ed.set_all(values)
        return ed

    def test_replace_scalar_keeps_comments(self):
        ed = self._edit({("general", "dry_run"): False})
        ed.save()
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# 顶部注释", text)
        self.assertIn("# 时区注释", text)
        self.assertFalse(tomllib.loads(text)["general"]["dry_run"])

    def test_replace_inline_comment_preserved(self):
        ed = self._edit({("general", "schedule"): ["09:00"]})
        ed.save()
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# 行尾注释", text)
        self.assertEqual(tomllib.loads(text)["general"]["schedule"], ["09:00"])

    def test_replace_multiline_array(self):
        ed = self._edit({("filter", "peer_blacklist"): ["333"]})
        ed.save()
        data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["filter"]["peer_blacklist"], ["333"])

    def test_add_key_to_existing_section(self):
        ed = self._edit({("filter", "only_incoming"): True})
        ed.save()
        data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(data["filter"]["only_incoming"])
        self.assertIn("# 群开关", self.path.read_text(encoding="utf-8"))

    def test_create_missing_section(self):
        ed = self._edit({("important", "keywords"): ["紧急"]})
        ed.save()
        data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["important"]["keywords"], ["紧急"])

    def test_multiline_prompt_roundtrip(self):
        prompt = "你是助手。\n\n规则一：忽略寒暄。\n路径示例 C:\\temp\\x"
        ed = self._edit({("prompt", "system_prompt"): prompt})
        ed.save()
        data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["prompt"]["system_prompt"], prompt)

    def test_backup_created(self):
        ed = self._edit({("general", "dry_run"): False})
        backup = ed.save()
        self.assertTrue(backup.endswith(".bak"))
        self.assertTrue(Path(backup).is_file())

    def test_no_change_no_write(self):
        ed = self._edit({("general", "dry_run"): True})
        self.assertEqual(ed.save(), "")

    def test_render_does_not_touch_disk(self):
        ed = self._edit({("general", "dry_run"): False})
        ed.render()
        self.assertIn("dry_run = true", self.path.read_text(encoding="utf-8"))

    def test_invalid_existing_file_rejected(self):
        self.path.write_text("this is not toml", encoding="utf-8")
        with self.assertRaises(TomlEditError):
            TomlEditor(self.path)

    def test_many_values_at_once(self):
        ed = self._edit(
            {
                ("general", "schedule"): ["12:00", "18:00", "22:00"],
                ("filter", "collect_group"): True,
                ("filter", "group_whitelist"): ["555", "666"],
                ("prompt", "extra_rules"): "金额一律标元",
                ("brandnew", "x"): 1,
            }
        )
        ed.save()
        data = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["general"]["schedule"], ["12:00", "18:00", "22:00"])
        self.assertTrue(data["filter"]["collect_group"])
        self.assertEqual(data["filter"]["group_whitelist"], ["555", "666"])
        self.assertEqual(data["prompt"]["extra_rules"], "金额一律标元")
        self.assertEqual(data["brandnew"]["x"], 1)


class TestRealConfigRoundTrip(unittest.TestCase):
    """对项目真实 config.toml 做一次完整往返，确保 GUI 保存不会破坏它。"""

    def test_roundtrip(self):
        src = Path(__file__).resolve().parent.parent / "config.toml"
        before = load_config(src)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "config.toml"
            work.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

            ed = TomlEditor(work)
            ed.set("important", "keywords", ["紧急", "转账"])
            ed.set("important", "peers", ["10001"])
            ed.set("filter", "group_whitelist", ["555000111"])
            ed.set("prompt", "extra_rules", "金额一律标注「元」。\n不要总结广告群。")
            ed.set("llm", "max_tokens", 4000)
            ed.save()

            after = load_config(work)
            self.assertEqual(after.important.keywords, ("紧急", "转账"))
            self.assertEqual(after.important.peers, frozenset({"10001"}))
            self.assertEqual(after.filter.group_whitelist, ("555000111",))
            self.assertIn("广告群", after.prompt.extra_rules)
            self.assertEqual(after.llm.max_tokens, 4000)
            # 未被修改的项保持不变
            self.assertEqual(after.onebot.self_id, before.onebot.self_id)
            self.assertEqual(after.general.schedule, before.general.schedule)

            # 注释仍在
            text = work.read_text(encoding="utf-8")
            self.assertIn("# ★ 强烈建议先用 true", text)
            self.assertIn("# 命中条件（满足任一即可）", text)


# ==========================================================================
# 重要规则
# ==========================================================================


class TestImportantRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = make_cfg(Path(self.tmp.name))
        self.cfg = replace(
            base,
            important=ImportantCfg(keywords=("紧急", "Transfer"), peers=frozenset({"8888"})),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _ev(self, text, user_id=1234):
        return {
            "post_type": "message",
            "message_type": "private",
            "message_id": 1,
            "user_id": user_id,
            "self_id": 1000,
            "time": 1700000000,
            "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"user_id": user_id, "nickname": "某人"},
        }

    def test_keyword_hit(self):
        self.assertTrue(matches_important("这个事情很紧急", "1234", self.cfg))
        rec, _ = parse_event(self._ev("这个事情很紧急"), self.cfg)
        assert rec is not None
        self.assertEqual(rec["important"], 1)

    def test_keyword_case_insensitive(self):
        self.assertTrue(matches_important("please TRANSFER now", "1234", self.cfg))
        self.assertTrue(matches_important("please transfer now", "1234", self.cfg))

    def test_peer_hit(self):
        self.assertTrue(matches_important("在吗", "8888", self.cfg))
        rec, _ = parse_event(self._ev("在吗", user_id=8888), self.cfg)
        assert rec is not None
        self.assertEqual(rec["important"], 1)

    def test_no_hit(self):
        self.assertFalse(matches_important("中午吃啥", "1234", self.cfg))
        rec, _ = parse_event(self._ev("中午吃啥"), self.cfg)
        assert rec is not None
        self.assertEqual(rec["important"], 0)

    def test_disabled_when_no_rules(self):
        cfg = replace(self.cfg, important=ImportantCfg())
        self.assertFalse(matches_important("紧急", "8888", cfg))


# ==========================================================================
# 提示词覆盖
# ==========================================================================


class TestPromptResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_when_empty(self):
        self.assertTrue(is_using_default_system_prompt(self.base))
        self.assertEqual(resolve_system_prompt(self.base), SYSTEM_PROMPT)
        self.assertEqual(resolve_map_system_prompt(self.base), MAP_SYSTEM_PROMPT)

    def test_custom_overrides_default(self):
        cfg = replace(self.base, prompt=PromptCfg(system_prompt="我的提示词"))
        self.assertEqual(resolve_system_prompt(cfg), "我的提示词")
        self.assertFalse(is_using_default_system_prompt(cfg))

    def test_extra_rules_appended(self):
        cfg = replace(self.base, prompt=PromptCfg(extra_rules="金额标元"))
        out = resolve_system_prompt(cfg)
        self.assertTrue(out.startswith(SYSTEM_PROMPT))
        self.assertIn("金额标元", out)
        self.assertIn("补充规则", out)

    def test_important_rules_do_not_pollute_system_prompt(self):
        """重要规则不应改写系统提示词。

        实测教训：把「重要提醒」规则追加进系统提示词会让 14B 模型丢内容，
        因此改为代码确定性生成（见 build_important_header）。
        """
        with_rules = replace(
            self.base, important=ImportantCfg(keywords=("紧急",), mark_prefix="★")
        )
        self.assertEqual(resolve_system_prompt(with_rules), SYSTEM_PROMPT)
        self.assertNotIn("【重要提醒】", resolve_system_prompt(with_rules))

    def test_map_prompt_custom(self):
        cfg = replace(self.base, prompt=PromptCfg(map_system_prompt="我的摘要提示词"))
        self.assertEqual(resolve_map_system_prompt(cfg), "我的摘要提示词")


# ==========================================================================
# 简报渲染：重要消息优先
# ==========================================================================


class TestImportantRendering(unittest.TestCase):
    def test_mark_prefix_in_transcript(self):
        msgs = [_msg("a", 100, text="紧急：服务器挂了", important=1)]
        text = render_transcript({m["peer_id"]: msgs for m in msgs}, tz=_utc(), mark="★")
        self.assertIn("★ 紧急：服务器挂了", text)

    def test_no_mark_when_not_important(self):
        msgs = [_msg("a", 100, text="普通消息")]
        block = render_peer_block(msgs, _utc(), mark="★")
        self.assertNotIn("★", block)

    def test_important_conversations_sorted_first(self):
        grouped = {
            "1": [_msg("a", 100, peer_id="1", peer_name="普通人", text="闲聊")],
            "2": [_msg("b", 100, peer_id="2", peer_name="老板", text="紧急", important=1)],
        }
        blocks = ordered_peer_blocks(grouped, _utc())
        self.assertEqual(blocks[0]["label"], "老板")
        self.assertEqual(blocks[1]["label"], "普通人")

    def test_highlight_breaks_tie(self):
        grouped = {
            "1": [_msg("a", 100, peer_id="1", peer_name="安静", text="嗯")],
            "2": [
                _msg("b", 100, peer_id="2", peer_name="提问者", text="在吗?", highlight=1)
            ],
        }
        blocks = ordered_peer_blocks(grouped, _utc())
        self.assertEqual(blocks[0]["label"], "提问者")

    def test_private_ranks_above_noisy_group(self):
        """★ 关键回归：私聊必须排在消息量更大的群聊前面。

        真实踩坑：某窗口 32 条消息里 30 条来自一个游戏群，唯一需要行动的
        两条私聊被排到最后，模型读完整屏闲聊后把它们也丢进了「可以忽略」。
        排序不能只按音量，要按信号强度。
        """
        grouped = {
            "100": [
                _msg(f"g{i}", 100 + i, peer_id="100", peer_name="游戏群", text="闲聊", chat_type="group")
                for i in range(30)
            ],
            "200": [
                _msg("p1", 100, peer_id="200", peer_name="wiki", text="你好，这个事情得你办一下")
            ],
        }
        blocks = ordered_peer_blocks(grouped, _utc())
        self.assertEqual(blocks[0]["label"], "wiki", "私聊会话必须排在最前")
        self.assertEqual(blocks[0]["chat_type"], "private")

    def test_important_beats_private(self):
        grouped = {
            "100": [_msg("p", 100, peer_id="100", peer_name="私聊者", text="在吗")],
            "200": [
                _msg("g", 100, peer_id="200", peer_name="重要群", text="紧急", chat_type="group", important=1)
            ],
        }
        blocks = ordered_peer_blocks(grouped, _utc())
        self.assertEqual(blocks[0]["label"], "群·重要群", "重要会话优先于私聊")

    def test_transcript_has_private_and_group_sections(self):
        grouped = {
            "100": [_msg("g", 100, peer_id="100", peer_name="某群", chat_type="group")],
            "200": [_msg("p", 100, peer_id="200", peer_name="某好友")],
        }
        text = render_transcript(grouped, _utc())
        self.assertIn("【私聊消息】", text)
        self.assertIn("【群聊消息】", text)
        self.assertLess(
            text.index("【私聊消息】"), text.index("【群聊消息】"), "私聊段必须在前"
        )

    def test_transcript_omits_empty_section(self):
        only_private = {"200": [_msg("p", 100, peer_id="200", peer_name="某好友")]}
        text = render_transcript(only_private, _utc())
        self.assertIn("【私聊消息】", text)
        self.assertNotIn("【群聊消息】", text)


def _utc():
    from datetime import timezone

    return timezone.utc


# ==========================================================================
# 代码生成的重要提醒小节
# ==========================================================================


class TestImportantHeader(unittest.TestCase):
    def test_empty_when_no_flagged(self):
        from qqdigest.digest import build_important_header

        msgs = [_msg("a", 100), _msg("b", 200)]
        self.assertEqual(build_important_header(msgs, _utc()), "")

    def test_lists_flagged_in_time_order(self):
        from qqdigest.digest import build_important_header

        msgs = [
            _msg("b", 200, text="第二条", important=1, sender_name="乙"),
            _msg("a", 100, text="第一条", important=1, sender_name="甲"),
            _msg("c", 300, text="普通", important=0, sender_name="丙"),
        ]
        out = build_important_header(msgs, _utc(), mark="★")
        self.assertTrue(out.startswith("【重要提醒】（共 2 条）"))
        self.assertLess(out.index("第一条"), out.index("第二条"))
        self.assertNotIn("普通", out)
        self.assertIn("★", out)

    def test_dedupes_identical_text_from_same_peer(self):
        from qqdigest.digest import build_important_header

        msgs = [
            _msg("a", 100, text="重复内容", important=1),
            _msg("b", 200, text="重复内容", important=1),
        ]
        out = build_important_header(msgs, _utc())
        self.assertEqual(out.count("重复内容"), 1)

    def test_limit_note(self):
        from qqdigest.digest import build_important_header

        msgs = [
            _msg(f"m{i}", 100 + i, text=f"重要{i}", important=1) for i in range(25)
        ]
        out = build_important_header(msgs, _utc(), limit=5)
        self.assertIn("其余", out)

    def test_self_messages_labelled_wo(self):
        from qqdigest.digest import build_important_header

        msgs = [_msg("a", 100, text="我说的", important=1, direction=1)]
        out = build_important_header(msgs, _utc())
        self.assertIn("我:", out)


# ==========================================================================
# 分块（map-reduce）打包与归并
# ==========================================================================


class TestPackAndMerge(unittest.TestCase):
    """锁死两个真实踩过的坑：

    1. 早期实现「一个会话一次模型调用」，48 个会话要 226 秒；
    2. 早期实现在超预算时**直接丢弃**多余的摘要块，实测 48 块丢掉 39 块，
       大量会话的内容永久消失。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))
        self.llm = FakeLlm("- 要点")

    def tearDown(self):
        self.tmp.cleanup()

    def _entries(self, n: int, chars: int = 200):
        return [
            {
                "label": f"会话{i}",
                "block": f"【会话】会话{i}\n" + ("内容" * (chars // 2)),
                "chat_type": "group",
            }
            for i in range(n)
        ]

    def test_pack_combines_multiple_conversations(self):
        from qqdigest.digest import _pack_blocks

        packs = _pack_blocks(self._entries(20, 200), map_budget=2000)
        self.assertLess(len(packs), 20, "应该把多个会话打包进同一次调用")
        # 每个打包块都不能超过预算太多
        for _, text in packs:
            self.assertLess(estimate_tokens(text), 2600)
        # 所有会话都要出现在某个包里，一个都不能漏
        joined = "".join(t for _, t in packs)
        for i in range(20):
            self.assertIn(f"会话{i}", joined)

    def test_pack_never_loses_content(self):
        from qqdigest.digest import _pack_blocks

        entries = self._entries(30, 500)
        packs = _pack_blocks(entries, map_budget=1500)
        joined = "".join(t for _, t in packs)
        for i in range(30):
            self.assertIn(f"会话{i}", joined)

    def test_merge_reduces_instead_of_dropping(self):
        """超预算时必须归并，而不是丢掉后面的摘要。"""
        from qqdigest.digest import _merge_until_fits

        summaries = [f"【会话{i}】\n" + ("要点内容" * 60) for i in range(20)]
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "model": "fake"}
        merged, rounds = _merge_until_fits(
            self.cfg, self.llm, summaries, budget=1500, map_system="sys", usage=usage
        )
        self.assertGreater(rounds, 0, "应触发归并")
        self.assertLess(len(merged), len(summaries), "归并后条数应减少")
        self.assertGreaterEqual(len(merged), 1)
        # 关键：不是丢弃，所以模型被调用过（说明内容被重写压缩）
        self.assertGreater(self.llm.calls, 0, "归并应该调用模型压缩，而不是直接丢")

    def test_merge_noop_when_within_budget(self):
        from qqdigest.digest import _merge_until_fits

        summaries = ["短摘要"]
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "model": "fake"}
        merged, rounds = _merge_until_fits(
            self.cfg, self.llm, summaries, budget=100000, map_system="sys", usage=usage
        )
        self.assertEqual(rounds, 0)
        self.assertEqual(merged, summaries)

    def test_split_block_preserves_header(self):
        from qqdigest.digest import _split_block

        block = "【会话】某人\n" + "\n".join(f"[12:0{i%10}] 甲: 第{i}行" for i in range(50))
        pieces = _split_block(block, 200)
        self.assertGreater(len(pieces), 1)
        for p in pieces:
            self.assertTrue(p.startswith("【会话】某人"), "每个切片都要保留会话头")
        joined = "\n".join(pieces)
        for i in range(50):
            self.assertIn(f"第{i}行", joined)


# ==========================================================================
# 数据库迁移
# ==========================================================================

_OLD_SCHEMA = """
CREATE TABLE messages (
    msg_id TEXT PRIMARY KEY, ts INTEGER NOT NULL, direction INTEGER NOT NULL,
    chat_type TEXT NOT NULL, peer_id TEXT, peer_name TEXT, sender_id TEXT, sender_name TEXT,
    content_type TEXT NOT NULL DEFAULT 'text', text TEXT,
    highlight INTEGER NOT NULL DEFAULT 0, collected_at INTEGER NOT NULL);
CREATE TABLE digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, slot TEXT UNIQUE, window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL, fire_at INTEGER NOT NULL, model TEXT,
    prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0, msg_count INTEGER DEFAULT 0,
    content TEXT, state TEXT NOT NULL DEFAULT 'pending', error TEXT, run_file TEXT,
    created_at INTEGER NOT NULL, sent_at INTEGER);
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER NOT NULL);
"""


class TestStoreMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(_OLD_SCHEMA)
        conn.execute(
            "INSERT INTO messages (msg_id, ts, direction, chat_type, peer_id, peer_name,"
            " sender_id, sender_name, text, highlight, collected_at)"
            " VALUES ('old1', 100, 0, 'private', '9', '老会话', '9', '老会话', '旧数据', 0, 100)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_db_opens_and_migrates(self):
        store = Store(self.db)
        try:
            cols = [r[1] for r in store._conn.execute("PRAGMA table_info(messages)")]
            self.assertIn("important", cols)
            self.assertEqual(store.count_messages(), 1)
            self.assertEqual(store.fetch_messages(0, 200)[0]["text"], "旧数据")
            # 索引也应已建立
            idx = [r[1] for r in store._conn.execute("PRAGMA index_list(messages)")]
            self.assertIn("idx_msg_important", idx)
        finally:
            store.close()

    def test_idempotent(self):
        for _ in range(3):
            store = Store(self.db)
            store.close()
        store = Store(self.db)
        try:
            self.assertEqual(store.count_messages(), 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
