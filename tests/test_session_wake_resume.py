"""tests/test_session_wake_resume.py — 会话结束后唤醒监听必须恢复。

修复「第一次有反应、之后没反应」：

根因：会话进行中会暂停唤醒监听（set_paused("session", True)），靠会话结束的
finished_session 信号释放。旧实现有两处脆弱点：
  1) ChatSession.run() 整体没有 try/finally——只要 judge.reply() 之外的任一步
     （gate.allow / 入队 TTS / 信号发射…）抛异常，run() 就在 set_mode("observe")
     和 finished_session.emit() 之前崩出 → mode 永远卡 chat + session 暂停永久泄漏
     → 唤醒监听被永久暂停。
  2) 释放暂停的 finished_session 连接在 session.start() 之后才建立——会话若很快
     结束（网络 401 / 内部异常）会在连接前发射，回调永远错过。

本测试用「allow() 必抛异常」的 gate 把会话做成体内瞬间崩，确定性复现竞态，
验证修复（run() 包 try/finally + ChatChannel.on_ended 在 start() 前连接）。
不依赖扬声器外放，CI / 无音频设备环境也能跑。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PySide6.QtCore import QCoreApplication, QThread

import core.paths as paths
from core.state import StateStore
from core.gate import SpeechGate
from main import load_config
from voice.moss import MossNanoEngine
from voice.player import SpeechPlayer
from voice.router import VoiceRouter
from chat.channel import ChatChannel
from voice.wake import WakeListener


class _FakeJudge:
    """瞬时返回，不做网络请求。"""

    def reply(self, user_text, history, frame_b64=None):
        return "（测试回复）好的主人。"


class _ThrowingGate:
    """allow() 必抛异常——把会话做成「体内瞬间崩」，触发竞态。"""

    def allow(self, kind, now):
        raise RuntimeError("模拟 gate.allow 异常（复现会话体内抛错）")

    def mark_reactive(self, now):
        pass


def _build(throwing: bool = False, idle_finish_sec: int = 2):
    app = QCoreApplication.instance() or QCoreApplication([])
    data_dir = paths.data_root()
    (pet, observe, gate_cfg, judge_cfg, voice_cfg, wake_cfg, chat, _pro,
     name, pv, style, addr) = load_config(paths.app_root() / "config.yaml", data_dir)
    store = StateStore(data_dir / "state.json", data_dir / "decisions.jsonl")
    store.set_mode("observe")
    # 这两个用例要真实加载 MOSS 引擎（验证唤醒暂停的释放时序），需要模型权重。
    # 开源仓库不含权重（models/ 只有 README），缺权重时跳过而不是报错——
    # 否则新克隆的人跑 pytest 会看到 2 个"失败"，误以为是代码坏了。
    if not list(Path(voice_cfg.model_dir).rglob("browser_poc_manifest.json")):
        pytest.skip("缺少 MOSS 模型权重（先跑 scripts/download_models.py），"
                    "跳过需要真实引擎的唤醒恢复测试")
    engine = MossNanoEngine(voice_cfg.model_dir, voice_cfg.voices_dir,
                          voice_cfg.voice_name, voice_cfg.threads, work_dir=data_dir)
    engine.load()
    player = SpeechPlayer(engine.sample_rate, engine.channels)
    pth = QThread()
    player.moveToThread(pth)
    pth.started.connect(player.run)
    pth.start()
    router = VoiceRouter(engine, player)
    judge = _FakeJudge()
    wake = WakeListener(wake_cfg, store)
    wake.start()
    gate = _ThrowingGate() if throwing else SpeechGate(gate_cfg, store)
    channel = ChatChannel(judge, store, gate, player, router, frame_source=None,
                         idle_finish_sec=idle_finish_sec,
                         on_ended=lambda: wake.set_paused("session", False))
    return {"app": app, "store": store, "engine": engine, "player": player,
            "pth": pth, "wake": wake, "channel": channel, "addr": addr}


def _teardown(env):
    env["wake"].stop()
    env["wake"].wait(3000)
    env["player"].shutdown()
    env["pth"].quit()
    env["pth"].wait(2000)


def _drive(env):
    """模拟：第一次唤醒 → 发指令 → 等待会话结束释放 session 暂停。"""
    app = env["app"]; store = env["store"]; wake = env["wake"]
    channel = env["channel"]; addr = env["addr"]; paused = wake._paused_reasons

    def on_woke(phrase):
        env["player"].enqueue_stream(
            f"ack-{int(time.time()*1000)}",
            env["engine"].stream("我在，" + addr + "。", env["engine"].new_stop_event()))
    wake.woke.connect(on_woke)
    wake.command.connect(lambda t: (wake.set_paused("session", True), channel.submit(t)))
    env["player"].started.connect(lambda _: wake.set_paused("speaking", True))
    env["player"].finished.connect(lambda _: wake.set_paused("speaking", False))

    wake.woke.emit("小鲸鱼")
    time.sleep(0.5)
    assert "speaking" in paused, "ack 播放中应处于 speaking 暂停"

    wake.command.emit("今天天气怎么样")
    t0 = time.monotonic()
    while "session" in paused and time.monotonic() - t0 < 15:
        app.processEvents()
        time.sleep(0.05)
    return paused, wake, store


def test_session_end_resumes_wake_normal():
    env = _build(throwing=False)
    paused, wake, store = _drive(env)
    try:
        # 关键断言：会话结束后 "session" 暂停必须被释放（用户报的"聊一次就喊不醒"）
        assert "session" not in paused, f"会话结束后 session 暂停未释放: {sorted(paused)}"
        assert wake.isRunning(), "监听器线程不应退出"
        # mode 必须恢复 observe（否则 _is_paused 会因 mode!=observe 一直为真）
        assert store.state.mode == "observe", f"mode 应恢复 observe，实为 {store.state.mode}"
    finally:
        _teardown(env)


def test_session_exception_still_resumes_wake():
    """决定性回归：会话体内瞬间抛异常，旧代码会永久泄漏 session 暂停。"""
    env = _build(throwing=True)
    paused, wake, store = _drive(env)
    try:
        assert "session" not in paused, f"异常会话结束后 session 暂停未释放: {sorted(paused)}"
        assert wake.isRunning(), "监听器线程不应退出"
        assert store.state.mode == "observe", f"mode 应恢复 observe，实为 {store.state.mode}"
    finally:
        _teardown(env)
