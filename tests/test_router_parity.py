"""tests/test_router_parity.py — VoiceRouter 必须与主引擎接口同形。

背景（真 bug）：给 MossNanoEngine（当时的 Audio8Engine）加了
attach_cache/warm 用于短句预热，但上层拿到的是 VoiceRouter，它没转发
这两个方法，于是预热抛异常、被 try/except 吞掉，唤醒后仍然要等 5 秒
才出声——症状看起来像"音频卡"，实际是静默失效。

所以这里钉住两件事：
  1. Router 必须暴露约定的全部方法（加引擎方法时别漏转发）；
  2. 这些方法必须**真的转发**到主引擎，而不只是 hasattr 存在。
"""

from __future__ import annotations

import numpy as np

from voice.router import VoiceRouter

REQUIRED_API = (
    "load",
    "synthesize",
    "stream",
    "new_stop_event",
    "signal_stop",
    "set_voice",
    "attach_cache",  # 短句 PCM 缓存（换音色要重挂）
    "warm",          # 启动预热，决定唤醒应答是不是 0 延迟
)


class _FakePrimary:
    """只记录调用，不真的加载 ONNX。"""

    def __init__(self) -> None:
        self.loaded = True
        self.voice_name = "old"
        self.cache = None
        self.warmed = None
        self.stopped = False
        self.sample_rate = 48000
        self.channels = 2

    def load(self) -> None:
        self.loaded = True

    def synthesize(self, text, stop_event=None):
        return np.zeros(1600, dtype=np.float32)

    def stream(self, text, stop_event=None):
        return iter([np.zeros(1600, dtype=np.float32)])

    def new_stop_event(self):
        return object()

    def signal_stop(self):
        self.stopped = True

    def attach_cache(self, cache) -> None:
        self.cache = cache

    def warm(self, texts) -> int:
        self.warmed = list(texts)
        return len(texts)


def test_router_exposes_required_api() -> None:
    for name in REQUIRED_API:
        assert hasattr(VoiceRouter, name), (
            f"VoiceRouter 缺少接口 {name}——上层拿的是 Router，"
            f"新加到 MossNanoEngine 的方法必须同步转发"
        )


def test_router_forwards_cache_calls_to_primary() -> None:
    primary = _FakePrimary()
    router = VoiceRouter(primary, fallback="none")

    router.attach_cache("CACHE-OBJ")
    assert primary.cache == "CACHE-OBJ"

    assert router.warm(["我在，主人。", "来啦来啦"]) == 2
    assert primary.warmed == ["我在，主人。", "来啦来啦"]


def test_router_warm_is_noop_when_primary_lacks_cache() -> None:
    """主引擎不带缓存能力时静默跳过，不能让预热带崩发声链路。"""

    class Bare:
        loaded = True
        voice_name = "x"

        def load(self) -> None:
            pass

    router = VoiceRouter(Bare(), fallback="none")
    router.attach_cache("CACHE")          # 不应抛异常
    assert router.warm(["a"]) == 0


def test_router_passes_through_audio_format() -> None:
    """播放器按 Router 暴露的采样率/声道数开流；换引擎时不能静默给错值
    （MOSS 48kHz 立体声 vs 旧 Audio8 44.1kHz 单声道，搞错=变声+卡顿）。"""
    primary = _FakePrimary()
    router = VoiceRouter(primary, fallback="none")
    assert (router.sample_rate, router.channels) == (48000, 2)
