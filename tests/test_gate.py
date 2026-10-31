"""tests/test_gate.py — SpeechGate 单测（时间注入，不 sleep）。

覆盖：30s 节流 / 每小时上限 / 预算保底区 / 防连点 / 并发计数不丢更新。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.gate import SpeechGate
from core.state import StateStore
from core.types import GateConfig


def make_gate(tmp_path: Path) -> tuple[SpeechGate, StateStore]:
    store = StateStore(tmp_path / "state.json", tmp_path / "decisions.jsonl")
    return SpeechGate(GateConfig(), store), store


def test_proactive_throttle(tmp_path: Path) -> None:
    gate, store = make_gate(tmp_path)
    now = 1000.0
    store.state.last_spoken_at = now
    store.save()
    assert gate.allow("proactive", now) == (False, "throttle")
    assert gate.allow("proactive", now + 31) == (True, "")
    print("PASS 30s 节流")


def test_hourly_cap(tmp_path: Path) -> None:
    gate, store = make_gate(tmp_path)
    now = 1000.0
    store.state.hour_window_start = now
    store.state.spoke_this_hour = 10
    store.save()
    assert gate.allow("proactive", now + 100) == (False, "hourly-cap")
    print("PASS 每小时 10 次上限")


def test_budget_reserve(tmp_path: Path) -> None:
    """启用预算限制时才按日预算/保底额度收口。"""
    store = StateStore(tmp_path / "state.json", tmp_path / "decisions.jsonl")
    gate = SpeechGate(GateConfig(budget_enabled=True), store)
    now = 1000.0
    store.state.calls_today = 250          # 余量 50 ≤ 保底 60
    store.state.calls_day = time.strftime("%Y-%m-%d")
    store.save()
    assert gate.allow("proactive", now + 100) == (False, "budget-reserve")
    assert gate.observe_suspended(now + 100) is True
    store.state.calls_today = 100
    assert gate.allow("proactive", now + 100) == (True, "")
    print("PASS 预算保底区（余量≤60 拒绝主动发言，留给会话）")


def test_budget_disabled_by_default(tmp_path: Path) -> None:
    """默认不限制调用次数（2026-10-31 用户拍板）：花销改用用量面板事后可见。

    反向断言：解除预算限制不等于放行一切——连续失败退避仍然要生效。
    """
    gate, store = make_gate(tmp_path)
    now = 1000.0
    store.state.calls_today = 9999          # 远超旧预算，本该被拦
    store.state.calls_day = time.strftime("%Y-%m-%d")
    store.save()
    assert gate.allow("proactive", now + 100) == (True, "")
    assert gate.observe_suspended(now + 100) is False

    store.state.suspended_until = now + 200   # 退避仍需生效
    assert gate.observe_suspended(now + 100) is True
    assert gate.allow("proactive", now + 100) == (False, "suspended")
    print("PASS 默认不设预算上限，但失败退避仍然收口")


def test_reactive_spam(tmp_path: Path) -> None:
    gate, _ = make_gate(tmp_path)
    now = 1000.0
    assert gate.allow("reactive", now) == (True, "")
    gate.mark_reactive(now)
    assert gate.allow("reactive", now + 1) == (False, "reactive-spam")
    assert gate.allow("reactive", now + 3) == (True, "")
    print("PASS 被动 2s 防连点")


def test_concurrent_counting(tmp_path: Path) -> None:
    """并发 mark_api_call 不丢更新（防击穿 ≤10 次/小时与日预算）。"""
    gate, store = make_gate(tmp_path)
    today = time.strftime("%Y-%m-%d")
    threads = [threading.Thread(target=store.mark_api_call, args=(today,))
               for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.state.calls_today == 50, f"计数丢失: {store.state.calls_today}"
    print("PASS 并发计数 50/50 无丢失")


def main() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_proactive_throttle(tmp)
        test_hourly_cap(tmp)
        test_budget_reserve(tmp)
        test_budget_disabled_by_default(tmp)
        test_reactive_spam(tmp)
        test_concurrent_counting(tmp)
    print("\n全部通过。")


if __name__ == "__main__":
    main()
