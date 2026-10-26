"""tests/test_wake.py — 唤醒词匹配与状态机单测（纯逻辑，不采音）。

覆盖：归一化 / 编辑距离 / 同音错字容错 / 唤醒→指令捕获 / 冷却 / 暂停丢弃。
真实链路验证（TTS 音频 → Vosk）见 scripts/verify_wake.py。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.types import WakeConfig
from voice.wake import (WakeListener, auto_tolerance, contains_wake,
                        edit_distance, normalize_text, rms_int16)

PHRASE = "小鲸鱼"


def make_listener(**over) -> WakeListener:
    cfg = WakeConfig(phrase=PHRASE, **over)
    return WakeListener(cfg)


def past_ignore_window(w: WakeListener) -> None:
    """真实场景里唤醒后要过 0.4s 才开始收指令；测试里手动越过。"""
    w._cmd_ignore_until = time.monotonic() - 1


def test_normalize() -> None:
    assert normalize_text(" 小鲸鱼，你好！ ") == "小鲸鱼你好"
    assert normalize_text("Hello, World") == "helloworld"
    assert normalize_text("") == ""
    print("PASS 归一化")


def test_edit_distance() -> None:
    assert edit_distance("", "") == 0
    assert edit_distance("abc", "abc") == 0
    assert edit_distance("abc", "abd") == 1
    assert edit_distance("abc", "ab") == 1
    assert edit_distance("abc", "xyz") == 3
    print("PASS 编辑距离")


def test_exact_match() -> None:
    assert contains_wake(PHRASE, "哎，小鲸鱼，你在吗")
    assert contains_wake(PHRASE, "小鲸鱼")
    assert not contains_wake(PHRASE, "今天天气不错")
    assert not contains_wake(PHRASE, "")
    print("PASS 精确匹配")


def test_homophone_tolerance() -> None:
    # Vosk 实测：会把"小鲸鱼"识别成"小金鱼"
    assert contains_wake(PHRASE, "小金鱼 今天 天气 怎么样")
    assert contains_wake(PHRASE, "小于鱼")
    assert contains_wake(PHRASE, "小鲸鱼儿")            # 增字
    # 不该把明显不同的词判成唤醒
    assert not contains_wake(PHRASE, "小鱼儿真可爱")
    assert not contains_wake(PHRASE, "小鱼")            # 2 字子串不算（见 contains_wake 注释）
    assert not contains_wake(PHRASE, "警察来了")
    print("PASS 同音错字容错（且不误触子串）")


def test_custom_phrase() -> None:
    assert contains_wake("小鲸鱼宝宝", "那个 小鲸鱼宝宝 在吗")
    assert auto_tolerance("小鲸鱼宝宝") == 2      # >4 字容 2 处
    assert auto_tolerance("鲸鱼") == 1
    print("PASS 自定义唤醒词")


def test_pinyin_short_phrase() -> None:
    """实测案例：2 字唤醒词「幼楚」被 Vosk 识别成「标出」，两个字都错，
    字符级编辑距离 2 超出容错上限——拼音音节级匹配堵住这类漏识别。"""
    assert contains_wake("幼楚", "有出")            # 同音不同字（最常见）
    assert contains_wake("幼楚", "标出")            # 实测误识别（探测脚本复现）
    assert contains_wake("幼楚", "鱿鱼")            # 首字同音
    assert contains_wake("幼楚", "幼楚啊")          # 唤醒词+语气词
    # 不该误触：两个音节都不同的日常词
    assert not contains_wake("幼楚", "你好")
    assert not contains_wake("幼楚", "今天天气不错")
    assert not contains_wake("幼楚", "出去")
    print("PASS 拼音音节级匹配（短唤醒词）")


def test_pinyin_three_chars() -> None:
    """3 字词：拼音级同样只容 1 个音节错，不会放宽到乱触发。"""
    assert contains_wake("小鲸鱼", "小金鱼")        # 字符级即可命中
    assert contains_wake("小鲸鱼", "小鲸于")        # 同音错字
    assert not contains_wake("小鲸鱼", "小鱼儿真可爱")   # 关键：仍不能误触
    assert not contains_wake("小鲸鱼", "小鱼")
    assert not contains_wake("小鲸鱼", "警察来了")
    print("PASS 拼音音节级匹配（3 字词不放松）")


def test_pinyin_degraded_fragments() -> None:
    """探测脚本真实片段序列：partial「有」→「标出」→ final「出」。"""
    w = WakeListener(WakeConfig(phrase="幼楚"))
    got: list[str] = []
    w.command.connect(got.append)
    assert w.feed("有", final=False) == []          # 单片段还判不出
    assert w.feed("标出", final=False) == ["woke"]   # 拼音级命中
    past_ignore_window(w)
    w.feed("讲个笑话", final=True)
    w._last_speech_at = time.monotonic() - 5
    w.tick_silence()
    assert got == ["讲个笑话"], got
    print("PASS 真实误识别片段序列触发唤醒")


def test_rms() -> None:
    import numpy as np
    assert rms_int16(np.zeros(100, dtype=np.int16)) == 0.0
    loud = np.full(100, 16000, dtype=np.int16)
    assert 0.4 < rms_int16(loud) < 0.6
    assert rms_int16(np.array([], dtype=np.int16)) == 0.0
    print("PASS RMS 能量")


def test_wake_then_command() -> None:
    w = make_listener()
    got: list[str] = []
    w.command.connect(got.append)
    assert w.feed("小金鱼", final=False) == ["woke"]
    past_ignore_window(w)
    assert w.state == WakeListener.COMMAND
    # 唤醒后继续说指令：partial 是渐进假设，final 覆盖整句
    w.feed("今天天气", final=False)
    w.feed("今天天气怎么样", final=False)
    w.feed("今天天气怎么样", final=True)
    assert w._cmd_finals == ["今天天气怎么样"], w._cmd_finals
    assert w._cmd_partial == "", repr(w._cmd_partial)
    # 接着说第二句 → 两条 final 累积
    w.feed("要不要出去玩", final=True)
    w._last_speech_at = time.monotonic() - 5
    w.tick_silence()
    assert got == ["今天天气怎么样要不要出去玩"], got
    assert w.state == WakeListener.IDLE
    print("PASS 唤醒后指令捕获（多句累积）")


def test_command_emitted_on_silence() -> None:
    w = make_listener()
    got: list[str] = []
    w.command.connect(got.append)
    w.feed("小鲸鱼", final=False)
    past_ignore_window(w)
    w.feed("给我讲个笑话", final=True)
    w._last_speech_at = time.monotonic() - 5      # 模拟早已说完
    w.tick_silence()
    assert got == ["给我讲个笑话"], got
    assert w.state == WakeListener.IDLE
    print("PASS 静默收尾发指令")


def test_no_command_when_silent() -> None:
    """唤醒后用户什么都没说 → 不发指令，只回 IDLE。"""
    w = make_listener()
    got: list[str] = []
    w.command.connect(got.append)
    w.feed("小鲸鱼", final=False)
    w._cmd_deadline = time.monotonic() - 1        # 已超时
    w.tick_silence()
    assert got == []
    assert w.state == WakeListener.IDLE
    print("PASS 空指令不发")


def test_cooldown_blocks_retrigger() -> None:
    w = make_listener(cooldown_sec=8.0)
    w.feed("小鲸鱼", final=False)
    w._finish_command_for_test()
    # 冷却期内再说唤醒词不应触发
    assert w.feed("小鲸鱼", final=False) == []
    print("PASS 冷却防连触")


def test_tail_noise_filtered() -> None:
    """唤醒词尾音与单字杂音不算指令。"""
    w = make_listener()
    got: list[str] = []
    w.command.connect(got.append)
    w.feed("小鲸鱼", final=False)          # 唤醒
    w.feed("鱼", final=True)               # 唤醒词尾音（0.4s 忽略窗内）
    w._last_speech_at = time.monotonic() - 5
    w.tick_silence()
    assert got == [], got
    # 忽略窗过后的一句单字仍不够 2 个有效字符
    w.feed("嗯", final=True)
    w._last_speech_at = time.monotonic() - 5
    w.tick_silence()
    assert got == [], got
    print("PASS 尾音/单字杂音过滤")


def test_pause_discards_command() -> None:
    """接管类暂停（会话/勿扰）：半个指令被丢弃，且不拾音。"""
    w = make_listener()
    w.feed("小鲸鱼", final=False)
    past_ignore_window(w)
    w.feed("今天天气", final=False)
    w.set_paused("session", True)
    assert w._is_paused()
    assert w.state == WakeListener.IDLE            # 已丢弃
    assert w._cmd_partial == "" and w._cmd_finals == []
    w.set_paused("session", False)
    assert not w._is_paused()
    print("PASS 接管类暂停丢弃指令")


def test_playback_pause_keeps_command() -> None:
    """播放类暂停（她自己发声）不能丢弃指令捕获。

    真实事故：唤醒后先播"我在，主人。"，播放暂停把指令窗口毁掉，
    用户每次唤醒后都得重叫一次名字。且暂停期间指令截止时间顺延，
    应答播多久都不吃掉用户本该有的思考时间。
    """
    import numpy as np

    class _FakeRec:
        def AcceptWaveform(self, pcm):
            return False

        def PartialResult(self):
            return '{"partial": ""}'

        def Result(self):
            return '{"text": ""}'

        def Reset(self):
            pass

    w = make_listener(silence_ms=3000, command_timeout_sec=10)
    w._rec = _FakeRec()
    silent = np.zeros(1600, dtype=np.int16)
    w.feed("小鲸鱼", final=False)
    past_ignore_window(w)
    w.feed("今天天气", final=False)                 # 说了一半
    deadline_before = w._cmd_deadline
    w.set_paused("speaking", True)                  # 应答开始播
    w._process(silent)                              # 暂停中的一帧
    w._paused_since -= 5.0                          # 模拟应答播了 5 秒（>silence_ms）
    w.set_paused("speaking", False)
    w._process(silent)                              # 恢复拾音的一帧
    assert not w._is_paused()
    assert w.state == WakeListener.COMMAND, "播放暂停不应丢弃指令"
    assert w._cmd_deadline > deadline_before, "暂停期间不应消耗指令截止时间"
    # 用户继续说完整句 → 正常收尾
    got: list[str] = []
    w.command.connect(got.append)
    w.feed("今天天气怎么样", final=True)
    w._last_speech_at = time.monotonic() - 5
    w.tick_silence()
    assert got == ["今天天气怎么样"], got
    print("PASS 播放类暂停保留指令且截止顺延")


def test_ack_pause_does_not_leak() -> None:
    """应答类语音播完后， speaking 暂停原因必须消失。

    真实事故：_on_player_finished 对应答语音只释放 "ack"，
    player.started 设的 "speaking" 永久泄漏 → 唤醒监听假死，
    用户反馈"聊过一次就再也喊不醒"。这里钉住释放逻辑。
    """
    w = make_listener()
    w.set_paused("speaking", True)      # player.started（任何语音都会设）
    w.set_paused("speaking", False)     # player.finished（必须无条件释放）
    assert "speaking" not in w._paused_reasons, w._paused_reasons
    print("PASS 播放暂停原因无泄漏")


def main() -> None:
    test_normalize()
    test_edit_distance()
    test_exact_match()
    test_homophone_tolerance()
    test_custom_phrase()
    test_pinyin_short_phrase()
    test_pinyin_three_chars()
    test_pinyin_degraded_fragments()
    test_rms()
    test_wake_then_command()
    test_command_emitted_on_silence()
    test_no_command_when_silent()
    test_cooldown_blocks_retrigger()
    test_tail_noise_filtered()
    test_pause_discards_command()
    test_playback_pause_keeps_command()
    test_ack_pause_does_not_leak()
    print("\n全部通过。")


# 供测试复用的内部入口（避免动到私有成员的语义）
def _finish_command_for_test(self) -> None:
    self._finish_command()


WakeListener._finish_command_for_test = _finish_command_for_test  # type: ignore[attr-defined]

if __name__ == "__main__":
    main()
