"""core/gate.py — SpeechGate：L 层唯一放行点（确定性代码，模型输出不可绕过）。

主动模式查：30s 最小间隔 / ≤10 次每小时 / 日预算（余量 ≤ 保底额度时拒绝主动发言，
把额度留给会话）/ 免打扰 / 黑名单。被动模式仅 2s 防连点。
"""

from __future__ import annotations

import time

from .state import StateStore
from .types import GateConfig


class SpeechGate:
    def __init__(self, cfg: GateConfig, store: StateStore) -> None:
        self.cfg = cfg
        self.store = store
        self._last_reactive = 0.0

    def allow(self, mode: str, now: float, dnd: bool = False,
              blacklisted: bool = False) -> tuple[bool, str]:
        state = self.store.state
        if blacklisted:
            return False, "blacklist"
        if dnd:
            return False, "dnd"

        if mode == "reactive":
            # 被动回应：不受主动节流约束（用户已确认"搭话必须回应"），仅防连点
            if now - self._last_reactive < 2.0:
                return False, "reactive-spam"
            return True, ""

        # 主动模式
        if now - state.last_spoken_at < self.cfg.proactive_min_interval_sec:
            return False, "throttle"
        state.roll_hour(now)
        if state.spoke_this_hour >= self.cfg.max_proactive_per_hour:
            return False, "hourly-cap"
        remaining = self.cfg.daily_call_budget - state.calls_today
        if remaining <= 0:
            return False, "budget-exhausted"
        if remaining <= self.cfg.session_reserve_calls:
            # 余量进入保底区：留给被动会话，观察循环挂起
            return False, "budget-reserve"
        if now < state.suspended_until:
            return False, "suspended"
        return True, ""

    def mark_reactive(self, now: float) -> None:
        self._last_reactive = now

    def observe_suspended(self, now: float) -> bool:
        """观察循环是否该挂起（预算进入保底区或退避中）。"""
        state = self.store.state
        if now < state.suspended_until:
            return True
        remaining = self.cfg.daily_call_budget - state.calls_today
        return remaining <= self.cfg.session_reserve_calls
