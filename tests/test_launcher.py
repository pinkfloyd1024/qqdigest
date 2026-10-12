"""启动器与只读访问的回归测试。

这些测试是为了锁死两个真实踩过的坑：

1. **批处理启动器必须纯 ASCII。**
   cmd.exe 会按字节偏移重新读取 .bat，文件里出现非 ASCII 字节时（尤其是配合
   代码页切换）会把后面的行撕碎，脚本于是落进错误分支并卡在隐藏的 ``pause`` 上，
   表现为「双击没反应」。本机实测：GBK 与 UTF-8 中文注释都会让它失效，纯 ASCII 正常。

2. **PowerShell 脚本必须带 UTF-8 BOM。**
   Windows PowerShell 5.1 读取无 BOM 的文件时按 ANSI 解释，中文会变乱码。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qqdigest.store import Store  # noqa: E402
from tests.test_core import _msg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class TestBatchLauncher(unittest.TestCase):
    def setUp(self):
        self.bat = ROOT / "启动界面.bat"

    def test_exists(self):
        self.assertTrue(self.bat.is_file(), f"缺少启动器：{self.bat}")

    def test_is_pure_ascii(self):
        raw = self.bat.read_bytes()
        offenders = [b for b in raw if b > 127]
        self.assertEqual(
            offenders,
            [],
            f"启动器含有 {len(offenders)} 个非 ASCII 字节。"
            "cmd.exe 会因此读取错位并静默挂起，必须保持纯 ASCII。",
        )

    def test_does_not_switch_code_page(self):
        text = self.bat.read_text(encoding="ascii")
        self.assertNotIn(
            "chcp",
            text,
            "启动器里不能出现 chcp：切换代码页会让 cmd.exe 的文件偏移错位。",
        )

    def test_references_entry_and_preflight(self):
        text = self.bat.read_text(encoding="ascii")
        self.assertIn("gui.py", text)
        self.assertIn("--preflight", text)

    def test_uses_start_for_detached_gui(self):
        text = self.bat.read_text(encoding="ascii")
        self.assertIn("start", text)
        self.assertIn("pythonw", text)


class TestPowerShellScripts(unittest.TestCase):
    def test_ps1_has_utf8_bom(self):
        ps1 = ROOT / "tools" / "register_task.ps1"
        if not ps1.is_file():
            self.skipTest("没有 PowerShell 脚本")
        raw = ps1.read_bytes()
        self.assertEqual(
            raw[:3],
            b"\xef\xbb\xbf",
            "PowerShell 5.1 会把无 BOM 的文件按 ANSI 解释，中文会乱码，必须写 BOM。",
        )


class TestReadOnlyStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ro.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_none_and_creates_nothing(self):
        self.assertIsNone(Store.open_readonly(self.db))
        self.assertFalse(self.db.exists(), "只读打开不应创建数据库文件")

    def test_reads_existing_data(self):
        store = Store(self.db)
        store.insert_messages([_msg("a", 100, text="只读可见")])
        store.close()

        ro = Store.open_readonly(self.db)
        self.assertIsNotNone(ro)
        assert ro is not None
        try:
            self.assertEqual(ro.count_messages(), 1)
            self.assertEqual(ro.fetch_messages(0, 200)[0]["text"], "只读可见")
            self.assertIsInstance(ro.recent_digests(3), list)
            self.assertIsInstance(ro.stats_by_peer(0, 1000), list)
            self.assertIsNone(ro.digest_by_slot("不存在"))
        finally:
            ro.close()

    def test_open_readonly_upgrades_outdated_schema(self):
        """契约（已更新）：只读打开**结构过旧的库**时会先做一次幂等升级。

        早期实现是"只读绝不迁移"，结果是界面读到缺列的旧库直接崩，
        而用户不知道要先启动一次后端。现在改为自动升级，但**数据必须完好**，
        并且**结构已经是最新时不做任何改动**（见下一个用例）。
        """
        old_schema = """
        CREATE TABLE messages (
            msg_id TEXT PRIMARY KEY, ts INTEGER NOT NULL, direction INTEGER NOT NULL,
            chat_type TEXT NOT NULL, peer_id TEXT, peer_name TEXT, sender_id TEXT,
            sender_name TEXT, content_type TEXT NOT NULL DEFAULT 'text', text TEXT,
            highlight INTEGER NOT NULL DEFAULT 0, collected_at INTEGER NOT NULL);
        CREATE TABLE digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, slot TEXT UNIQUE, window_start INTEGER NOT NULL,
            window_end INTEGER NOT NULL, fire_at INTEGER NOT NULL, model TEXT,
            prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
            msg_count INTEGER DEFAULT 0, content TEXT, state TEXT NOT NULL DEFAULT 'pending',
            error TEXT, run_file TEXT, created_at INTEGER NOT NULL, sent_at INTEGER);
        CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER NOT NULL);
        """
        import sqlite3

        conn = sqlite3.connect(self.db)
        conn.executescript(old_schema)
        conn.execute(
            "INSERT INTO messages (msg_id, ts, direction, chat_type, peer_id, text, collected_at)"
            " VALUES ('old1', 100, 0, 'private', '9', '旧数据不能丢', 100)"
        )
        conn.commit()
        conn.close()

        ro = Store.open_readonly(self.db)
        assert ro is not None
        try:
            cols = [r[1] for r in ro._conn.execute("PRAGMA table_info(messages)")]
            self.assertIn("muted", cols, "缺列的旧库应被自动升级")
            self.assertIn("important", cols)
            self.assertEqual(ro.count_messages(), 1, "升级不能丢数据")
            self.assertEqual(ro.fetch_messages(0, 200)[0]["text"], "旧数据不能丢")
            # 视图也应建好
            views = [
                r[0]
                for r in ro._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='view'"
                ).fetchall()
            ]
            self.assertIn("v_group_messages", views)
            self.assertIn("v_private_messages", views)
        finally:
            ro.close()

    def test_open_readonly_does_not_touch_current_schema(self):
        """结构已是最新时，只读打开不得改动任何东西。"""
        store = Store(self.db)
        store.insert_messages([_msg("a", 100, text="保持原样")])
        store.close()

        before = self.db.read_bytes()
        ro = Store.open_readonly(self.db)
        assert ro is not None
        try:
            self.assertEqual(ro.count_messages(), 1)
        finally:
            ro.close()
        self.assertEqual(self.db.read_bytes(), before, "结构已最新时不应写库")

    def test_write_attempt_is_rejected(self):
        import sqlite3

        store = Store(self.db)
        store.close()
        ro = Store.open_readonly(self.db)
        assert ro is not None
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro._conn.execute(
                    "INSERT INTO messages (msg_id, ts, direction, chat_type,"
                    " content_type, collected_at) VALUES ('x', 1, 0, 'private', 'text', 1)"
                )
        finally:
            ro.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
