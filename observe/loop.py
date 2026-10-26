"""observe/loop.py — ObserveLoop：观察线程状态机（设计文档 §4.1）。

两条独立的主动开口路径（频率由设置窗「主动互动」控制）：
1. **到点搭话**（定时器）：每隔 [3600/最高, 3600/最低] 秒随机取一个间隔，
   到点就请她说一句——视觉开着带截图，关了就纯文字闲聊。
2. **画面变动**（事件）：dHash 变化超阈值 → JUDGE → 说话。
   频率设为"不主动互动"时两条都停（也不再截屏，省 CPU 与 API 花费）。

视觉关闭（mode=off）时只跑到点搭话，不截屏、不做 diff。
"""

from __future__ import annotations

import hashlib
import random
import time
from collections import deque
from datetime import date

from PySide6.QtCore import QThread, Signal

from core.state import StateStore, _fingerprint
from core.types import ObserveConfig, ProactiveConfig
from observe.differ import FrameDiffer
from observe.judge import VisionJudge, classify_error, encode_frame
from observe.screen import ScreenSource


def _today() -> str:
    return date.today().isoformat()


class ObserveLoop(QThread):
    state_changed = Signal(str)        # idle / think / work / talk / nap / error
    spoke = Signal(str)                # 宠物说出的文本（UI 气泡用）
    log_line = Signal(str)             # 人类可读的一行日志（控制台/UI）

    def __init__(self, cfg: ObserveConfig, screen: ScreenSource, differ: FrameDiffer,
                 judge: VisionJudge, gate, store: StateStore, player, engine,
                 recent_size: int = 3,
                 proactive_cfg: ProactiveConfig | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.screen = screen
        self.differ = differ
        self.judge = judge
        self.gate = gate
        self.store = store
        self.player = player
        self.engine = engine
        self.proactive_cfg = proactive_cfg or ProactiveConfig()
        self._recent: deque[str] = deque(maxlen=recent_size)
        self._stopped = False
        self._last_hash = 0
        self._last_api_at = 0.0
        self._active_until = 0.0
        self._next_proactive_at = 0.0     # 首次进循环时按频率抽一次

    def stop(self) -> None:
        self._stopped = True

    # ---- 主动互动频率 ----

    def _interval_sec(self) -> float:
        """按 [最低, 最高] 次/小时 抽一个随机间隔（秒）；不主动互动返回 0。

        短边 = 3600/最高（最频繁时的间隔），长边 = 3600/最低。
        两边都夹到 ≥20s：自定义填 300 次/小时也不会变成复读机。
        """
        lo, hi = self.proactive_cfg.resolve()
        if hi <= 0:
            return 0.0
        short = max(20.0, 3600.0 / max(1, hi))
        long = max(short, 3600.0 / max(1, lo))
        return random.uniform(short, long)

    def reschedule_proactive(self, now: float | None = None) -> None:
        """重抽下次主动搭话的时间点（说完了/改了设置就调一次）。"""
        now = time.monotonic() if now is None else now
        gap = self._interval_sec()
        self._next_proactive_at = (now + gap) if gap > 0 else float("inf")

    def proactive_hint(self) -> str:
        """一行人类可读的说明（日志/自检用）。"""
        lo, hi = self.proactive_cfg.resolve()
        if hi <= 0:
            return "不主动互动"
        return f"{lo}–{hi} 次/小时（间隔约 {3600 / hi:.0f}–{3600 / lo:.0f}s）"

    # ---- 主循环 ----

    def run(self) -> None:
        self.state_changed.emit("idle")
        self.reschedule_proactive()
        while not self._stopped:
            now = time.monotonic()

            # 会话 / 免打扰进行中让行
            mode = self.store.state.mode
            if mode in ("chat", "dnd"):
                self.msleep(500)
                continue

            # 预算进入保底区 / 退避挂起 → 打盹
            if self.gate.observe_suspended(now):
                self.state_changed.emit("nap")
                self.msleep(2000)
                continue

            vision_on = mode == "observe"
            proactive_off = self.proactive_cfg.is_off()

            # ---- 路径一：到点主动搭话（视觉无关） ----
            if not proactive_off and now >= self._next_proactive_at:
                self._proactive_turn(vision_on)
                continue

            if proactive_off or not vision_on:
                # 不主动互动 = 什么都不做；视觉关 = 只留到点搭话
                self.msleep(1000)
                continue

            # ---- 路径二：画面有较大变动 ----
            try:
                frame = self.screen.capture()
            except Exception as exc:
                self.log_line.emit(f"截屏失败: {exc}")
                self.msleep(3000)
                continue

            try:
                self._observe_once(frame)
            except Exception as exc:  # noqa: BLE001 兜底：意外异常不杀线程
                category = classify_error(exc)
                self.log_line.emit(f"观察循环异常[{category}]: {exc}")
                self.state_changed.emit("error")
                self.msleep(3000)

    def _observe_once(self, frame) -> None:
        """单轮观察（抽出来方便整体兜底）。"""
        now = time.monotonic()
        h = self.differ.dhash(frame)
        diff = self.differ.distance(self._last_hash, h)
        self._last_hash = h

        if diff < self.cfg.diff_threshold:
            interval = self.cfg.interval_active_sec if now < self._active_until \
                else self.cfg.interval_idle_sec
            self.msleep(int(interval * 1000))
            return

        # 隐私 / 免打扰
        if self.screen.title_blacklisted(self.gate.cfg.title_blacklist):
            self._record_skip("blacklist", diff)
            self.msleep(int(self.cfg.interval_idle_sec * 1000))
            return
        # 全屏（视频/游戏/演示）：画面本来就在动，diff 高是常态，不能当成
        # "值得开口"的信号——放进来会每 5s 烧一次 API。所以这条路径永远跳过；
        # 全屏时是否还能"到点搭话"由 gate.dnd_fullscreen 决定（见 _proactive_turn）。
        if self.screen.is_fullscreen():
            self._record_skip("fullscreen-motion", diff)
            self.msleep(int(self.cfg.interval_idle_sec * 1000))
            return

        # API 最小间隔
        if now - self._last_api_at < self.cfg.min_api_interval_sec:
            self.msleep(500)
            return
        self._last_api_at = now
        self._active_until = now + self.cfg.active_window_sec

        self._judge_and_maybe_speak(frame, diff)

    # ---- 主动搭话（到点触发） ----

    def _proactive_turn(self, vision_on: bool) -> None:
        """到点开口：隐私/免打扰先挡，再按视觉开关决定带不带画面。"""
        if self.screen.title_blacklisted(self.gate.cfg.title_blacklist):
            self._record_skip("blacklist", 0, mode="proactive")
            self.reschedule_proactive()
            self.msleep(int(self.cfg.interval_idle_sec * 1000))
            return
        if self.gate.cfg.dnd_fullscreen and self.screen.is_fullscreen():
            self._record_skip("fullscreen-quiet", 0, mode="proactive")
            self.reschedule_proactive()
            self.msleep(int(self.cfg.interval_idle_sec * 1000))
            return

        now = time.monotonic()
        if now - self._last_api_at < self.cfg.min_api_interval_sec:
            self.msleep(1000)       # 刚调过 API，稍等（不重抽，免得整轮错过）
            return
        self._last_api_at = now
        self.reschedule_proactive(now)      # 先排下一轮：失败也不会卡在重试里

        frame_b64 = None
        if vision_on:
            try:
                frame_b64 = encode_frame(self.screen.capture())
            except Exception as exc:  # noqa: BLE001 截不到就纯文字
                self.log_line.emit(f"主动搭话截屏失败（改纯文字）: {exc}")

        self._proactive_speak(frame_b64)

    def _proactive_speak(self, frame_b64: str | None) -> None:
        self.state_changed.emit("think")
        try:
            decision = self.judge.proactive(frame_b64,
                                            "；".join(self._recent) or "（无）")
        except Exception as exc:
            category = classify_error(exc)
            failures = self.store.mark_failure(time.monotonic())
            self.store.record_decision({"mode": "proactive", "trigger": "timer",
                                        "api_called": True, "error": category,
                                        "failures": failures})
            self.log_line.emit(f"主动搭话失败[{category}]: {exc}")
            self.state_changed.emit("error")
            if failures >= 5:
                backoff = min(600, 30 * (2 ** min(failures - 5, 5)))
                self.store.state.suspended_until = time.monotonic() + backoff
                self.store.save()
                self.log_line.emit(f"连续失败 {failures} 次，退避挂起 {backoff}s")
            self.msleep(3000)
            return

        self.store.reset_failures()
        self.store.mark_api_call(_today())

        allowed, gate_reason = False, ""
        if decision.speak and decision.text:
            fp = _fingerprint(decision.text)
            now = time.monotonic()
            if now < self.store.state.text_cooldown.get(fp, 0.0):
                gate_reason = "cooldown"
            else:
                allowed, gate_reason = self.gate.allow(
                    "proactive", now, dnd=False, blacklisted=False)

        self.store.record_decision({
            "mode": "proactive", "trigger": "timer", "api_called": True,
            "vision": bool(frame_b64),
            "decision": {"speak": decision.speak, "text": decision.text,
                         "reason": decision.reason},
            "gate": "allow" if allowed else (gate_reason or "silent"),
            "model": decision.model, "prompt_version": decision.prompt_version,
            "latency_ms": decision.latency_ms,
        })

        if allowed and decision.text:
            self._speak(decision.text)
            self.reschedule_proactive()      # 说完重新计时（间隔从这次开口算）
        elif decision.speak and decision.text:
            self.log_line.emit(f"主动搭话被拦[{gate_reason}]")
        self.state_changed.emit("idle")

    # ---- 判断与放行 ----

    def _judge_and_maybe_speak(self, frame, diff: int) -> None:
        self.state_changed.emit("think")
        try:
            decision = self.judge.judge(encode_frame(frame), "；".join(self._recent) or "（无）")
        except Exception as exc:
            category = classify_error(exc)
            failures = self.store.mark_failure(time.monotonic())
            self.store.record_decision({"mode": "observe", "diff": diff, "api_called": True,
                                        "error": category, "failures": failures})
            self.log_line.emit(f"API 失败[{category}]: {exc}")
            self.state_changed.emit("error")
            if failures >= 5:
                backoff = min(600, 30 * (2 ** min(failures - 5, 5)))
                self.store.state.suspended_until = time.monotonic() + backoff
                self.store.save()
                self.log_line.emit(f"连续失败 {failures} 次，退避挂起 {backoff}s")
            self.msleep(3000)
            return

        self.store.reset_failures()
        self.store.mark_api_call(_today())

        gate_reason = ""
        allowed = False
        if decision.speak and decision.text:
            fp = _fingerprint(decision.text)
            cooldown_until = self.store.state.text_cooldown.get(fp, 0.0)
            now = time.monotonic()
            if now < cooldown_until:
                gate_reason = "cooldown"
            else:
                allowed, gate_reason = self.gate.allow(
                    "proactive", now,
                    dnd=False, blacklisted=False)

        self.store.record_decision({
            "mode": "observe", "diff": diff, "api_called": True,
            "decision": {"speak": decision.speak, "text": decision.text,
                         "reason": decision.reason},
            "gate": "allow" if allowed else (gate_reason or "silent"),
            "model": decision.model, "prompt_version": decision.prompt_version,
            "latency_ms": decision.latency_ms,
        })

        if allowed and decision.text:
            self._speak(decision.text)
        elif decision.speak and decision.text and gate_reason == "cooldown":
            pass  # 打转抑制已记录
        self.state_changed.emit("idle")

    def _speak(self, text: str) -> None:
        now = time.monotonic()
        self.store.mark_spoken(now, text)
        fp = _fingerprint(text)
        # 打转检测：同一文本第三次出现进 10 分钟冷却
        self.store.state.text_cooldown[fp] = now + 600
        self.store.save()
        self._recent.append(text)
        utt_id = f"{fp}-{int(now * 1000)}"
        stop_event = self.engine.new_stop_event()
        self.player.enqueue_stream(utt_id, self.engine.stream(text, stop_event))
        self.spoke.emit(text)
        self.log_line.emit(f"开口: {text}")

    def _record_skip(self, reason: str, diff: int, mode: str = "observe") -> None:
        self.store.record_decision({"mode": mode, "diff": diff,
                                    "api_called": False, "gate": reason})
