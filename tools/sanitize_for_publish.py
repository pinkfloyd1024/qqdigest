"""发布前脱敏：把源码/文档里出现的真实账号换成占位符。

**设计要点：本脚本自身不含任何真实账号。**

早期版本把"真实值 -> 占位符"的映射硬编码在脚本里，结果这个脚本本身
就成了泄漏源（它是要被提交的）。现在改为**运行时从 ``config.toml`` 和数据库里
读取真实值**——这两个来源都在 .gitignore 里，不会进仓库。

用法::

    python tools/sanitize_for_publish.py            # 脱敏
    python tools/sanitize_for_publish.py --check    # 只检查，不改动
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.toml"
DB = ROOT / "data" / "store.db"

SCAN_SUFFIXES = {".py", ".md", ".bat", ".ps1", ".txt", ".toml", ".cfg", ".ini"}
SKIP_DIRS = {"__pycache__", "data", "runs", "logs", ".git"}
#: 这些文件保持原样：本地配置不提交，模板本身就是脱敏产物
SKIP_NAMES = {"config.toml", "config.toml.bak", "config.example.toml"}

#: 长度 >= 5 的纯数字才当成账号；太短的容易误伤（端口、年份等）
_MIN_ID_LEN = 5


def collect_real_ids() -> dict[str, str]:
    """从 config.toml 与数据库收集真实账号，生成占位符。

    返回 ``{真实值: 占位符}``。占位符形如 ``10000001``，
    便于阅读又不指向任何真实账号。
    """
    ids: set[str] = set()

    if CONFIG.is_file():
        try:
            raw = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        for section, keys in (("onebot", ("self_id",)), ("target", ("send_to",))):
            sec = raw.get(section) or {}
            for key in keys:
                value = str(sec.get(key) or "").strip()
                if value.isdigit() and len(value) >= _MIN_ID_LEN:
                    ids.add(value)
        # 白名单/黑名单/重要联系人里也全是账号
        filt = raw.get("filter") or {}
        for key in ("group_whitelist", "peer_blacklist"):
            for value in filt.get(key) or []:
                text = str(value).strip()
                if text.isdigit() and len(text) >= _MIN_ID_LEN:
                    ids.add(text)
        important = raw.get("important") or {}
        for value in important.get("peers") or []:
            text = str(value).strip()
            if text.isdigit() and len(text) >= _MIN_ID_LEN:
                ids.add(text)
        manual = (raw.get("tags") or {}).get("manual") or {}
        for key in manual:
            text = str(key).strip()
            if text.isdigit() and len(text) >= _MIN_ID_LEN:
                ids.add(text)

    if DB.is_file():
        try:
            conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
            try:
                for sql in (
                    "SELECT DISTINCT peer_id FROM messages WHERE peer_id <> ''",
                    "SELECT DISTINCT sender_id FROM messages WHERE sender_id <> ''",
                ):
                    for row in conn.execute(sql):
                        text = str(row[0]).strip()
                        if text.isdigit() and len(text) >= _MIN_ID_LEN:
                            ids.add(text)
            finally:
                conn.close()
        except Exception:
            pass

    # 长的先替换，避免短号是长号子串时替换错
    out: dict[str, str] = {}
    for i, real in enumerate(sorted(ids, key=len, reverse=True), start=1):
        out[real] = f"1000{i:04d}"
    return out


#: 读起来像"通用词"的名字不参与替换，避免误伤代码。
#:
#: 除了编程常见词，还包含 **OneBot / NapCat 的协议字段值**——例如
#: `"sub_type": "friend"` 里的 friend 是协议规定值，不是谁泄露的群名。
#: 早期自检把这类值当成"真实会话名"报了警，属于误报。
_GENERIC_NAMES = {
    # 编程常见词
    "test", "dev", "admin", "user", "group", "chat", "info", "news", "work",
    "home", "main", "data", "code", "note", "team", "club", "abc", "wiki",
    "foo", "bar", "baz", "none", "true", "false", "text", "item", "name",
    # OneBot / NapCat 协议字段值
    "friend", "private", "normal", "notice", "request", "message", "meta_event",
    "sender", "temp", "discuss", "anonymous", "heartbeat", "lifecycle",
    "group_upload", "friend_add", "poke", "reply", "image", "face", "record",
    "video", "json", "xml", "markdown", "forward", "node", "online", "offline",
    # QQ 官方系统账号名
    "系统消息", "群系统消息", "QQ 系统消息",
}

#: 纯标点/符号的名字（含 Python 的 `...` 省略号字面量）绝不能替换
_PUNCT_ONLY = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fff]+$")
#: 含中日韩字符
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")


def _is_safe_to_replace(name: str) -> bool:
    """判断一个名字是否适合做全文替换。

    **这一步是必须的**：曾经差点用数据库里的会话名做无条件全文替换，
    而数据库里存在 ``...`` ``abc`` 这类名字——``...`` 是 Python 的 Ellipsis
    字面量，替换它会直接改坏源码。纯 ASCII 的名字太容易撞车，一律跳过。
    """
    if len(name) < 3:
        return False
    if _PUNCT_ONLY.match(name):
        return False
    if name.lower() in _GENERIC_NAMES:
        return False
    if _HAS_CJK.search(name):
        # 中文名足够有辨识度，长度 >=3 即可
        return True
    # 纯 ASCII 的名字要求更长，且必须不是常见代码词
    return len(name) >= 5 and name.isascii()


def collect_real_names() -> dict[str, str]:
    """从数据库收集真实**会话名**（群名/昵称），生成中性占位符。

    这些名字会出现在 README 的举例、提示词注释、测试用例里——公开仓库里
    等于公开了"你加了哪些群、在哪所学校"。同样从数据库读，脚本自身不含真名。
    """
    names: set[str] = set()
    if not DB.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
        try:
            for sql in (
                "SELECT DISTINCT peer_name FROM messages WHERE peer_name <> ''",
                "SELECT DISTINCT sender_name FROM messages WHERE sender_name <> ''",
            ):
                for row in conn.execute(sql):
                    text = str(row[0]).strip()
                    if _is_safe_to_replace(text):
                        names.add(text)
        finally:
            conn.close()
    except Exception:
        return {}

    # 长的先替换
    out: dict[str, str] = {}
    for i, real in enumerate(sorted(names, key=len, reverse=True), start=1):
        out[real] = f"示例会话{i:02d}"
    return out


def iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_NAMES:
            continue
        if path.suffix.lower() in SCAN_SUFFIXES:
            yield path


def apply(mapping: dict[str, str], *, check_only: bool) -> tuple[list[tuple[str, int]], list[str]]:
    changed: list[tuple[str, int]] = []
    leftovers: list[str] = []
    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits = sum(text.count(real) for real in mapping)
        if not hits:
            continue
        rel = str(path.relative_to(ROOT))
        if check_only:
            leftovers.append(f"{rel} 命中 {hits} 处真实账号")
            continue
        for real, fake in mapping.items():
            text = text.replace(real, fake)
        path.write_text(text, encoding="utf-8")
        changed.append((rel, hits))
    return changed, leftovers


def make_example_config(mapping: dict[str, str]) -> Path | None:
    """从真实配置生成脱敏模板。"""
    if not CONFIG.is_file():
        return None
    text = CONFIG.read_text(encoding="utf-8")
    for real, fake in mapping.items():
        text = text.replace(real, fake)
    text = re.sub(r'self_id\s*=\s*"[^"]*"', 'self_id = "你的大号QQ"', text)
    text = re.sub(r'send_to\s*=\s*"[^"]*"', 'send_to = "接收简报的小号QQ"', text)
    for key in ("group_whitelist", "peer_blacklist", "peers"):
        text = re.sub(rf"^{key}\s*=\s*\[[^\]]*\]", f"{key} = []", text, flags=re.M)
    header = (
        "# ==========================================================================\n"
        "# qqdigest 配置模板\n"
        "#\n"
        "# 复制成 config.toml 后按自己的情况修改。\n"
        "# config.toml / data/ / runs/ / logs/ 都在 .gitignore 里，不会被提交。\n"
        "# ==========================================================================\n\n"
    )
    dst = ROOT / "config.example.toml"
    dst.write_text(header + text, encoding="utf-8")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description="发布前脱敏")
    ap.add_argument("--check", action="store_true", help="只检查，不修改")
    args = ap.parse_args()

    mapping = collect_real_ids()
    if not mapping:
        print("没有从 config.toml / 数据库里读到任何账号，无需脱敏。")
        return 0
    print(f"从本地配置与数据库收集到 {len(mapping)} 个真实账号（值不显示）")

    changed, leftovers = apply(mapping, check_only=args.check)
    if args.check:
        if leftovers:
            print("\n!! 以下文件仍含真实账号：")
            for x in leftovers:
                print("   ", x)
            return 1
        print("\n校验通过：待提交内容中没有真实账号。")
        return 0

    print()
    for name, hits in changed:
        print(f"  已脱敏 {name}（{hits} 处）")
    if not changed:
        print("  无需改动")

    dst = make_example_config(mapping)
    if dst:
        print(f"\n已生成模板：{dst.name}")

    _, still = apply(mapping, check_only=True)
    if still:
        print("\n!! 仍有残留：")
        for x in still:
            print("   ", x)
        return 1
    print("\n自查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
