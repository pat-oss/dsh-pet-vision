"""voice/cache.py — 短句 PCM 缓存与分句（与具体 TTS 引擎无关）。

UtteranceCache：内存 + 磁盘 npz，按「引擎+音色」隔离。迎接"响应越快越好"
与"本机合成慢于实时"的矛盾：固定短句启动时预热一次，之后命中缓存直接出声，
不再等合成。voice 身份串带引擎名前缀，换引擎（如 Audio8→MOSS）后旧缓存
自动作废，不会把 44.1kHz 单声道旧数据当 48kHz 立体声播。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator

import numpy as np


class UtteranceCache:
    """常用短句 PCM 缓存（内存 + 磁盘 npz），按音色隔离。"""

    def __init__(self, path: Path | None, voice: str) -> None:
        self.path = Path(path) if path else None
        self.voice = voice
        self._mem: dict[str, np.ndarray] = {}
        self._dirty = False

    def load(self) -> None:
        """磁盘缓存仅在音色一致时采用（换音色后旧音频作废）。"""
        if not self.path or not self.path.is_file():
            return
        try:
            with np.load(self.path, allow_pickle=False) as data:
                if str(np.asarray(data["voice"]).item()) != self.voice:
                    return
                texts = [str(t) for t in np.asarray(data["texts"])]
                for i, text in enumerate(texts):
                    self._mem[text] = np.asarray(data[f"a{i}"], dtype=np.float32)
        except Exception:  # noqa: BLE001 缓存损坏不影响发声
            self._mem = {}

    def get(self, text: str) -> np.ndarray | None:
        return self._mem.get(text)

    def put(self, text: str, pcm: np.ndarray) -> None:
        self._mem[text] = np.asarray(pcm, dtype=np.float32)
        self._dirty = True

    def save(self) -> None:
        if not self.path or not self._dirty or not self._mem:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 坑：np.savez_compressed 会给「不以 .npz 结尾」的文件名自动追加
            # .npz。临时名若写成 *.tmp，实际落盘是 *.tmp.npz，随后
            # os.replace(tmp, ...) 找不到文件 → 抛错被吞 → 缓存永远存不下，
            # 每次开机都要重新预热十几秒。所以临时名必须以 .npz 收尾。
            tmp = self.path.with_name(self.path.name + ".tmp.npz")
            payload: dict[str, object] = {
                "voice": np.array(self.voice),
                "texts": np.array(list(self._mem.keys())),
            }
            for i, pcm in enumerate(self._mem.values()):
                payload[f"a{i}"] = np.asarray(pcm, dtype=np.float32)
            np.savez_compressed(tmp, **payload)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception as exc:  # noqa: BLE001 缓存写失败不该影响发声
            print(f"[warn] TTS 缓存保存失败: {exc}")


def split_sentences(text: str, max_chars: int = 32) -> list[str]:
    """按标点切分为短句（MOSS 官方建议单句 ≤150 字符；本工程按 32 字控延迟）。

    分句播放的意义：合成慢于实时（RTF ~0.7x）时，第一句合成完就能开播，
    不必等整段说完；句间插 0.2s 静音防止粘字。
    """
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;，,、\n])", text)
    parts = [p for p in parts if p]
    chunks: list[str] = []
    current = ""
    for part in parts:
        if len(current) + len(part) <= max_chars:
            current += part
        else:
            if current:
                chunks.append(current)
            while len(part) > max_chars:  # 超长无标点句硬切
                chunks.append(part[:max_chars])
                part = part[max_chars:]
            current = part
    if current:
        chunks.append(current)
    return chunks


def concat_with_pause(chunks: list[np.ndarray], pause_samples: int,
                      channels: int) -> np.ndarray:
    """把多段 PCM 拼成一整段，段间补 pause_samples 个静音帧。"""
    if not chunks:
        return np.zeros((0, channels), dtype=np.float32)
    if len(chunks) == 1:
        return chunks[0]
    pause = np.zeros((pause_samples, channels), dtype=np.float32)
    out: list[np.ndarray] = []
    for i, chunk in enumerate(chunks):
        out.append(chunk)
        if i < len(chunks) - 1:
            out.append(pause)
    return np.concatenate(out, axis=0)
