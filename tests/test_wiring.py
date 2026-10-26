"""tests/test_wiring.py — PetApp._wire() 信号接线回归测试（offscreen）。

背景 bug：player.started 是 Signal(str)（只发 utt_id），而 main.py 里的
lambda 写了两个形参（utt_id, _ms）。后果：① 每次宠物开口都抛 TypeError；
② "播放中暂停唤醒拾音"的防自触发逻辑静默失效——她能听到自己的声音。
这类"参数个数不匹配"只在真机发信号时才暴露，单测各模块抓不到，
所以在这里端到端地把 _wire 接起来再逐个发信号。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _build_app(tmp_dir: Path):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from core.types import PetConfig
    from main import PetApp
    from pet.app import PetWindow
    args = SimpleNamespace(config=Path(__file__).resolve().parents[1] / "config.yaml",
                           setup=False, self_test=False)
    pet = PetApp(args)
    # _wire 只需要这些对象；loop/channel 置空（代码内有 None 保护）
    pet.window = PetWindow(Path(__file__).resolve().parents[1] / "pet" / "assets" / "frames",
                           PetConfig())
    pet.loop = None
    pet.channel = None
    return pet, app


def test_player_signals_do_not_raise(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LITTLE_WHALE_DATA_DIR", str(tmp_path))
    pet, _app = _build_app(tmp_path)

    from voice.player import SpeechPlayer

    # 不搬线程：真实应用里 started/first_audio 由播放线程自己发（同线程直连），
    # 这里保持同线程直连语义；搬了线程再从主线程发会变队列投递，测不到。
    pet.player = SpeechPlayer()
    pet._wire()
    # 逐个发信号：参数个数不匹配会在这里抛 TypeError
    pet.player.started.emit("utt-1")
    pet.player.first_audio.emit("utt-1", 120)
    pet.player.finished.emit("utt-1")
    pet.window.user_text_submitted.emit("你好")     # channel=None → 只冒泡
    pet.window.act_dnd.toggled.emit(True)           # 勿扰切换
    pet.window.act_dnd.toggled.emit(False)
    print("PASS 播放/输入/勿扰信号接线无异常")


def test_speaking_pause_actually_applies(tmp_path, monkeypatch) -> None:
    """started 命中后 wake 暂停原因必须真的加上（防自触发的核心）。"""
    monkeypatch.setenv("LITTLE_WHALE_DATA_DIR", str(tmp_path))
    pet, _app = _build_app(tmp_path)

    from voice.player import SpeechPlayer
    from voice.wake import WakeListener
    from core.types import WakeConfig

    pet.player = SpeechPlayer()
    wake = WakeListener(WakeConfig(phrase="幼楚"))
    pet.wake = wake
    pet._wire()
    pet.player.started.emit("utt-2")
    assert "speaking" in wake._paused_reasons, wake._paused_reasons
    pet.player.finished.emit("utt-2")
    assert "speaking" not in wake._paused_reasons
    print("PASS 播放中确实暂停了唤醒拾音")


def test_ack_playback_does_not_leak_pause(tmp_path, monkeypatch) -> None:
    """唤醒应答语音播完后暂停原因必须清零。

    真实事故：_on_player_finished 曾对 ack 类语音只释放 "ack"，
    漏放 player.started 设的 "speaking"——泄漏一个原因 = 唤醒监听
    永久假死。用户原话："第一次叫有反应，之后再叫就没反应"。
    """
    monkeypatch.setenv("LITTLE_WHALE_DATA_DIR", str(tmp_path))
    pet, _app = _build_app(tmp_path)

    from voice.player import SpeechPlayer
    from voice.wake import WakeListener
    from core.types import WakeConfig

    pet.player = SpeechPlayer()
    wake = WakeListener(WakeConfig(phrase="幼楚"))
    pet.wake = wake
    pet._wire()
    # ack- 前缀的 utt_id 走的是同一条 started/finished 通道
    pet.player.started.emit("ack-1234-567")
    assert "speaking" in wake._paused_reasons
    pet.player.finished.emit("ack-1234-567")
    assert wake._paused_reasons == set(), (
        f"应答播完仍有暂停原因泄漏：{wake._paused_reasons}——唤醒将永久假死")
    print("PASS 应答语音播完无暂停泄漏")


def main() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_player_signals_do_not_raise(Path(d), _NoopMonkey())
        with tempfile.TemporaryDirectory() as d2:
            test_speaking_pause_actually_applies(Path(d2), _NoopMonkey())
        with tempfile.TemporaryDirectory() as d3:
            test_ack_playback_does_not_leak_pause(Path(d3), _NoopMonkey())
    print("\n全部通过。")


class _NoopMonkey:
    def setenv(self, k, v):
        os.environ[k] = v


if __name__ == "__main__":
    main()
