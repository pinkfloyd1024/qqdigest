"""qqdigest 命令行入口。

子命令：
    run          常驻运行：启动事件上报服务 + 调度循环（默认）
    once         执行一轮检查后退出（适合交给 Windows 任务计划）
    digest-now   立即用当前窗口生成一份简报（调试/验收用）
    selftest     端到端自检：合成假消息 -> 真实调用本地模型 -> 输出简报
    check        体检：配置、本地模型、NapCat 连通性、上报端口
    stats        查看数据库统计

用法示例：
    python run.py check
    python run.py run
    python run.py digest-now --hold
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# 允许直接以脚本方式运行（python run.py）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from qqdigest import __version__  # noqa: E402
from qqdigest.config import Config, ensure_dirs, load_config  # noqa: E402
from qqdigest.digest import run_digest  # noqa: E402
from qqdigest.llm import LlmClient, LlmError  # noqa: E402
from qqdigest.logging_setup import setup_logging  # noqa: E402
from qqdigest.onebot import OneBotClient, OneBotError, ReportServer, parse_event  # noqa: E402
from qqdigest import tagging  # noqa: E402
from qqdigest.scheduler import Scheduler  # noqa: E402
from qqdigest.sender import send_digest  # noqa: E402
from qqdigest.store import Store  # noqa: E402
from qqdigest.util import prune_runs  # noqa: E402

log = logging.getLogger("qqdigest")


# --------------------------------------------------------------------------
# 事件处理
# --------------------------------------------------------------------------


def make_event_handler(store: Store, cfg: Config):
    """构造上报事件处理器：解析 -> 过滤 -> 入库 ->（命中重要规则时）即时提醒。"""

    def handle(event: dict[str, Any]) -> None:
        record, reason = parse_event(event, cfg)
        if record is None:
            log.debug("事件已忽略：%s", reason)
            return
        inserted = store.insert_messages([record])
        if not inserted:
            log.debug("重复消息，已跳过：%s", record["msg_id"])
            return

        who = "我" if record["direction"] == 1 else record["sender_name"] or record["peer_id"]
        log.info(
            "收录消息 [%s] %s: %s",
            record["chat_type"],
            who,
            str(record["text"])[:60],
        )

        if int(record.get("important") or 0) and cfg.important.notify_immediately:
            _notify_important(cfg, record)

    return handle


def _notify_important(cfg: Config, record: dict[str, Any]) -> None:
    """重要消息即时提醒（桌面弹窗 + 剪贴板），失败不影响主流程。"""
    from qqdigest.notify import notify

    mark = cfg.important.mark_prefix or "⭐"
    who = record["sender_name"] or record["peer_id"]
    title = f"{mark} 重要消息 · {who}"
    body = str(record["text"])[:300]
    try:
        result = notify(
            title,
            body,
            use_toast=cfg.important.notify_toast,
            use_clipboard=cfg.important.notify_clipboard,
            log_file=cfg.storage.data_dir / "notices.log",
        )
        log.info("重要消息提醒已发出：%s", result.describe())
    except Exception:
        log.exception("发送重要消息提醒时出错")


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------


def cmd_run(cfg: Config) -> int:
    store = Store(cfg.db_path)
    llm = LlmClient(cfg.llm)
    server = ReportServer(cfg, make_event_handler(store, cfg))
    prune_runs(cfg.storage.runs_dir, cfg.storage.runs_keep_days)
    try:
        server.start_background()
    except OSError as exc:
        log.error("无法监听 %s:%s —— %s", cfg.onebot.report_host, cfg.onebot.report_port, exc)
        return 2
    scheduler = Scheduler(store, cfg, llm)
    log.info("qqdigest %s 已启动 | 干跑=%s", __version__, cfg.general.dry_run)
    log.info("请在 NapCat 中把「HTTP 上报」指向：%s", server.address)
    try:
        scheduler.run_forever()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，正在退出")
    finally:
        server.stop()
        store.close()
    return 0


def cmd_once(cfg: Config) -> int:
    store = Store(cfg.db_path)
    try:
        scheduler = Scheduler(store, cfg, LlmClient(cfg.llm))
        stats = scheduler.tick()
        log.info("一次性检查完成：%s", stats)
        return 0
    finally:
        store.close()


def cmd_digest_now(cfg: Config, hold: bool, send: bool = False, force_send: bool = False) -> int:
    """立即用当前窗口生成简报；``--send`` 时顺便发到 QQ。"""
    store = Store(cfg.db_path)
    try:
        now = datetime.now(cfg.tz)
        now_ts = int(now.timestamp())
        slot = f"manual-{now.strftime('%Y%m%d-%H%M%S')}"
        window_start = store.get_window_end(now_ts - cfg.general.first_run_lookback_hours * 3600)
        digest_id = run_digest(
            store,
            cfg,
            LlmClient(cfg.llm),
            slot=slot,
            window_start=min(window_start, now_ts),
            window_end=now_ts,
            fire_at=now_ts,
        )
        if digest_id is None:
            log.warning("简报未生成")
            return 1

        row = store.get_digest(digest_id) or {}
        content = str(row.get("content") or "")

        if send and cfg.general.dry_run and not force_send:
            print()
            print("！当前是干跑模式，默认不会真的发送。")
            print("  如果确实要发这一次，请加 --force-send；")
            print("  或者到界面「常规」页关闭干跑模式。")
            store.set_digest_state(digest_id, "held")
            print()
            print("=" * 72)
            print(f"简报（slot={slot}，{row.get('msg_count', 0)} 条消息，已挂起未发送）")
            print("=" * 72)
            print(content)
            print("=" * 72)
            return 0

        if send:
            ok, detail = send_digest(cfg, row, force=force_send)
            if ok:
                store.mark_sent(digest_id)
                print(f"\n已发送：{detail}")
            else:
                store.mark_failed(digest_id, detail)
                print(f"\n发送失败：{detail}")
                return 1
        elif hold:
            store.set_digest_state(digest_id, "held")
            log.info("简报已标记为 held，调度器不会自动发送它")

        print()
        print("=" * 72)
        print(f"简报（slot={slot}，{row.get('msg_count', 0)} 条消息）")
        print("=" * 72)
        print(content)
        print("=" * 72)
        return 0
    finally:
        store.close()


def cmd_selftest(cfg: Config) -> int:
    """合成一批假消息，走完整链路（入库 -> 整合 -> 落档），用于装机自检。"""
    from tools.selftest_data import SYNTHETIC_EVENTS  # 延迟导入，避免常驻依赖

    db = cfg.storage.data_dir / "selftest.db"
    if db.exists():
        db.unlink()
    store = Store(db)
    try:
        handler = make_event_handler(store, cfg)
        accepted = 0
        for ev in SYNTHETIC_EVENTS:
            record, reason = parse_event(ev, cfg)
            if record is None:
                print(f"  过滤掉：{reason}")
                continue
            store.insert_messages([record])
            accepted += 1
        print(f"合成事件 {len(SYNTHETIC_EVENTS)} 条，通过过滤 {accepted} 条，入库 {store.count_messages()} 条")

        now = datetime.now(cfg.tz)
        now_ts = int(now.timestamp())
        window_start = now_ts - 6 * 3600
        slot = "selftest-" + now.strftime("%Y%m%d-%H%M%S")
        print("开始调用本地模型整合……")
        started = time.time()
        digest_id = run_digest(
            store,
            cfg,
            LlmClient(cfg.llm),
            slot=slot,
            window_start=window_start,
            window_end=now_ts,
            fire_at=now_ts,
        )
        elapsed = time.time() - started
        row = store.get_digest(digest_id) if digest_id else None
        if not row:
            print("整合失败：未生成简报")
            return 1
        print()
        print("=" * 72)
        print(f"整合完成，耗时 {elapsed:.1f}s")
        print(f"prompt={row.get('prompt_tokens')} completion={row.get('completion_tokens')}")
        print("=" * 72)
        print(row.get("content"))
        print("=" * 72)
        print(f"留档文件：{row.get('run_file')}")
        print("自检通过。" if row.get("content") else "自检失败：正文为空")
        return 0 if row.get("content") else 1
    finally:
        store.close()
        # 保留自检数据库便于排查；如需清理可手动删除


def cmd_watch(cfg: Config) -> int:
    """实时监听 NapCat 上报：把收到的事件逐条打印出来，直到 Ctrl+C。

    这是验证「消息上报」方向是否打通的最直接方式：
    跑起来之后，用手机给自己发一条 QQ 消息，应该立刻在下面看到它。
    """
    store = Store(cfg.db_path)
    base_handler = make_event_handler(store, cfg)
    stats = {"total": 0, "kept": 0}

    def verbose_handler(event: dict[str, Any]) -> None:
        stats["total"] += 1
        stamp = datetime.now(cfg.tz).strftime("%H:%M:%S")
        record, reason = parse_event(event, cfg)
        if record is None:
            print(f"[{stamp}] 已忽略  {str(event.get('post_type', '?')):<12} {reason}")
        else:
            base_handler(event)
            stats["kept"] += 1
            who = (
                "我"
                if record["direction"] == 1
                else (record["sender_name"] or record["peer_id"])
            )
            mark = "★" if record.get("important") else " "
            print(
                f"[{stamp}] 已收录 {mark} [{record['chat_type']}] {who}: "
                f"{str(record['text'])[:70]}"
            )
        sys.stdout.flush()

    server = ReportServer(cfg, verbose_handler)
    try:
        server.start_background()
    except OSError as exc:
        print(f"无法监听 {cfg.onebot.report_host}:{cfg.onebot.report_port} —— {exc}")
        print("可能已有另一个实例在运行（后端或另一次 watch）。")
        store.close()
        return 2

    print("=" * 72)
    print("  实时监听模式（Ctrl+C 退出）")
    print("=" * 72)
    print(f"  本程序监听   : {server.address}")
    print(f"  NapCat API   : {cfg.onebot.api_base}")
    print(f"  大号 self_id : {cfg.onebot.self_id}")
    print()
    print("  请确认 NapCat 已在运行，且「HTTP 上报」指向上面的地址。")
    print("  然后从手机给自己发一条 QQ 消息，或让别人给你发一条。")
    print("  收到的事件会实时打印在下面。")
    print("=" * 72)
    print()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        print(f"共收到 {stats['total']} 个事件，其中入库 {stats['kept']} 条。")
        if stats["total"] == 0:
            print()
            print("一条都没收到？按顺序检查：")
            print("  1. NapCat 是否正在运行，并且已经登录大号")
            print(f"  2. NapCat 的「HTTP 上报」地址是否填成 {server.address}")
            print("  3. 该上报配置是否处于「启用」状态")
            print("  4. 是否勾选了「上报自身消息」（否则你手机发出的消息不会上报）")
        print("实时监听已退出。")
    finally:
        server.stop()
        store.close()
    return 0


def cmd_classify(cfg: Config, force: bool, no_model: bool) -> int:
    """为库里已知的会话打标签（结果缓存，之后不重复调用模型）。"""
    store = Store(cfg.db_path)
    try:
        peers = store.list_peers()
        if not peers:
            print("数据库里还没有任何消息，先让后端采集一段时间再分类。")
            return 0
        cached = store.get_peer_tags()
        print(f"库中共 {len(peers)} 个会话，已有标签 {len(cached)} 个。")
        if not cfg.tags.enabled:
            print("[tags].enabled = false，标签功能已关闭。")
            return 1

        model_needed = cfg.tags.auto_classify and not no_model
        todo = [p for p in peers if force or p[0] not in cached]
        if model_needed and todo:
            print(f"将调用本地模型识别最多 {len(todo)} 个会话（可用的先用规则判定，不调模型）。")
        print("开始分类…\n")

        def progress(idx: int, total: int, decision) -> None:
            print(
                f"  [{idx}/{total}] {decision.peer_name or decision.peer_id} "
                f"-> {decision.label}  [{decision.source}]  {decision.detail}"
            )

        stats = tagging.classify_peers(
            store,
            cfg,
            LlmClient(cfg.llm) if model_needed else None,
            force=force,
            use_model=model_needed,
            progress=progress,
        )
        print()
        print("=" * 72)
        print(
            f"完成：共 {stats['total']} 个会话"
            f"（跳过已缓存 {stats['skipped']}，手工 {stats.get('manual', 0)}，"
            f"关键词 {stats.get('keyword', 0)}，模型 {stats.get('model', 0)}，"
            f"未识别回落 {stats.get('default', 0)}）"
        )
        print("=" * 72)
        print()
        print(tagging.describe(store))
        return 0
    finally:
        store.close()


def cmd_compress(cfg: Config) -> int:
    """把当前窗口内低权重会话的消息逐群预压缩存本地。"""
    from qqdigest.compress import compress_window

    store = Store(cfg.db_path)
    try:
        now = int(datetime.now(cfg.tz).timestamp())
        start = store.get_window_end(now - cfg.general.first_run_lookback_hours * 3600)
        print(f"窗口：{datetime.fromtimestamp(start, cfg.tz):%Y-%m-%d %H:%M} ~ "
              f"{datetime.fromtimestamp(now, cfg.tz):%Y-%m-%d %H:%M}")
        print(f"高权重阈值：{cfg.general.no_compress_weight}（>= 此值不压缩，保留原文）")
        msgs = store.fetch_messages(start, now, include_muted=False)
        print(f"窗口内消息 {len(msgs)} 条")
        stats = compress_window(
            store, cfg, LlmClient(cfg.llm),
            window_start=start, window_end=now,
            progress=lambda i, n, name: print(f"  [{i}/{n}] {name}"),
        )
        print()
        print("=" * 72)
        print(stats.summary())
        print(f"  prompt={stats.prompt_tokens} completion={stats.completion_tokens}")
        print("=" * 72)
        summaries = store.get_compressed_summaries(start, now)
        for pid, text in list(summaries.items())[:8]:
            row = store.get_peer_tag(pid) or {}
            print(f"\n【{row.get('peer_name') or pid}】权重 {row.get('weight')}")
            print("  " + str(text).replace(chr(10), chr(10) + "  ")[:400])
        return 0
    finally:
        store.close()


def cmd_tags(cfg: Config) -> int:
    """查看当前会话标签与权重。"""
    store = Store(cfg.db_path)
    try:
        print(f"标签配置：{ '、'.join(cfg.tags.labels) }")
        print("权重：" + "  ".join(f"{k}={v}" for k, v in sorted(
            cfg.tags.weights.items(), key=lambda kv: -kv[1])))
        print(f"私聊权重：{cfg.tags.private_weight}　"
              f"默认标签：{cfg.tags.default_label}　"
              f"权重0排除：{cfg.tags.exclude_weight_zero}")
        print("=" * 72)
        print(tagging.describe(store))
        return 0
    finally:
        store.close()


def _check_port(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def cmd_check(cfg: Config) -> int:
    print(f"qqdigest {__version__} 体检")
    print("-" * 72)
    print(f"项目根目录     : {cfg.root}")
    print(f"配置文件       : {cfg.root / 'config.toml'}")
    print(f"数据库         : {cfg.db_path}")
    print(f"时区           : {cfg.general.timezone} -> {cfg.tz}")
    print(f"大号 self_id   : {cfg.onebot.self_id}")
    print(f"发送到 send_to : {cfg.target.send_to}")
    print(f"发送时刻       : {', '.join(cfg.general.schedule)}（提前 {cfg.general.precompute_lead_min} 分钟预计算）")
    print(f"干跑模式       : {cfg.general.dry_run}")
    print(f"模型           : {cfg.llm.model} @ {cfg.llm.base_url}")
    print(f"max_tokens     : {cfg.llm.max_tokens} / no_think={cfg.llm.no_think}")
    print(f"采集范围       : 私聊={cfg.filter.collect_private} 群={cfg.filter.collect_group} "
          f"白名单={list(cfg.filter.group_whitelist) or '（全部）'}")
    print("-" * 72)

    rc = 0

    # 1) 本地模型
    try:
        llm = LlmClient(cfg.llm)
        models = llm.list_models()
        hit = any(cfg.llm.model in m or m in cfg.llm.model for m in models)
        print(f"[模型服务] 可达，已加载/可用模型 {len(models)} 个")
        if not hit:
            print(f"  ⚠ 未找到配置的模型 {cfg.llm.model}，当前列表：{models}")
            rc = 1
    except LlmError as exc:
        print(f"[模型服务] ✗ {exc}")
        rc = 1

    # 2) NapCat API
    if _check_port("127.0.0.1", _port_of(cfg.onebot.api_base)):
        try:
            info = OneBotClient(cfg.onebot).get_login_info()
            data = info.get("data") or {}
            print(f"[NapCat API] 可达，当前登录账号：{data.get('user_id')}（{data.get('nickname')}）")
            if str(data.get("user_id")) != cfg.onebot.self_id:
                print(f"  ⚠ NapCat 登录的是 {data.get('user_id')}，配置里是 {cfg.onebot.self_id}")
                rc = 1
        except OneBotError as exc:
            print(f"[NapCat API] ✗ {exc}")
            rc = 1
    else:
        print(f"[NapCat API] ✗ 端口未监听：{cfg.onebot.api_base}（NapCat 未启动或未开启 HTTP 服务端）")
        rc = 1

    # 3) 上报端口
    if _check_port(cfg.onebot.report_host, cfg.onebot.report_port):
        print(f"[上报端口] ⚠ {cfg.onebot.report_port} 已被占用，可能已有实例在运行")
        rc = 1
    else:
        print(f"[上报端口] {cfg.onebot.report_host}:{cfg.onebot.report_port} 空闲，可绑定")
        print(f"  请在 NapCat 中把「HTTP 上报」指向 http://{cfg.onebot.report_host}:{cfg.onebot.report_port}/")

    print("-" * 72)
    print("体检通过，可以启动。" if rc == 0 else "体检发现问题，请按上面提示处理。")
    return rc


def _port_of(url: str) -> int:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def cmd_stats(cfg: Config) -> int:
    store = Store(cfg.db_path)
    try:
        total = store.count_messages()
        now = datetime.now(cfg.tz)
        now_ts = int(now.timestamp())
        print(f"数据库：{cfg.db_path}")
        print(f"累计收录消息：{total} 条")
        print()
        print("最近 24 小时按会话统计：")
        rows = store.stats_by_peer(now_ts - 86400, now_ts + 1)
        if not rows:
            print("  （无）")
        for peer_id, peer_name, n in rows[:20]:
            print(f"  {n:>5} 条  {peer_name or peer_id}  ({peer_id})")
        print()
        print("最近简报：")
        for d in store.recent_digests(8):
            print(
                f"  #{d['id']:<4} {str(d.get('slot')):<20} {d.get('state'):<8} "
                f"msg={d.get('msg_count'):<5} {str(d.get('content') or '')[:40]!r}"
            )
        return 0
    finally:
        store.close()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qqdigest",
        description="QQ 消息定时整合系统（全本地、零第三方依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "once", "watch", "digest-now", "selftest", "check",
                            "stats", "classify", "tags", "compress"])
    p.add_argument("--config", default=None, help="config.toml 路径（默认取项目根目录）")
    p.add_argument("--hold", action="store_true", help="digest-now：生成后挂起，不让调度器自动发送")
    p.add_argument("--send", action="store_true", help="digest-now：生成后立刻发送到 QQ")
    p.add_argument("--force-send", action="store_true",
                   help="digest-now：即使在干跑模式下也真的发送（仅这一次）")
    p.add_argument("--force", action="store_true", help="classify：忽略缓存重新分类")
    p.add_argument("--no-model", action="store_true", help="classify：只用规则，不调用模型")
    p.add_argument("--debug", action="store_true", help="输出 DEBUG 级日志")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    ensure_dirs(cfg)
    setup_logging(cfg.storage.logs_dir, level=logging.DEBUG if args.debug else logging.INFO)

    dispatch = {
        "run": lambda: cmd_run(cfg),
        "once": lambda: cmd_once(cfg),
        "watch": lambda: cmd_watch(cfg),
        "digest-now": lambda: cmd_digest_now(cfg, args.hold, args.send, args.force_send),
        "classify": lambda: cmd_classify(cfg, args.force, args.no_model),
        "tags": lambda: cmd_tags(cfg),
        "compress": lambda: cmd_compress(cfg),
        "selftest": lambda: cmd_selftest(cfg),
        "check": lambda: cmd_check(cfg),
        "stats": lambda: cmd_stats(cfg),
    }
    return dispatch[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
