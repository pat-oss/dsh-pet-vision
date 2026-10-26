"""tests/test_moss_retry.py — MOSS 短文本采样跑偏的换种子 + 超时重试。

背景（实测）：vendor 的 OnnxTtsRuntime 固定 rng 种子 1234，且默认
sample_mode="fixed" 走 ONNX 图内采样（温度等常量烘焙在图里，Python 侧
改不了）。后果有两个：
  1. 每次引擎加载后第一句合成必然是同一个跑偏样本（必现陷阱）——
     实测「我在，主人。」首次合成产出 4.9s 的"我在主人这，主人这…"；
  2. 换句不换种子仍有 ~1/8 概率跑偏。
修复：_synth_one 每次合成都重新播种 rng，超过 _duration_cap 判定跑偏
并重试（实测一次过 8/10、重试救回 2/2）。

这里用假 runtime 钉住四件事：换种子、超限重试、重试收敛、stop 优先。
"""

from __future__ import annotations

import threading

import numpy as np

from voice.moss import MAX_SYNTH_RETRIES, MossNanoEngine, _duration_cap, _ensure_eos


class _FakeRuntime:
    """记录合成调用与 rng 赋值；按脚本依次返回不同时长的波形。"""

    def __init__(self, durations_sec: list[float], sample_rate: int = 48000):
        self.durations_sec = list(durations_sec)
        self.sample_rate = sample_rate
        self.calls = 0
        self.rng_count = 0
        object.__setattr__(self, "rng", None)

    def __setattr__(self, name: str, value) -> None:
        if name == "rng":
            self.__dict__["rng_count"] = self.__dict__.get("rng_count", 0) + 1
        object.__setattr__(self, name, value)

    def prepare_synthesis_text(self, text: str, enable_wetext: bool = False) -> dict:
        return {"text": text}

    def synthesize_single_chunk(self, text: str, prompt_audio_codes, streaming: bool) -> dict:
        idx = min(self.calls, len(self.durations_sec) - 1)
        self.calls += 1
        frames = int(self.durations_sec[idx] * self.sample_rate)
        # 均匀大幅信号：tighten_silence 不会裁掉任何样本
        t = np.arange(frames, dtype=np.float32) / self.sample_rate
        wave = 0.5 * np.sin(2 * np.pi * 220.0 * t)
        return {"waveform": np.stack([wave, wave], axis=1)}


def _engine(fake: _FakeRuntime) -> MossNanoEngine:
    engine = MossNanoEngine.__new__(MossNanoEngine)
    engine._runtime = fake
    engine._codes = [[1, 2, 3]]
    engine._stop_event = None
    return engine


def test_first_attempt_within_cap_no_retry() -> None:
    fake = _FakeRuntime([2.0])
    wave = _engine(fake)._synth_one("我在，主人。", None)
    assert fake.calls == 1, "时长正常时不应重试"
    assert abs(wave.shape[0] / 48000 - 2.0) < 1e-3


def test_over_cap_triggers_retry_until_converged() -> None:
    # 第一次 4.9s（跑偏），第二次 1.3s（正常）
    fake = _FakeRuntime([4.9, 1.3])
    wave = _engine(fake)._synth_one("我在，主人。", None)
    assert fake.calls == 2, "超限必须重试"
    assert abs(wave.shape[0] / 48000 - 1.3) < 1e-3, "应返回重试后的正常样本"


def test_all_attempts_over_cap_returns_fallback_not_raise() -> None:
    fake = _FakeRuntime([5.0])
    wave = _engine(fake)._synth_one("我在，主人。", None)
    assert fake.calls == MAX_SYNTH_RETRIES + 1
    assert wave.size > 0, "全部超限时交最后一个样本，不应抛异常也不交空"


def test_stop_event_short_circuits() -> None:
    fake = _FakeRuntime([1.0])
    stop = threading.Event()
    stop.set()
    wave = _engine(fake)._synth_one("我在，主人。", stop)
    assert fake.calls == 0, "stop 已设置时不应合成"
    assert wave.shape == (0, 2)


def test_rng_reseeded_each_attempt() -> None:
    """每次合成都必须换种子——固定种子 1234 下首句必跑偏（必现陷阱）。"""
    fake = _FakeRuntime([4.9, 4.9, 1.2])
    engine = _engine(fake)
    engine._synth_one("我在，主人。", None)
    # 三次尝试都应发生（前两次超限都走了换种子重试路径），
    # 且每次合成前都重新赋过 rng
    assert fake.calls == 3
    assert fake.rng_count == 3, "每次合成前必须重新播种 rng"


def test_duration_cap_scales_with_length() -> None:
    assert _duration_cap("我在") == 2.2, "短句保底 2.2s"
    cap6 = _duration_cap("我在，主人。")   # 6 字符（含标点）
    assert 2.7 < cap6 < 2.9, f"6 字符上限应约 2.8s，实际 {cap6}"
    cap32 = _duration_cap("啊" * 32)
    assert 10.4 < cap32 < 10.8, f"32 字上限应约 10.6s，实际 {cap32}"
    # 跑偏样本实测最低 3.2s，必须被 6 字符句的上限抓到
    assert _duration_cap("我在，主人。") < 3.2


def test_ensure_eos_appends_period() -> None:
    """无标点短文本系统性跑偏（实测「好的」9 尝试 7 挂），必须补句号。"""
    assert _ensure_eos("好的") == "好的。"
    assert _ensure_eos("小鲸鱼") == "小鲸鱼。"


def test_ensure_eos_replaces_comma_tail() -> None:
    """逗号收尾语义上"没说完"，同样锚不住 EOS（实测首次跑偏率 4/6）。"""
    assert _ensure_eos("好的，") == "好的。"
    assert _ensure_eos("第一、") == "第一。"


def test_ensure_eos_keeps_real_punctuation() -> None:
    for text in ("我在，主人。", "嗯？主人叫我吗～", "来啦来啦，主人想说什么？",
                 "太棒了！", "原来如此…", " Really?", ""):
        assert _ensure_eos(text) == text, f"「{text}」不应被改写"
