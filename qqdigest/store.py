"""本地暂存层：SQLite 存储。

三张表：
- ``messages``  消息暂存，``msg_id`` 主键天然去重
- ``digests``   每档简报及其状态机（pending -> sent / failed），``slot`` 唯一防止重复生成
- ``state``     键值状态，记录采集窗口水位线等

线程安全：HTTP 上报线程写、调度线程读写，因此使用单连接 + 互斥锁，并开启 WAL。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger("qqdigest.store")

SCHEMA = """
-- 建表与索引分开：必须先建表，再迁移补齐新列，最后才能建依赖新列的索引
CREATE TABLE IF NOT EXISTS messages (
    msg_id       TEXT PRIMARY KEY,
    ts           INTEGER NOT NULL,
    direction    INTEGER NOT NULL,
    chat_type    TEXT    NOT NULL,
    peer_id      TEXT,
    peer_name    TEXT,
    sender_id    TEXT,
    sender_name  TEXT,
    content_type TEXT    NOT NULL DEFAULT 'text',
    text         TEXT,
    highlight    INTEGER NOT NULL DEFAULT 0,
    important    INTEGER NOT NULL DEFAULT 0,
    muted        INTEGER NOT NULL DEFAULT 0,
    collected_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    slot              TEXT UNIQUE,
    window_start      INTEGER NOT NULL,
    window_end        INTEGER NOT NULL,
    fire_at           INTEGER NOT NULL,
    model             TEXT,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    msg_count         INTEGER DEFAULT 0,
    content           TEXT,
    state             TEXT NOT NULL DEFAULT 'pending',
    error             TEXT,
    run_file          TEXT,
    created_at        INTEGER NOT NULL,
    sent_at           INTEGER
);

CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS peer_tags (
    peer_id    TEXT PRIMARY KEY,
    peer_name  TEXT,
    chat_type  TEXT,
    label      TEXT NOT NULL,
    weight     INTEGER NOT NULL DEFAULT 1,
    source     TEXT,
    detail     TEXT,
    msg_count  INTEGER DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS compressed_blocks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id      TEXT NOT NULL,
    peer_name    TEXT,
    window_start INTEGER NOT NULL,
    window_end   INTEGER NOT NULL,
    summary      TEXT NOT NULL,
    msg_count    INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    UNIQUE(peer_id, window_start, window_end)
);
"""

#: 视图：把群聊/私聊按用户要求的字段组织成两张"逻辑上独立的表"。
#:
#: 为什么不真的拆成两张物理表：``chat_type`` 已经在表里且有索引，拆表会让每一次
#: 窗口查询都要 UNION、去重逻辑写两遍、统计要跨表聚合，而收益只是"看起来分开"。
#: 视图让查询和报表就像两张独立的表，底层数据不碎片化。
#: 两个视图都自动排除 muted（权重 0）的消息。
SCHEMA_VIEWS = """
CREATE VIEW IF NOT EXISTS v_group_messages AS
SELECT
    ts                              AS ts,
    datetime(ts, 'unixepoch')       AS time_utc,
    peer_id                         AS group_id,
    peer_name                       AS group_name,
    sender_id                       AS sender_qq,
    sender_name                     AS sender_name,
    text                            AS content,
    important                       AS important
FROM messages
WHERE chat_type = 'group' AND muted = 0;

CREATE VIEW IF NOT EXISTS v_private_messages AS
SELECT
    ts                              AS ts,
    datetime(ts, 'unixepoch')       AS time_utc,
    peer_id                         AS peer_qq,
    sender_name                     AS sender_name,
    direction                       AS direction,
    text                            AS content,
    important                       AS important
FROM messages
WHERE chat_type = 'private' AND muted = 0;
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_msg_ts        ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_msg_peer      ON messages(peer_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_important ON messages(important, ts);
CREATE INDEX IF NOT EXISTS idx_msg_muted     ON messages(muted, ts);
CREATE INDEX IF NOT EXISTS idx_msg_sender    ON messages(peer_id, sender_id, ts);
CREATE INDEX IF NOT EXISTS idx_digests_state ON digests(state, fire_at);
"""

# 消息记录字段（顺序与 INSERT 对应）
_MSG_FIELDS = (
    "msg_id",
    "ts",
    "direction",
    "chat_type",
    "peer_id",
    "peer_name",
    "sender_id",
    "sender_name",
    "content_type",
    "text",
    "highlight",
    "important",
    "muted",
    "collected_at",
)

# 旧库升级：列名 -> 建列语句
_MIGRATIONS: dict[str, str] = {
    "important": "ALTER TABLE messages ADD COLUMN important INTEGER NOT NULL DEFAULT 0",
    "muted": "ALTER TABLE messages ADD COLUMN muted INTEGER NOT NULL DEFAULT 0",
}


class Store:
    """SQLite 存储门面。"""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.executescript(SCHEMA_INDEXES)
            self._conn.executescript(SCHEMA_VIEWS)
            self._conn.commit()

    @classmethod
    def open_readonly(cls, db_path: Path | str) -> "Store | None":
        """以**只读**方式打开已存在的数据库，供界面等只读消费者使用。

        与 :meth:`__init__` 的区别：正常情况下不建表、不迁移、不写 ``PRAGMA``，
        因此不会和后端进程抢数据库，也不会误创建空库。文件不存在时返回 ``None``。

        例外：如果检测到**库结构比代码旧**（例如新增了列但后端还没重启过），
        会先做一次幂等的结构升级再以只读方式打开。否则界面会因为缺列直接报错，
        而用户完全不知道要先启动一次后端。
        """
        path = Path(db_path)
        if not path.is_file():
            return None

        if cls._needs_upgrade(path):
            log.info("检测到数据库结构较旧，正在自动升级：%s", path)
            try:
                with cls(path) as upgrader:  # __init__ 会执行建表/迁移/索引/视图
                    pass
            except Exception as exc:  # pragma: no cover - 升级失败不应让界面崩掉
                log.warning("自动升级失败（可能后端正在占用）：%s", exc)

        obj = cls.__new__(cls)
        obj.db_path = path
        obj._lock = threading.RLock()
        uri = f"file:{path.as_posix()}?mode=ro"
        obj._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        obj._conn.row_factory = sqlite3.Row
        return obj

    @staticmethod
    def _needs_upgrade(path: Path) -> bool:
        """快速检查库结构是否落后于当前代码（只读探测，不改动）。"""
        expected = set(_MIGRATIONS) | {"muted"}
        try:
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
                return not expected.issubset(cols)
            finally:
                conn.close()
        except Exception:
            return False

    def _migrate(self) -> None:
        """为旧版本数据库补齐新增列（幂等）。"""
        cols = {
            str(r["name"])
            for r in self._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        for column, ddl in _MIGRATIONS.items():
            if column not in cols:
                self._conn.execute(ddl)
                self._conn.execute(
                    "INSERT INTO state (key, value, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (f"migrated_{column}", time.strftime("%Y-%m-%d %H:%M:%S"), int(time.time())),
                )

    # ---------------------------------------------------------------- 生命周期

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------- 消息

    def insert_messages(self, records: Iterable[dict[str, Any]]) -> int:
        """批量写入消息，``msg_id`` 冲突则忽略。返回实际新增条数。"""
        rows = []
        for r in records:
            missing = [f for f in _MSG_FIELDS if f not in r]
            if missing:
                raise ValueError(f"消息记录缺少字段：{missing}")
            rows.append(tuple(r[f] for f in _MSG_FIELDS))
        if not rows:
            return 0
        placeholders = ",".join("?" * len(_MSG_FIELDS))
        sql = f"INSERT OR IGNORE INTO messages ({','.join(_MSG_FIELDS)}) VALUES ({placeholders})"
        with self._lock:
            cur = self._conn.executemany(sql, rows)
            self._conn.commit()
            return cur.rowcount if cur.rowcount is not None else 0

    def fetch_messages(
        self,
        start_ts: int,
        end_ts: int,
        *,
        only_incoming: bool = False,
        include_muted: bool = False,
    ) -> list[dict[str, Any]]:
        """取 ``start_ts <= ts < end_ts`` 的消息，按时间升序。

        ``include_muted=False``（默认）时排除静音消息，即权重 0 的会话。
        整合与压缩都走这个默认值——**静音内容不该进入模型上下文**。
        统计页需要看全量时再显式传 True。
        """
        sql = "SELECT * FROM messages WHERE ts >= ? AND ts < ?"
        params: list[Any] = [start_ts, end_ts]
        if only_incoming:
            sql += " AND direction = 0"
        if not include_muted:
            sql += " AND muted = 0"
        sql += " ORDER BY ts ASC"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def count_messages(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def stats_by_peer(self, start_ts: int, end_ts: int) -> list[tuple[str, str, int]]:
        """返回 ``[(peer_id, peer_name, count)]``，按条数降序。"""
        sql = (
            "SELECT peer_id, COALESCE(MAX(peer_name),'') AS peer_name, COUNT(*) AS n "
            "FROM messages WHERE ts >= ? AND ts < ? "
            "GROUP BY peer_id ORDER BY n DESC"
        )
        with self._lock:
            return [
                (r["peer_id"] or "", r["peer_name"] or "", int(r["n"]))
                for r in self._conn.execute(sql, (start_ts, end_ts)).fetchall()
            ]

    # ------------------------------------------------------------------ 状态

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO state (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, int(time.time())),
            )
            self._conn.commit()

    def get_window_end(self, default_ts: int) -> int:
        raw = self.get_state("window_end")
        if raw is None:
            return default_ts
        try:
            return int(raw)
        except ValueError:
            return default_ts

    def set_window_end(self, ts: int) -> None:
        self.set_state("window_end", str(ts))

    # ------------------------------------------------------------------ 简报

    def create_digest(
        self,
        slot: str,
        window_start: int,
        window_end: int,
        fire_at: int,
    ) -> int | None:
        """为某档时间点创建简报占位。``slot`` 已存在则返回 None（防重复）。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO digests "
                "(slot, window_start, window_end, fire_at, state, created_at) "
                "VALUES (?,?,?,?, 'pending', ?)",
                (slot, window_start, window_end, fire_at, int(time.time())),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            return int(cur.lastrowid or 0)

    def finish_digest(
        self,
        digest_id: int,
        *,
        content: str,
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        msg_count: int = 0,
        run_file: str = "",
        state: str = "pending",
        error: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE digests SET content=?, model=?, prompt_tokens=?, completion_tokens=?, "
                "msg_count=?, run_file=?, state=?, error=? WHERE id=?",
                (
                    content,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    msg_count,
                    run_file,
                    state,
                    error or None,
                    digest_id,
                ),
            )
            self._conn.commit()

    def get_digest(self, digest_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM digests WHERE id=?", (digest_id,)).fetchone()
        return dict(row) if row else None

    def digest_by_slot(self, slot: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM digests WHERE slot=?", (slot,)).fetchone()
        return dict(row) if row else None

    def due_digests(self, now_ts: int) -> list[dict[str, Any]]:
        """已到发送时刻且尚未发送的简报。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM digests WHERE state='pending' AND fire_at <= ? "
                "ORDER BY fire_at ASC",
                (now_ts,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_sent(self, digest_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE digests SET state='sent', sent_at=? WHERE id=?",
                (int(time.time()), digest_id),
            )
            self._conn.commit()

    def set_digest_state(self, digest_id: int, state: str) -> None:
        """直接设置简报状态（用于手动生成的简报挂起，避免被调度器自动发送）。"""
        with self._lock:
            self._conn.execute("UPDATE digests SET state=? WHERE id=?", (state, digest_id))
            self._conn.commit()

    def mark_failed(self, digest_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE digests SET state='failed', error=? WHERE id=?",
                (error[:500], digest_id),
            )
            self._conn.commit()

    def recent_digests(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def has_digest_for_slot(self, slot: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM digests WHERE slot=?", (slot,)).fetchone()
        return row is not None

    # ------------------------------------------------------------------ 会话

    def list_peers(self) -> list[tuple[str, str, str, int]]:
        """列出库里出现过的所有会话：``[(peer_id, peer_name, chat_type, 消息数)]``。"""
        sql = (
            "SELECT peer_id, COALESCE(MAX(peer_name),'') AS peer_name, "
            "       MAX(chat_type) AS chat_type, COUNT(*) AS n "
            "FROM messages WHERE peer_id IS NOT NULL AND peer_id <> '' "
            "GROUP BY peer_id ORDER BY n DESC"
        )
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [
            (str(r["peer_id"]), str(r["peer_name"] or ""), str(r["chat_type"] or ""), int(r["n"]))
            for r in rows
        ]

    def sample_messages(self, peer_id: str, limit: int = 12) -> list[str]:
        """取某会话最近的若干条文本，供标签分类参考。"""
        sql = (
            "SELECT text FROM messages WHERE peer_id=? AND text IS NOT NULL AND text <> '' "
            "ORDER BY ts DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, (peer_id, max(1, limit))).fetchall()
        return [str(r["text"]) for r in rows]

    # -------------------------------------------------------------- 会话标签

    def upsert_peer_tag(
        self,
        *,
        peer_id: str,
        peer_name: str,
        chat_type: str,
        label: str,
        weight: int,
        source: str = "",
        detail: str = "",
        msg_count: int = 0,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO peer_tags "
                "(peer_id, peer_name, chat_type, label, weight, source, detail, msg_count, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(peer_id) DO UPDATE SET "
                "peer_name=excluded.peer_name, chat_type=excluded.chat_type, label=excluded.label, "
                "weight=excluded.weight, source=excluded.source, detail=excluded.detail, "
                "msg_count=excluded.msg_count, updated_at=excluded.updated_at",
                (
                    peer_id,
                    peer_name,
                    chat_type,
                    label,
                    int(weight),
                    source,
                    detail[:200],
                    int(msg_count),
                    int(time.time()),
                ),
            )
            self._conn.commit()

    def get_peer_tags(self) -> dict[str, dict[str, Any]]:
        """返回 ``peer_id -> 标签行``。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM peer_tags").fetchall()
        return {str(r["peer_id"]): dict(r) for r in rows}

    def get_peer_tag(self, peer_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM peer_tags WHERE peer_id=?", (peer_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_peer_label(self, peer_id: str, label: str, weight: int) -> bool:
        """手工改某个会话的标签（用于界面覆盖模型判断）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE peer_tags SET label=?, weight=?, source='manual', updated_at=? "
                "WHERE peer_id=?",
                (label, int(weight), int(time.time()), peer_id),
            )
            self._conn.commit()
            return (cur.rowcount or 0) > 0

    def delete_peer_tag(self, peer_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM peer_tags WHERE peer_id=?", (peer_id,))
            self._conn.commit()

    # -------------------------------------------------------------- 静音与清理

    def apply_mute_flags(self, weights: dict[str, int], *, mute_weight: int = 0) -> int:
        """按标签权重给消息打/取消 ``muted`` 标记，返回被静音的消息条数。

        **这是"权重 0 的消息干脆不入库"的可逆替代方案。**

        为什么不在入库时就丢掉：标签是模型判断的、会错判；一旦丢掉，
        事后把标签改回来也找不回历史消息。这里照常入库、只打标记，
        查询与整合都会跳过它，磁盘占用再用 ``purge_muted`` 按保留期清理。
        """
        muted_peers = [pid for pid, w in weights.items() if w <= mute_weight]
        with self._lock:
            self._conn.execute("UPDATE messages SET muted=0 WHERE muted<>0")
            if muted_peers:
                placeholders = ",".join("?" * len(muted_peers))
                self._conn.execute(
                    f"UPDATE messages SET muted=1 WHERE peer_id IN ({placeholders})",
                    muted_peers,
                )
            self._conn.commit()
            row = self._conn.execute("SELECT COUNT(*) FROM messages WHERE muted=1").fetchone()
        return int(row[0]) if row else 0

    def muted_message_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM messages WHERE muted=1").fetchone()
        return int(row[0]) if row else 0

    def purge_range(self, start_ts: int, end_ts: int) -> int:
        """删除某时间窗口内的消息（定时发送成功后调用）。

        **只按时间窗口删，绝不"清空全部"**——窗口外（比如刚到的）消息必须留下。
        手动"立即整合并发送"永远不会调用它。
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM messages WHERE ts >= ? AND ts < ?", (start_ts, end_ts)
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def purge_muted(self, keep_days: int, *, now_ts: int | None = None) -> int:
        """清理超过保留期的静音消息（权重 0 那些）。``keep_days<=0`` 表示不清理。"""
        if keep_days <= 0:
            return 0
        cutoff = int(now_ts if now_ts is not None else time.time()) - keep_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM messages WHERE muted=1 AND ts < ?", (cutoff,))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def database_size_bytes(self) -> int:
        try:
            return self.db_path.stat().st_size
        except OSError:
            return 0

    # ------------------------------------------------------------ 预压缩摘要

    def save_compressed_block(
        self,
        *,
        peer_id: str,
        peer_name: str,
        window_start: int,
        window_end: int,
        summary: str,
        msg_count: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO compressed_blocks "
                "(peer_id, peer_name, window_start, window_end, summary, msg_count, created_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(peer_id, window_start, window_end) DO UPDATE SET "
                "summary=excluded.summary, msg_count=excluded.msg_count, "
                "peer_name=excluded.peer_name, created_at=excluded.created_at",
                (
                    peer_id,
                    peer_name,
                    int(window_start),
                    int(window_end),
                    summary,
                    int(msg_count),
                    int(time.time()),
                ),
            )
            self._conn.commit()

    def get_compressed_summaries(self, start_ts: int, end_ts: int) -> dict[str, str]:
        """取**能覆盖整个查询窗口**的预压缩摘要，返回 ``peer_id -> 摘要``。

        语义是"包含"（``block_start <= start AND block_end >= end``），不是"落在窗口内"。
        用只覆盖一部分窗口的摘要会**替掉**块外的消息，导致内容凭空缺一截——
        这种情况下宁可用原始消息。早期实现把条件写反了（找的是落在窗口内的块），
        测试才把这个语义错误暴露出来。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT peer_id, summary FROM compressed_blocks "
                "WHERE window_start <= ? AND window_end >= ? "
                "ORDER BY window_start",
                (int(start_ts), int(end_ts)),
            ).fetchall()
        out: dict[str, str] = {}
        for r in rows:
            pid = str(r["peer_id"])
            # 同一个会话有多块时按时间拼接
            out[pid] = (out[pid] + "\n" + str(r["summary"])) if pid in out else str(r["summary"])
        return out

    def purge_compressed(self, keep_days: int, *, now_ts: int | None = None) -> int:
        """清理过期的预压缩摘要。"""
        if keep_days <= 0:
            return 0
        cutoff = int(now_ts if now_ts is not None else time.time()) - keep_days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM compressed_blocks WHERE window_end < ?", (cutoff,)
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ 统计

    def hourly_distribution(self, start_ts: int, end_ts: int, *, include_muted: bool = False):
        """按小时统计消息量，返回 ``[(小时 0-23, 条数)]``（按 UTC+8 换算）。

        用 SQLite 的 strftime 直接分组，避免把几万行拉到 Python 再统计。
        """
        sql = (
            "SELECT CAST(strftime('%H', ts, 'unixepoch', '+8 hours') AS INTEGER) AS h, "
            "       COUNT(*) AS n "
            "FROM messages WHERE ts >= ? AND ts < ?"
        )
        if not include_muted:
            sql += " AND muted = 0"
        sql += " GROUP BY h ORDER BY h"
        with self._lock:
            rows = self._conn.execute(sql, (start_ts, end_ts)).fetchall()
        return [(int(r["h"]), int(r["n"])) for r in rows]

    def daily_distribution(self, start_ts: int, end_ts: int, *, include_muted: bool = False):
        """按天统计消息量，返回 ``[(日期, 条数)]``。"""
        sql = (
            "SELECT date(ts, 'unixepoch', '+8 hours') AS d, COUNT(*) AS n "
            "FROM messages WHERE ts >= ? AND ts < ?"
        )
        if not include_muted:
            sql += " AND muted = 0"
        sql += " GROUP BY d ORDER BY d"
        with self._lock:
            rows = self._conn.execute(sql, (start_ts, end_ts)).fetchall()
        return [(str(r["d"]), int(r["n"])) for r in rows]

    def peer_totals(self, start_ts: int, end_ts: int, *, include_muted: bool = False):
        """各会话的消息总数，返回 ``[(peer_id, peer_name, chat_type, 条数)]``。"""
        sql = (
            "SELECT peer_id, COALESCE(MAX(peer_name),'') AS peer_name, "
            "       MAX(chat_type) AS chat_type, COUNT(*) AS n "
            "FROM messages WHERE ts >= ? AND ts < ?"
        )
        if not include_muted:
            sql += " AND muted = 0"
        sql += " GROUP BY peer_id ORDER BY n DESC"
        with self._lock:
            rows = self._conn.execute(sql, (start_ts, end_ts)).fetchall()
        return [
            (str(r["peer_id"]), str(r["peer_name"] or ""), str(r["chat_type"] or ""), int(r["n"]))
            for r in rows
        ]
