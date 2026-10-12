"""调度层：两段式「提前预计算 + 整点发送」。

核心规则（避免丢消息）：
    采集窗口的结束边界 = **预计算时刻**，而不是发送时刻。
    例：12:00 发送、提前 5 分钟 -> 11:55 预计算，窗口为 [上次窗口末, 11:55)；
        下一档从 11:55 起算。11:55~12:00 到的消息顺延到下一档，总体零丢失。

档位幂等：``digests.slot`` 唯一约束保证同一档位只会生成一次简报，进程重启也不会重复发送。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from .config import Config
from .digest import run_digest
from .llm import LlmClient
from .sender import send_digest, send_failure_notice
from .store import Store

log = logging.getLogger("qqdigest.scheduler")


@dataclass(frozen=True)
class Slot:
    """一个发送档位。"""

    key: str  # 例：2026-02-05T12:00
    fire_at: int  # 发送时刻（Unix 秒）
    precompute_at: int  # 预计算时刻（Unix 秒）

    def describe(self) -> str:
        return self.key.replace("T", " ")


def _parse_hm(value: str) -> tuple[int, int]:
    hh, mm = value.split(":", 1)
    return int(hh), int(mm)


def slots_for_day(day: date, cfg: Config, tz: tzinfo) -> list[Slot]:
    """计算某一天的全部档位。

    ``schedule_mode = "hourly"`` 时按**工作时间内的每个整点**生成档位
    （例如 08:00~23:00 → 16 档）；否则用 ``schedule`` 里显式列出的时刻。
    """
    lead = cfg.general.precompute_lead_min * 60
    times: list[tuple[int, int]] = []

    if cfg.general.schedule_mode == "hourly":
        start_h, start_m = _parse_hm(cfg.general.work_hours_start)
        end_h, end_m = _parse_hm(cfg.general.work_hours_end)
        first = start_h * 60 + start_m
        last = end_h * 60 + end_m
        if last < first:
            raise ValueError(
                f"工作时间设置反了：{cfg.general.work_hours_start} ~ {cfg.general.work_hours_end}"
            )
        minute = first
        while minute <= last:
            times.append((minute // 60, minute % 60))
            minute += 60
    else:
        for hm in cfg.general.schedule:
            times.append(_parse_hm(hm))

    out: list[Slot] = []
    seen: set[str] = set()
    for hh, mm in times:
        fire = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
        fire_ts = int(fire.timestamp())
        key = fire.strftime("%Y-%m-%dT%H:%M")
        if key in seen:
            continue
        seen.add(key)
        out.append(Slot(key=key, fire_at=fire_ts, precompute_at=fire_ts - lead))
    out.sort(key=lambda s: s.fire_at)
    return out


def slots_around(now: datetime, cfg: Config, *, days_back: int = 1, days_ahead: int = 1) -> list[Slot]:
    """取前后若干天的档位，按发送时刻升序。"""
    tz = now.tzinfo or cfg.tz
    out: list[Slot] = []
    for offset in range(-days_back, days_ahead + 1):
        day = (now + timedelta(days=offset)).date()
        out.extend(slots_for_day(day, cfg, tz))
    out.sort(key=lambda s: s.fire_at)
    return out


class Scheduler:
    """调度器：被 :meth:`tick` 驱动，便于测试与一次性执行。"""

    def __init__(self, store: Store, cfg: Config, llm: LlmClient) -> None:
        self.store = store
        self.cfg = cfg
        self.llm = llm

    # ------------------------------------------------------------------ 工具
    def _default_window_start(self, now_ts: int) -> int:
        return now_ts - self.cfg.general.first_run_lookback_hours * 3600

    def _bounded_window_start(self, now_ts: int) -> int:
        """水位线兜底：为空取回溯值，过旧则按最长窗口截断。"""
        raw_start = self.store.get_window_end(self._default_window_start(now_ts))
        max_span = self.cfg.general.max_window_hours * 3600
        if now_ts - raw_start > max_span:
            log.warning(
                "上次窗口末距今超过 %d 小时，按最长窗口截断（更早的消息不会被整合）",
                self.cfg.general.max_window_hours,
            )
            raw_start = now_ts - max_span
        return min(raw_start, now_ts)

    # ------------------------------------------------------------------ 预计算
    def precompute(self, slot: Slot, now_ts: int) -> int | None:
        """为某档生成简报（窗口结束边界 = 当前时刻）。

        先做**低权重会话的逐群预压缩**，再整合。这样：
        - 压缩天然跟着档位按小时发生（工作时间内的每个整点）；
        - 摘要刚生成就被用掉，不存在过期问题；
        - 高权重会话不参与压缩，原文进上下文。
        """
        window_start = self._bounded_window_start(now_ts)
        window_end = now_ts

        if self.cfg.general.compress_low_weight:
            try:
                from .compress import compress_window

                stats = compress_window(
                    self.store, self.cfg, self.llm,
                    window_start=window_start, window_end=window_end,
                )
                if stats.peers or stats.skipped_high or stats.skipped_muted:
                    log.info("预压缩：%s", stats.summary())
            except Exception:
                log.exception("预压缩失败，本次整合将使用原文")

        try:
            digest_id = run_digest(
                self.store,
                self.cfg,
                self.llm,
                slot=slot.key,
                window_start=window_start,
                window_end=window_end,
                fire_at=slot.fire_at,
            )
        except Exception as exc:
            log.error("档位 %s 整合失败：%s", slot.describe(), exc)
            try:
                send_failure_notice(self.cfg, slot.key, str(exc))
            except Exception:
                log.exception("发送失败通知时又出错")
            return None

        if digest_id is not None:
            self.store.set_window_end(window_end)
            log.info(
                "档位 %s 预计算完成，窗口水位线推进到 %s",
                slot.describe(),
                datetime.fromtimestamp(window_end, self.cfg.tz).strftime("%Y-%m-%d %H:%M:%S"),
            )
        return digest_id

    # ------------------------------------------------------------------ 时间段
    def window_label(self, digest: dict[str, Any]) -> str:
        """生成人类可读的时间段标签：``上次成功发送时刻 → 本次发送时刻``。

        用户要求定时消息指明覆盖的时间段。这里用**发送时刻**做标签
        （12:00 → 13:00），而内部的数据边界用的是预计算时刻——
        标签给人看，水位线保证不丢消息，两者各司其职。
        """
        fire_at = int(digest.get("fire_at") or 0)
        prev = self.store.get_state("last_success_fire_at")
        try:
            prev_ts = int(prev) if prev else 0
        except ValueError:
            prev_ts = 0
        if not prev_ts:
            prev_ts = int(digest.get("window_start") or fire_at)
        fmt = "%m-%d %H:%M"
        return (
            f"{datetime.fromtimestamp(prev_ts, self.cfg.tz).strftime(fmt)}"
            f" → "
            f"{datetime.fromtimestamp(fire_at, self.cfg.tz).strftime(fmt)}"
        )

    # ------------------------------------------------------------------ 发送
    def send_due(self, now_ts: int) -> int:
        """发送所有已到时刻的简报，返回成功条数。

        定时发送成功后（可选）删除该窗口内已投递的消息——
        **手动"立即整合并发送"不会走到这里，因此不会删数据。**
        """
        pending = self.store.due_digests(now_ts)
        ok_count = 0
        retry_window = self.cfg.general.send_retry_window_min * 60
        for d in pending:
            label = self.window_label(d)
            row = dict(d)
            row["window_label"] = label
            ok, detail = send_digest(self.cfg, row)
            slot = str(d.get("slot") or "")
            if ok:
                self.store.mark_sent(int(d["id"]))
                self.store.set_state("last_success_fire_at", str(int(d.get("fire_at") or now_ts)))
                ok_count += 1
                log.info("档位 %s [%s] %s", slot.replace("T", " "), label, detail)
                self._maybe_purge(d)
            else:
                overdue = now_ts - int(d.get("fire_at") or now_ts)
                if overdue > retry_window:
                    self.store.mark_failed(int(d["id"]), detail)
                    log.error("档位 %s 发送最终失败：%s", slot, detail)
                else:
                    log.warning("档位 %s 发送失败，稍后重试：%s", slot, detail)
        return ok_count

    def _maybe_purge(self, digest: dict[str, Any]) -> None:
        """定时发送成功后清理已投递窗口内的消息（可配置关闭）。"""
        if not self.cfg.storage.delete_after_scheduled_send:
            return
        start = int(digest.get("window_start") or 0)
        end = int(digest.get("window_end") or 0)
        if end <= start:
            return
        try:
            removed = self.store.purge_range(start, end)
            log.info("已清理该时段已投递的消息 %d 条（%d ~ %d）", removed, start, end)
        except Exception:
            log.exception("清理已投递消息时出错")

    # ------------------------------------------------------------------ 主循环
    def tick(self, now: datetime | None = None) -> dict[str, int]:
        """执行一轮检查。返回本轮统计，便于测试断言。"""
        now = now or datetime.now(self.cfg.tz)
        now_ts = int(now.timestamp())
        stats = {"precomputed": 0, "sent": 0, "skipped": 0}

        catch_up = self.cfg.general.catch_up_max_min * 60
        for slot in slots_around(now, self.cfg):
            if slot.precompute_at > now_ts:
                continue  # 还没到预计算时刻
            if self.store.has_digest_for_slot(slot.key):
                continue  # 已处理过

            if now_ts - slot.fire_at > catch_up:
                # 程序当时没运行，且已错过太久：登记为跳过，避免反复尝试
                digest_id = self.store.create_digest(
                    slot.key,
                    window_start=slot.precompute_at,
                    window_end=slot.precompute_at,
                    fire_at=slot.fire_at,
                )
                if digest_id is not None:
                    self.store.mark_failed(digest_id, "程序当时未运行，已跳过该档")
                    stats["skipped"] += 1
                    log.warning("档位 %s 已错过且超出补发窗口，跳过", slot.describe())
                continue

            if self.precompute(slot, now_ts) is not None:
                stats["precomputed"] += 1

        stats["sent"] = self.send_due(now_ts)
        return stats

    def run_forever(self, *, stop_flag: list[bool] | None = None) -> None:
        """常驻循环。``stop_flag`` 为可被外部置位的单元素列表，便于测试停止。"""
        log.info(
            "调度器启动：每日 %s 发送，提前 %d 分钟预计算，干跑=%s",
            "、".join(self.cfg.general.schedule),
            self.cfg.general.precompute_lead_min,
            self.cfg.general.dry_run,
        )
        while True:
            if stop_flag is not None and stop_flag and stop_flag[0]:
                log.info("收到停止信号，调度器退出")
                return
            try:
                stats = self.tick()
                if any(stats.values()):
                    log.info("本轮：%s", stats)
            except Exception:
                log.exception("调度循环出错，将在下一轮重试")
            time.sleep(max(1, self.cfg.general.poll_interval_sec))
