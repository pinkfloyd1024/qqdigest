"""保留注释的 TOML 写回器。

GUI 需要修改 ``config.toml``，但 ``tomllib`` 只能读不能写，而全量重写会丢掉
文件里所有注释和排版。本模块采用「定位并替换单个键值」的方式：

- 只替换目标键所在的那一行（或那个多行值区间），其余字节原样保留；
- 找不到键时，在该配置段末尾追加；找不到配置段时，在文件末尾新建；
- **写盘前用 ``tomllib`` 重新解析一遍**，解析不通过就拒绝写入，避免写坏配置。

支持的取值：``bool`` / ``int`` / ``float`` / ``str``（含多行）/ 以上类型组成的列表。
"""

from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
_KEY_RE_TMPL = r'^\s*(?:"{key}"|\'{key}\'|{key})\s*='
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def format_key(key: str) -> str:
    """把键格式化为合法的 TOML 键。

    TOML 的裸键只允许 ``A-Za-z0-9_-``；中文等其它字符**必须加双引号**，
    否则整个文件会解析失败（`课程 = 10` 是非法语句，`"课程" = 10` 才合法）。
    """
    if _BARE_KEY_RE.match(key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class TomlEditError(RuntimeError):
    """TOML 写回失败。"""


# --------------------------------------------------------------------------
# 取值格式化
# --------------------------------------------------------------------------


def _format_multiline(text: str) -> str:
    body = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    # 以单引号结尾时，紧邻的 ''' 会产生歧义，退化为多一行
    if "'''" not in body and not body.endswith("'"):
        return "'''\n" + body + "'''"
    if "'''" not in body:
        return "'''\n" + body + "\n'''"
    escaped = body.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return '"""\n' + escaped + '"""'


def format_string(text: str) -> str:
    """把字符串格式化为合法的 TOML 字面量。"""
    if "\n" in text:
        return _format_multiline(text)
    if "\\" in text or '"' in text:
        if "'" not in text and "\t" not in text:
            return "'" + text + "'"
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if "\t" in text:
        return '"' + text.replace("\t", "\\t") + '"'
    return '"' + text + '"'


def format_value(value: Any) -> str:
    """把 Python 值格式化为 TOML 取值文本。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == int(value):
            return f"{value:.1f}"
        return repr(value)
    if isinstance(value, str):
        return format_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    raise TomlEditError(f"不支持写入的配置类型：{type(value).__name__}")


# --------------------------------------------------------------------------
# 多行取值边界扫描
# --------------------------------------------------------------------------


def _value_end(lines: list[str], start: int, col: int) -> tuple[int, int]:
    """从 ``(start 行, col 列)`` 开始扫描一个 TOML 取值，返回结束位置 ``(行, 列)``。

    处理三种跨行情形：数组 / 内联表、多行基本字符串、多行字面量字符串。
    """
    depth = 0
    i = start
    line = lines[i]
    basic = literal = False
    triple_basic = triple_literal = False

    while True:
        line = lines[i]
        while col < len(line):
            ch = line[col]
            nxt3 = line[col : col + 3]

            if triple_basic:
                if nxt3 == '"""':
                    triple_basic = False
                    col += 3
                    continue
                col += 1
                continue
            if triple_literal:
                if nxt3 == "'''":
                    triple_literal = False
                    col += 3
                    continue
                col += 1
                continue
            if basic:
                if ch == "\\":
                    col += 2
                    continue
                if ch == '"':
                    basic = False
                col += 1
                continue
            if literal:
                if ch == "'":
                    literal = False
                col += 1
                continue

            if nxt3 == '"""':
                triple_basic = True
                col += 3
                continue
            if nxt3 == "'''":
                triple_literal = True
                col += 3
                continue
            if ch == '"':
                basic = True
                col += 1
                continue
            if ch == "'":
                literal = True
                col += 1
                continue
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "#" and depth == 0:
                break  # 行内注释，取值到此结束
            col += 1

        if depth <= 0 and not (basic or literal or triple_basic or triple_literal):
            return i, col
        i += 1
        if i >= len(lines):
            return len(lines) - 1, len(lines[-1])
        col = 0


# --------------------------------------------------------------------------
# 编辑器
# --------------------------------------------------------------------------


class TomlEditor:
    """对单个 TOML 文件做「保留注释」的键值修改。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.text = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
        self._pending: dict[tuple[str, str], str] = {}
        # 校验原文件本身可解析
        if self.text.strip():
            try:
                tomllib.loads(self.text)
            except tomllib.TOMLDecodeError as exc:
                raise TomlEditError(f"原配置文件无法解析，已拒绝编辑：{exc}") from exc

    # ------------------------------------------------------------------ 收集
    def set(self, section: str, key: str, value: Any) -> None:
        """登记一项修改（此时不落盘）。"""
        self._pending[(section, key)] = format_value(value)

    def set_all(self, values: dict[tuple[str, str], Any]) -> None:
        for (section, key), value in values.items():
            self.set(section, key, value)

    @property
    def changed_keys(self) -> list[tuple[str, str]]:
        return sorted(self._pending)

    # ------------------------------------------------------------------ 渲染
    def _find_section(self, lines: list[str], section: str) -> tuple[int, int] | None:
        """返回 ``(段落起止行号, 下一段起始行号)``。"""
        start = None
        for idx, line in enumerate(lines):
            m = _SECTION_RE.match(line)
            if not m:
                continue
            if m.group(1).strip().strip('"').strip("'") == section:
                start = idx
                continue
            if start is not None:
                return start, idx
        if start is not None:
            return start, len(lines)
        return None

    def render(self) -> str:
        """应用全部待写入项，返回新的文件文本（不落盘）。"""
        text = self.text
        for (section, key), formatted in self._pending.items():
            text = self._apply(text, section, key, formatted)
        # 安全网：结果必须仍能被解析
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise TomlEditError(
                f"修改后配置无法解析，已放弃写入：{exc}\n"
                f"（原有配置未被改动，请检查取值是否合法）"
            ) from exc
        return text

    def _apply(self, text: str, section: str, key: str, formatted: str) -> str:
        lines = text.split("\n")
        span = self._find_section(lines, section)
        key_re = re.compile(_KEY_RE_TMPL.format(key=re.escape(key)))
        key_text = format_key(key)

        search_from = span[0] + 1 if span else 0
        search_to = span[1] if span else len(lines)

        for idx in range(search_from, search_to):
            m = key_re.match(lines[idx])
            if not m:
                continue
            eq = lines[idx].index("=", m.start())
            end_line, end_col = _value_end(lines, idx, eq + 1)
            prefix = lines[idx][: m.start()]
            suffix = lines[end_line][end_col:]
            # 保留行尾注释，但去除多余空白
            suffix = suffix.rstrip()
            suffix = ("  " + suffix) if suffix else ""
            new_line = f"{prefix}{key_text} = {formatted}{suffix}"
            lines[idx : end_line + 1] = [new_line]
            return "\n".join(lines)

        # 未找到该键 -> 插入
        new_line = f"{key_text} = {formatted}"
        if span is None:
            tail = lines[:]
            while tail and not tail[-1].strip():
                tail.pop()
            if tail:
                tail.append("")
            tail.append(f"[{section}]")
            tail.append(new_line)
            tail.append("")
            return "\n".join(tail)

        insert_at = span[1]
        while insert_at > span[0] + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, new_line)
        return "\n".join(lines)

    # ------------------------------------------------------------------ 落盘
    def save(self, *, backup: bool = True) -> str:
        """应用修改并写盘。返回备份文件路径（未备份则为空串）。"""
        if not self._pending:
            return ""
        new_text = self.render()
        if new_text == self.text:
            return ""

        backup_path = ""
        if backup and self.path.is_file():
            backup_path = str(self.path) + ".bak"
            try:
                shutil.copy2(self.path, backup_path)
            except OSError:
                backup_path = ""

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        tmp.replace(self.path)
        self.text = new_text
        self._pending.clear()
        return backup_path


def update_config(path: Path | str, values: dict[tuple[str, str], Any]) -> str:
    """便捷函数：一次性写入多项，返回备份路径。"""
    editor = TomlEditor(path)
    editor.set_all(values)
    return editor.save()
