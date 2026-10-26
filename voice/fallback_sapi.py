"""voice/fallback_sapi.py — SAPI5 保底发声（MOSS-TTS-Nano 不可用时降级）。

用 pywin32 调 Windows 内置语音，音质差但零额外依赖、断网可用。
SAPI.SpVoice 是线程亲和的 COM 对象：本类按调用线程懒创建并缓存实例，
每个新线程首次说话前 CoInitialize，避免跨线程 Dispatch 失败。
speak 为异步（不等说完），由 VoiceRouter 决定何时调用。
"""

from __future__ import annotations

import threading


class SapiVoice:
    def __init__(self, rate: int = 0) -> None:
        self._rate = rate          # -10..10，0 = 默认语速
        self._local = threading.local()

    def _voice(self):
        """当前线程的 SpVoice 实例（线程本地，首次用时初始化 COM）。"""
        voice = getattr(self._local, "voice", None)
        if voice is None:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Rate = self._rate
            self._local.voice = voice
        return voice

    def speak(self, text: str) -> None:
        if not text or not text.strip():
            return
        self._voice().Speak(text, 1)      # SVSFlagsAsync=1：异步，立即返回

    def stop(self) -> None:
        voice = getattr(self._local, "voice", None)
        if voice is not None:
            voice.Speak("", 2)            # SVSFPurgeBeforeSpeak=2：立即停止
