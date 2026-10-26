"""voice/router.py — VoiceRouter：MOSS-TTS-Nano 优先、SAPI 保底的统一发声接口。

对上层（observe/loop.py、chat/session.py）暴露与 MossNanoEngine 相同的
new_stop_event / stream / signal_stop / synthesize / loaded 接口：

  - MOSS 加载成功 → 走进程内 ONNX 合成，PCM 交给 SpeechPlayer 播；
  - MOSS 加载失败且 config.voice.fallback == "sapi" → 调 Windows SAPI5
    直接出声。此时 stream() 返回空迭代器（没有 PCM 可喂播放器），
    播放器拿到空任务会立即结束，不影响气泡与状态机。

单进程约束：SAPI 是进程内 COM 调用，不起服务、不开端口。
"""

from __future__ import annotations

import threading
from typing import Iterator

import numpy as np


class VoiceRouter:
    def __init__(self, primary, fallback: str = "sapi") -> None:
        self._primary = primary
        self._fallback_name = fallback
        self._fallback = None
        self._dummy_stop = threading.Event()

    # ---- 加载（主线程或后台线程调用一次） ----

    def load(self) -> None:
        try:
            self._primary.load()
        except Exception as exc:
            if self._fallback_name == "sapi":
                from voice.fallback_sapi import SapiVoice
                self._fallback = SapiVoice()
                print(f"[voice] MOSS-TTS-Nano 加载失败（{exc}），已降级 SAPI5 保底。")
            else:
                raise

    # ---- 状态 ----

    @property
    def loaded(self) -> bool:
        return self._primary.loaded

    @property
    def using_fallback(self) -> bool:
        return (not self._primary.loaded) and self._fallback is not None

    # ---- 音频格式（透传主引擎；播放器据此开流） ----

    @property
    def sample_rate(self) -> int:
        return int(getattr(self._primary, "sample_rate", 48000))

    @property
    def channels(self) -> int:
        return int(getattr(self._primary, "channels", 2))

    # ---- 与 MossNanoEngine 同形的接口 ----

    def new_stop_event(self) -> threading.Event:
        if self._primary.loaded:
            return self._primary.new_stop_event()
        return self._dummy_stop

    def _ensure_fallback(self) -> bool:
        """主引擎还没加载完时按需建 SAPI 保底（不阻塞等待加载）。"""
        if self._fallback is None and self._fallback_name == "sapi":
            try:
                from voice.fallback_sapi import SapiVoice
                self._fallback = SapiVoice()
            except Exception:  # noqa: BLE001 COM 不可用等情况
                return False
        return self._fallback is not None

    def synthesize(self, text: str, stop_event: threading.Event | None = None) -> np.ndarray:
        if self._primary.loaded:
            return self._primary.synthesize(text, stop_event)
        if self._ensure_fallback():
            self._fallback.speak(text)
            return np.zeros(1, dtype=np.float32)   # 无 PCM，占位
        raise RuntimeError("没有可用的 TTS 引擎（MOSS 未加载且无保底）")

    def stream(self, text: str, stop_event: threading.Event | None = None) -> Iterator[np.ndarray]:
        if self._primary.loaded:
            return self._primary.stream(text, stop_event)
        if self._ensure_fallback():
            self._fallback.speak(text)             # 异步，立即返回
            return iter(())                        # 空迭代器：播放器直接结束
        raise RuntimeError("没有可用的 TTS 引擎（MOSS 未加载且无保底）")

    def signal_stop(self) -> None:
        if self._primary.loaded:
            self._primary.signal_stop()
        elif self._fallback is not None:
            self._fallback.stop()

    def set_voice(self, name: str) -> None:
        """热切换音色（设置窗保存后立即生效，无需重启）。"""
        self._primary.voice_name = name

    def attach_cache(self, cache) -> None:
        """把短句 PCM 缓存挂到主引擎（缓存按音色隔离，换音色要重挂）。

        用 getattr 兜底：主引擎若不带缓存能力（比如将来换成别的 TTS），
        静默跳过即可，不能让预热把整个发声链路带崩。
        """
        attach = getattr(self._primary, "attach_cache", None)
        if attach is not None:
            attach(cache)

    def warm(self, texts: list[str]) -> int:
        """预热短句 PCM。本机合成只有 ~0.7x 实时，预热后唤醒应答 0 延迟。"""
        warm = getattr(self._primary, "warm", None)
        if warm is None:
            return 0
        return int(warm(texts))
