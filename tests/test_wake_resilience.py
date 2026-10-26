"""tests/test_wake_resilience.py — 唤醒监听音频设备自愈合回归测试。

背景（用户实测反馈 + 本机声学 e2e 复现）：
    MME 输入流会被瞬时故障打断——别的程序抢占麦克风、音频栈重初始化
    （用户机器还挂着麦克风 AI 降噪软件）。旧版 run() 遇到 read 异常直接
    break 退出线程：唤醒监听永久假死，症状是"第一次叫有回应，之后再叫
    没反应"，日志留着「麦克风读取中断，唤醒监听停止。」。

现在必须：读异常 / 读卡死（read_available 不增长）都走退避重开流，
恢复后重置捕获状态继续听，stop() 能随时打断退避。这组测试钉住行为。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import Qt

from core.types import WakeConfig
from voice.wake import WakeListener


class _FakeRec:
    def __init__(self) -> None:
        self.accepts = 0
        self.resets = 0

    def AcceptWaveform(self, pcm):
        self.accepts += 1
        return False

    def PartialResult(self):
        return '{"partial": ""}'

    def Result(self):
        return '{"text": ""}'

    def Reset(self):
        self.resets += 1


class _FakeStream:
    """可控假音频流。mode: ok=正常 / raise=read 抛异常 / stall=无数据。"""

    def __init__(self, mode: str = "ok", loud: bool = False) -> None:
        self.mode = mode
        self.loud = loud
        self.reads = 0
        self.closed = False
        self._available = 1600

    @property
    def read_available(self) -> int:
        return 0 if self.mode == "stall" else self._available

    def read(self, n: int):
        self.reads += 1
        if self.mode == "raise":
            raise OSError("设备已被其他程序占用")
        if self.loud:
            return np.full((n, 1), 16000, dtype=np.int16), False
        return np.zeros((n, 1), dtype=np.int16), False

    def close(self) -> None:
        self.closed = True


def _install_fake_setup(w: WakeListener, stream_seq: list[_FakeStream]) -> None:
    """把 _setup 换成装假流的版本（按序取，取完再造正常的）。"""

    def fake_setup() -> None:
        w._rec = _FakeRec()
        w._stream = stream_seq.pop(0) if stream_seq else _FakeStream()

    w._setup = fake_setup


def _wait_for(pred, timeout: float, what: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    print(f"[timeout] {what}")
    return False


def test_read_failure_recovers() -> None:
    """read 抛异常 → 重开流继续听，线程不退（旧版这里永久退出）。"""
    w = WakeListener(WakeConfig(phrase="小鲸鱼"))
    bad, good = _FakeStream(mode="raise"), _FakeStream(mode="ok", loud=True)
    _install_fake_setup(w, [bad, good])
    logs: list[str] = []
    w.log_line.connect(logs.append, Qt.DirectConnection)
    w.start()
    try:
        assert _wait_for(lambda: any("麦克风已恢复" in l for l in logs), 10,
                         "等待重开恢复"), logs
        assert any("麦克风异常" in l for l in logs), logs
        assert bad.closed, "坏流必须被关闭"
        assert _wait_for(lambda: w._rec.accepts > 0, 5, "恢复后继续拾音"), logs
        assert w.isRunning(), "线程必须还活着（旧版遇到异常已退出）"
        print("PASS read 异常后重开恢复，线程存活")
    finally:
        w.stop()
        assert w.wait(3000), "线程未能在 3s 内退出"


def test_stall_recovers() -> None:
    """read_available 不增长（设备卡死）→ 超时重开，不静默挂死。"""
    import voice.wake as wake_mod
    old = wake_mod.STALL_SEC
    wake_mod.STALL_SEC = 0.3
    try:
        w = WakeListener(WakeConfig(phrase="小鲸鱼"))
        stuck, good = _FakeStream(mode="stall"), _FakeStream(mode="ok")
        _install_fake_setup(w, [stuck, good])
        logs: list[str] = []
        w.log_line.connect(logs.append, Qt.DirectConnection)
        w.start()
        try:
            assert _wait_for(lambda: any("麦克风已恢复" in l for l in logs), 10,
                             "等待卡死重开"), logs
            assert any("设备卡死" in l for l in logs), logs
            assert stuck.closed, "卡死的流必须被关闭"
            print("PASS 设备卡死后超时重开恢复")
        finally:
            w.stop()
            assert w.wait(3000)
    finally:
        wake_mod.STALL_SEC = old


def test_stop_interrupts_setup_backoff() -> None:
    """建流一直失败时，stop() 必须能打断退避睡眠（否则退出要等 10s+）。"""
    w = WakeListener(WakeConfig(phrase="小鲸鱼"))

    def failing_setup() -> None:
        raise RuntimeError("没有可用的麦克风")

    w._setup = failing_setup
    logs: list[str] = []
    w.log_line.connect(logs.append, Qt.DirectConnection)
    w.start()
    time.sleep(0.5)                     # 第一次失败 + 进入退避睡眠
    assert any("启动失败" in l for l in logs), logs
    t0 = time.monotonic()
    w.stop()
    assert w.wait(3000), "stop() 未能在 3s 内结束线程（退避睡眠挡住退出）"
    assert time.monotonic() - t0 < 2.5, "退出耗时过长"
    print("PASS stop() 及时打断重试退避")


def test_reset_after_reopen_clears_state() -> None:
    """重开后：半截指令丢弃、冷却清空、暂停过渡标志复位。"""
    w = WakeListener(WakeConfig(phrase="小鲸鱼"))
    w.feed("小鲸鱼", final=False)       # 进入 COMMAND
    assert w.state == WakeListener.COMMAND
    w._cooldown_until = time.monotonic() + 8
    w._cmd_finals = ["半截指令"]
    w._cmd_partial = "截"
    w._was_paused = True
    w._reset_after_reopen()
    assert w.state == WakeListener.IDLE
    assert w._cmd_finals == [] and w._cmd_partial == ""
    assert w._cooldown_until == 0.0
    assert w._was_paused is False
    print("PASS 重开后捕获状态重置")


def main() -> None:
    test_read_failure_recovers()
    test_stall_recovers()
    test_stop_interrupts_setup_backoff()
    test_reset_after_reopen_clears_state()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
