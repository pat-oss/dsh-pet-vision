"""最小桩：只提供 WeTextProcessingManager 与 prepare_tts_request_texts。

官方版依赖 pynini/WeTextProcessing（Windows 上安装困难）。我们的文本是
人设生成的短中文句，本身已规范，所以这里只做去空白 + 全角转半角。
enable_wetext=False 时 WeTextProcessingManager 根本不会被实例化。
"""
from __future__ import annotations

import re
import unicodedata


class _Snapshot:
    def __init__(self) -> None:
        self.ready = False
        self.error = "WeTextProcessing 未安装（本工程不使用该后端）"
        self.message = self.error


class WeTextProcessingManager:
    def __init__(self, *args, **kwargs) -> None:
        self._snapshot = _Snapshot()

    def ensure_ready(self):
        return self._snapshot


def _simple_normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"\s+", "", text)
    return text.strip()


def prepare_tts_request_texts(*, text, prompt_text: str = "", voice: str = "",
                              enable_wetext: bool = False,
                              enable_normalize_tts_text: bool = True,
                              text_normalizer_manager=None, **kwargs) -> dict:
    body = str(text or "")
    if enable_normalize_tts_text:
        body = _simple_normalize(body)
    return {
        "text": body,
        "prompt_text": _simple_normalize(prompt_text) if prompt_text else "",
        "voice": str(voice or ""),
    }
