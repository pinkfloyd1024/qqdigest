"""会话标签与权重的测试。

覆盖：规则判定优先级、模型输出容错解析、缓存与强制重分类、
权重排序、权重 0 排除、以及配置读写（含中文键的 TOML 细节）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqdigest import tagging  # noqa: E402
from qqdigest.config import ImportantCfg, TagsCfg, load_config  # noqa: E402
from qqdigest.digest import ordered_peer_blocks, render_transcript, render_transcript_ex  # noqa: E402
from qqdigest.llm import LlmResult  # noqa: E402
from qqdigest.store import Store  # noqa: E402
from qqdigest.toml_writer import TomlEditor, format_key  # noqa: E402
from tests.test_core import _msg, make_cfg  # noqa: E402

TZ = __import__("datetime").timezone.utc


class ScriptedLlm:
    """按顺序返回预设标签的假模型，用来测试解析与缓存。"""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    def chat(self, system: str, user: str, **kwargs) -> LlmResult:  # noqa: ARG002
        self.calls += 1
        text = self.outputs.pop(0) if self.outputs else "聊天群"
        return LlmResult(
            content=text,
            prompt_tokens=10,
            completion_tokens=3,
            reasoning_tokens=0,
            elapsed_sec=0.01,
            model="fake",
        )


class TestRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_keyword_hit(self):
        hit = tagging.match_keywords(self.cfg, "某某学院2026级新生群")
        self.assertIsNotNone(hit)
        label, word = hit  # type: ignore[misc]
        self.assertEqual(label, "学校")
        self.assertIn(word, "某某学院2026级新生群")

    def test_keyword_no_hit(self):
        self.assertIsNone(tagging.match_keywords(self.cfg, "小海豹群"))
        self.assertIsNone(tagging.match_keywords(self.cfg, ""))

    def test_manual_beats_keyword(self):
        cfg = self.cfg
        cfg2 = cfg.__class__(
            **{
                **{f: getattr(cfg, f) for f in cfg.__dataclass_fields__},
                "tags": TagsCfg(
                    labels=cfg.tags.labels,
                    weights=cfg.tags.weights,
                    keywords=cfg.tags.keywords,
                    manual={"555": "工作"},
                    default_label=cfg.tags.default_label,
                ),
            }
        )
        # 群名命中"学院"本应是学校，但手工指定优先级更高
        d = tagging.decide_by_rules(cfg2, "555", "某某学院群", "group")
        self.assertIsNotNone(d)
        self.assertEqual(d.label, "工作")  # type: ignore[union-attr]
        self.assertEqual(d.source, "manual")  # type: ignore[union-attr]

    def test_private_is_manual(self):
        d = tagging.decide_by_rules(self.cfg, "123", "某人", "private")
        self.assertIsNotNone(d)
        self.assertEqual(d.source, "manual")  # type: ignore[union-attr]

    def test_private_weight_is_high(self):
        self.assertEqual(self.cfg.tags.weight_for_peer("private", None), 100)
        self.assertEqual(self.cfg.tags.weight_for_peer("group", "营销"), 0)
        self.assertEqual(self.cfg.tags.weight_for_peer("group", "课程"), 10)


class TestModelParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, output: str):
        llm = ScriptedLlm([output])
        return tagging.classify_by_model(self.cfg, llm, "某群", ["消息1", "消息2"])

    def test_plain_label(self):
        label, _ = self._run("课程")
        self.assertEqual(label, "课程")

    def test_label_with_punctuation(self):
        for raw in ["课程。", "**课程**", "`课程`", " 课程 ", "标签：课程", "答案：\"课程\""]:
            label, _ = self._run(raw)
            self.assertEqual(label, "课程", f"未能解析 {raw!r}")

    def test_unknown_output_returns_none(self):
        label, detail = self._run("我不确定这个群是什么")
        self.assertIsNone(label)
        self.assertIn("无法解析", detail)

    def test_empty_output(self):
        label, _ = self._run("")
        self.assertIsNone(label)


class TestClassifyPeers(unittest.TestCase):
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
                _msg("a", 100, peer_id="100", peer_name="某某学院2026级新生群", chat_type="group"),
                _msg("b", 110, peer_id="100", chat_type="group", text="上课了"),
                _msg("c", 120, peer_id="200", peer_name="小海豹群", chat_type="group"),
                _msg("d", 130, peer_id="300", peer_name="某数码促销群", chat_type="group"),
                _msg("e", 140, peer_id="400", peer_name="某人", chat_type="private"),
            ]
        )

    def test_classify_uses_rules_then_model(self):
        self._seed()
        llm = ScriptedLlm(["技术", "聊天群"])  # 200 -> 技术, 300 -> 聊天群
        stats = tagging.classify_peers(self.store, self.cfg, llm)  # type: ignore[arg-type]
        tags = self.store.get_peer_tags()

        self.assertEqual(tags["100"]["label"], "学校")  # 关键词命中，未消耗模型
        self.assertEqual(tags["100"]["source"], "keyword")
        self.assertEqual(tags["400"]["label"], "私聊")
        self.assertEqual(tags["400"]["weight"], 100)
        self.assertEqual(llm.calls, 2, "只有两个无法用规则判断的群才该调模型")
        self.assertEqual(stats["keyword"], 1)

    def test_cached_tags_are_not_reclassified(self):
        self._seed()
        tagging.classify_peers(self.store, self.cfg, ScriptedLlm(["技术", "聊天群"]))  # type: ignore[arg-type]
        llm2 = ScriptedLlm(["课程", "课程", "课程"])
        stats = tagging.classify_peers(self.store, self.cfg, llm2)  # type: ignore[arg-type]
        self.assertEqual(llm2.calls, 0, "已缓存的不应重复调用模型")
        self.assertEqual(stats["skipped"], 4)  # 库里只有 4 个不同会话

    def test_force_reclassifies(self):
        self._seed()
        tagging.classify_peers(self.store, self.cfg, ScriptedLlm(["技术", "聊天群"]))  # type: ignore[arg-type]
        llm2 = ScriptedLlm(["课程", "课程"])
        tagging.classify_peers(self.store, self.cfg, llm2, force=True)  # type: ignore[arg-type]
        self.assertEqual(llm2.calls, 2)

    def test_model_only_mode_skips_llm(self):
        self._seed()
        llm = ScriptedLlm(["技术"])
        tagging.classify_peers(self.store, self.cfg, llm, use_model=False)  # type: ignore[arg-type]
        self.assertEqual(llm.calls, 0)
        tags = self.store.get_peer_tags()
        self.assertEqual(tags["200"]["label"], self.cfg.tags.default_label)
        self.assertEqual(tags["200"]["source"], "default")

    def test_manual_label_survives_reclassify(self):
        self._seed()
        tagging.classify_peers(self.store, self.cfg, ScriptedLlm(["技术", "聊天群"]))  # type: ignore[arg-type]
        self.assertTrue(self.store.set_peer_label("200", "工作", 10))
        self.store.set_peer_label("200", "工作", 10)
        row = self.store.get_peer_tag("200")
        assert row is not None
        self.assertEqual(row["label"], "工作")
        self.assertEqual(row["source"], "manual")


class TestWeightedRendering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _grouped(self):
        # 营销群消息最多，但权重 0；课程群消息少，权重 10
        return {
            "1": [_msg(f"m{i}", 100 + i, peer_id="1", peer_name="营销群", chat_type="group") for i in range(50)],
            "2": [_msg("c", 200, peer_id="2", peer_name="课程群", chat_type="group", text="明天交作业")],
        }

    def test_weight_orders_blocks(self):
        blocks = ordered_peer_blocks(self._grouped(), TZ, weights={"1": 0, "2": 10})
        self.assertEqual(blocks[0]["label"], "群·课程群")
        self.assertEqual(blocks[0]["weight"], 10)

    def test_weight_zero_excluded(self):
        text, info = render_transcript_ex(
            self._grouped(), TZ, weights={"1": 0, "2": 10}, exclude_weight_zero=True
        )
        self.assertIn("课程群", text)
        self.assertNotIn("营销群", text, "权重 0 的会话不应进入上下文")
        self.assertEqual(info["excluded_peers"], 1)
        self.assertEqual(info["excluded_msgs"], 50)
        self.assertIn("群·营销群", info["excluded_labels"])

    def test_zero_exclusion_can_be_disabled(self):
        text, info = render_transcript_ex(
            self._grouped(), TZ, weights={"1": 0, "2": 10}, exclude_weight_zero=False
        )
        self.assertIn("营销群", text)
        self.assertEqual(info["excluded_peers"], 0)

    def test_no_weights_falls_back_to_old_behaviour(self):
        text = render_transcript(self._grouped(), TZ)
        self.assertIn("营销群", text)
        self.assertIn("课程群", text)

    def test_important_still_beats_weight(self):
        grouped = {
            "1": [_msg("g", 100, peer_id="1", peer_name="聊天群", chat_type="group", text="紧急", important=1)],
            "2": [_msg("c", 100, peer_id="2", peer_name="课程群", chat_type="group")],
        }
        blocks = ordered_peer_blocks(grouped, TZ, weights={"1": 1, "2": 10})
        self.assertEqual(blocks[0]["label"], "群·聊天群", "重要标记仍应最优先")


class TestTagsConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = TagsCfg()
        self.assertIn("课程", cfg.labels)
        # 数据类默认值必须是真实权重，不能是空的——否则不经 config.toml 的
        # 调用路径会让所有权重退化成 1，"权重 0 排除" 直接失效
        self.assertEqual(cfg.weight_of("课程"), 10)
        self.assertEqual(cfg.weight_of("营销"), 0)
        self.assertEqual(cfg.weight_of("未知标签"), 1)  # 未定义标签回落到 default
        self.assertEqual(cfg.weight_for_peer("private", None), 100)

    def test_unknown_weight_key_rejected(self):
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
                    "[tags]",
                    'labels = ["课程"]',
                    "[tags.weights]",
                    '"课程" = 5',
                    '"不存在" = 3',
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError) as ctx:
            load_config(root / "config.toml")
        self.assertIn("未在 labels 中定义", str(ctx.exception))

    def test_keyword_label_must_exist(self):
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
                    "[tags]",
                    'labels = ["课程"]',
                    "[tags.keywords]",
                    '"不存在" = ["x"]',
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            load_config(root / "config.toml")

    def test_real_config_has_tags(self):
        cfg = load_config(Path(__file__).resolve().parent.parent / "config.toml")
        self.assertTrue(cfg.tags.enabled)
        self.assertEqual(cfg.tags.weight_of("营销"), 0)
        self.assertGreater(cfg.tags.weight_of("课程"), cfg.tags.weight_of("聊天群"))


class TestTomlChineseKeys(unittest.TestCase):
    """TOML 裸键只允许 A-Za-z0-9_-，中文键必须加引号——踩过这个坑。"""

    def test_format_key(self):
        self.assertEqual(format_key("abc"), "abc")
        self.assertEqual(format_key("a_b-1"), "a_b-1")
        self.assertEqual(format_key("课程"), '"课程"')
        self.assertEqual(format_key("a b"), '"a b"')

    def test_write_into_chinese_subtable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        path = root / "config.toml"
        path.write_text(
            "\n".join(
                [
                    "[onebot]",
                    'self_id = "1000"',
                    "[target]",
                    'send_to = "2000"',
                    "[tags]",
                    'labels = ["课程", "营销"]',
                    "[tags.weights]",
                    '# 权重注释',
                    '"课程" = 10',
                    '"营销" = 0',
                ]
            ),
            encoding="utf-8",
        )
        ed = TomlEditor(path)
        ed.set("tags.weights", "课程", 20)
        ed.set("tags", "private_weight", 120)
        ed.save()

        cfg = load_config(path)
        self.assertEqual(cfg.tags.weight_of("课程"), 20)
        self.assertEqual(cfg.tags.private_weight, 120)
        self.assertIn("# 权重注释", path.read_text(encoding="utf-8"))
        self.assertIn('"课程" = 20', path.read_text(encoding="utf-8"))

    def test_unknown_weight_key_is_rejected(self):
        """往权重表里塞一个没在 labels 中定义的标签，必须被明确拒绝。

        这条校验很关键：否则界面手改权重时可能写出「权重表里有、标签集里没有」
        的配置，行为会变得不可预期。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        path = root / "config.toml"
        path.write_text(
            "\n".join(
                [
                    "[onebot]",
                    'self_id = "1000"',
                    "[target]",
                    'send_to = "2000"',
                    "[tags]",
                    'labels = ["课程"]',
                    "[tags.weights]",
                    '"课程" = 10',
                ]
            ),
            encoding="utf-8",
        )
        ed = TomlEditor(path)
        ed.set("tags.weights", "新标签", 5)
        ed.save()
        self.assertIn('"新标签" = 5', path.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError) as ctx:
            load_config(path)
        self.assertIn("未在 labels 中定义", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
