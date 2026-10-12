"""发布前自检：确认待提交内容里没有真实信息。

**本脚本不硬编码任何真实账号或群名**——全部从 ``config.toml`` 和数据库读取，
而这两个来源都在 .gitignore 里。

这不是洁癖。本项目在"查泄漏的工具自己泄漏"这件事上连续踩了四次坑：
脱敏脚本、群名迁移脚本、本脚本的第一版，都把真实值写死在了自己的映射表里。
只要工具需要知道"什么算敏感"，就不能把敏感值写进工具本身。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.sanitize_for_publish import (  # noqa: E402
    collect_real_ids,
    collect_real_names,
)

#: 与本机环境相关、但不来自数据库的模式（正则）
GENERIC_PATTERNS = [
    (r"[A-Za-z]:\\\\?NapCat", "本机 NapCat 安装路径"),
    (r"C:\\\\?Users\\\\?[A-Za-z0-9_]+", "本机用户目录"),
    (r"\b9\.9\.\d{2}-\d{4,5}\b", "具体 QQ 版本号"),
]


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    return [x for x in out.stdout.splitlines() if x.strip()]


def main() -> int:
    files = tracked_files()
    ids = collect_real_ids()
    names = collect_real_names()
    probes: list[tuple[str, str]] = []
    probes += [(v, "真实账号") for v in ids]
    probes += [(v, "真实会话名/昵称") for v in names]

    print(f"扫描 {len(files)} 个已跟踪文件")
    print(f"探针：{len(ids)} 个账号 + {len(names)} 个会话名 + {len(GENERIC_PATTERNS)} 条通用模式")
    print()

    contents: dict[str, str] = {}
    for rel in files:
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            contents[rel] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

    bad = 0
    for value, kind in probes:
        hits = [rel for rel, text in contents.items() if value in text]
        if hits:
            bad += 1
            print(f"  !! [{kind}] {value!r} -> {', '.join(hits[:3])}")

    for pattern, label in GENERIC_PATTERNS:
        rx = re.compile(pattern)
        for rel, text in contents.items():
            if rx.search(text):
                bad += 1
                print(f"  !! [{label}] 命中 {rel}")

    print()
    if bad:
        print(f"{bad} 处残留，先跑 tools/sanitize_for_publish.py 再提交")
        return 1
    print("✓ 待提交内容干净：无真实账号、群名、本机路径")
    return 0


if __name__ == "__main__":
    sys.exit(main())
