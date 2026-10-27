"""tests/test_state_boot.py — 跨重启 monotonic 时间戳消毒（P0 回归）。

事故背景（2026-10-27）：state.json 里的 last_spoken_at 是 monotonic 值，
电脑重启后 monotonic 归零，旧值变成"未来时间"。gate 里
`now - last_spoken_at < 30` 于是恒成立 → 主动搭话**永久静默**，
而观察循环照旧每几秒调一次 API（钱照烧，屏幕毫无反应）。

判定标准：
1. 加载时必须清掉来自旧开机周期的时间戳；
2. 运行期 gate 也要能自愈（state 文件被外部改坏的情况）；
3. 不能过度修复——正常窗口内的节流必须保留。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.gate import SpeechGate
from core.state import StateStore
from core.types import GateConfig, RuntimeState


def write_state(tmp_path: Path, data: dict) -> StateStore:
    (tmp_path / "state.json").write_text(json.dumps(data), encoding="utf-8")
    return StateStore(tmp_path / "state.json", tmp_path / "decisions.jsonl")


# ---- 1. 加载期消毒 ----

def test_load_resets_future_last_spoken(tmp_path: Path) -> None:
    """旧开机周期（值远大于当前 monotonic）→ 必须归零。"""
    future = time.monotonic() + 33_000
    store = write_state(tmp_path, {"last_spoken_at": future})
    assert store.state.last_spoken_at == 0.0


def test_load_keeps_recent_last_spoken(tmp_path: Path) -> None:
    """本周期内的值不能动，否则节流形同虚设。"""
    recent = time.monotonic() - 5.0
    store = write_state(tmp_path, {"last_spoken_at": recent})
    assert store.state.last_spoken_at == recent


def test_load_resets_future_hour_window(tmp_path: Path) -> None:
    """窗口起点被重置 = 新窗口从此刻开始，旧周期计数必须一起清零，
    否则这一小时会平白少说话。"""
    store = write_state(tmp_path, {
        "hour_window_start": time.monotonic() + 33_000,
        "spoke_this_hour": 7,
    })
    assert store.state.hour_window_start <= time.monotonic()
    assert store.state.spoke_this_hour == 0


def test_load_prunes_stale_cooldown(tmp_path: Path) -> None:
    now = time.monotonic()
    store = write_state(tmp_path, {
        "text_cooldown": {
            "old1": now + 30_000,     # 旧周期残留
            "old2": now + 33_000,
            "ok": now + 60,           # 正常冷却，保留
        }
    })
    assert set(store.state.text_cooldown) == {"ok"}


def test_load_resets_absurd_suspension(tmp_path: Path) -> None:
    store = write_state(tmp_path, {"suspended_until": time.monotonic() + 30_000})
    assert store.state.suspended_until == 0.0


# ---- 2. 运行期自愈（gate 双保险） ----

def test_gate_heals_future_timestamp(tmp_path: Path) -> None:
    """即便 state 在内存里被写成未来值，gate 也不能永久拒绝。"""
    store = StateStore(tmp_path / "state.json", tmp_path / "decisions.jsonl")
    gate = SpeechGate(GateConfig(), store)
    store.state.last_spoken_at = time.monotonic() + 33_000

    allowed, reason = gate.allow("proactive", time.monotonic())
    assert allowed is True and reason == ""


def test_gate_still_throttles_within_window(tmp_path: Path) -> None:
    """不过度修复：刚说过话仍要被拦。"""
    store = StateStore(tmp_path / "state.json", tmp_path / "decisions.jsonl")
    gate = SpeechGate(GateConfig(), store)
    now = time.monotonic()
    store.state.last_spoken_at = now - 5.0

    allowed, reason = gate.allow("proactive", now)
    assert allowed is False and reason == "throttle"


# ---- 3. roll_hour 负差防御 ----

def test_roll_hour_resets_on_negative_gap() -> None:
    st = RuntimeState(hour_window_start=45_000.0, spoke_this_hour=99)
    st.roll_hour(15_000.0)   # 模拟重启后 monotonic 归零
    assert st.spoke_this_hour == 0
    assert st.hour_window_start == 15_000.0


def test_roll_hour_keeps_count_inside_window() -> None:
    st = RuntimeState(hour_window_start=100.0, spoke_this_hour=5)
    st.roll_hour(200.0)
    assert st.spoke_this_hour == 5
