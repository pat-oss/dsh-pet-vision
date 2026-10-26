"""voice/player.py — SpeechPlayer：sounddevice 独立线程播放 + 队列 + 即时打断。

QObject 移入 QThread；支持两种任务：流式块迭代（首音延迟最低）与整段 PCM。
utterance_id 幂等：同一 utterance 不重复播放。stop_all() 停播+清队列。

采样率/声道数由构造参数决定（TTS 引擎的音频格式，当前 MOSS 为 48kHz
立体声）：播放器不做重采样，引擎给什么格式就按什么开流，保音质。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Iterator

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, Signal, QThread


class SpeechPlayer(QObject):
    started = Signal(str)                 # utt_id
    first_audio = Signal(str, int)        # utt_id, 从入队到首音的毫秒数
    finished = Signal(str)                # utt_id（播完或被中断）
    all_stopped = Signal()

    def __init__(self, sample_rate: int = 48000, channels: int = 2) -> None:
        super().__init__()
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._stream: sd.OutputStream | None = None
        self._played: set[str] = set()
        self._current: str | None = None
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)

    # ---- 任务投递（线程安全） ----

    def enqueue_stream(self, utt_id: str, chunks: Iterator[np.ndarray]) -> None:
        self._queue.put(("stream", utt_id, chunks))

    def enqueue_pcm(self, utt_id: str, pcm: np.ndarray) -> None:
        self._queue.put(("pcm", utt_id, pcm))

    def stop_all(self) -> None:
        """立即停播 + 清空未播队列。已播音频不可收回（副作用清单见设计文档）。"""
        self._stop.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception:
                pass
        if self._current:
            self.finished.emit(self._current)
            self._current = None
        self.all_stopped.emit()

    # ---- 播放线程主循环 ----

    def run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:  # 毒丸：退出
                return
            kind, utt_id, payload = job
            if utt_id in self._played:
                continue
            self._played.add(utt_id)
            if len(self._played) > 500:
                self._played = set(list(self._played)[-100:])
            self._stop.clear()
            self._current = utt_id
            enqueued_at = time.monotonic()
            self.started.emit(utt_id)
            try:
                self._stream = sd.OutputStream(samplerate=self.sample_rate,
                                               channels=self.channels,
                                               dtype="float32")
                self._stream.start()
                first = True
                if kind == "pcm":
                    chunks = [payload]
                else:
                    chunks = payload  # 迭代器：边合成边播
                for chunk in chunks:
                    if self._stop.is_set():
                        break
                    if chunk.size == 0:
                        continue
                    self._stream.write(chunk)
                    if first:
                        first = False
                        latency_ms = int((time.monotonic() - enqueued_at) * 1000)
                        self.first_audio.emit(utt_id, latency_ms)
                if not self._stop.is_set():
                    self._stream.stop()
            except Exception as exc:  # 播放失败不崩进程，记 finished 让上层知道
                print(f"[player] 播放失败: {exc}")
            finally:
                if self._stream is not None:
                    try:
                        self._stream.close()
                    except Exception:
                        pass
                    self._stream = None
                if not self._stop.is_set():
                    self.finished.emit(utt_id)
                self._current = None

    def shutdown(self) -> None:
        self._queue.put(None)
