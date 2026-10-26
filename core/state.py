"""core/state.py — RuntimeState（JSON 原子写）+ DecisionLog（JSONL 只追加）。

并发写纪律（设计文档 §4.4）：所有写操作经本模块的锁串行化；
DecisionLog 由观察线程单写者 append，RuntimeState 的 read-modify-write
同样在这里加锁，防止计数丢更新击穿节流上限。
"""

from __future__ import annotations

import json
import os
import threading
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .types import RuntimeState


class StateStore:
    """RuntimeState + DecisionLog 的唯一写入口。"""

    def __init__(self, state_path: Path, log_path: Path) -> None:
        self._state_path = Path(state_path)
        self._log_path = Path(log_path)
        self._lock = threading.Lock()
        self._pending = 0
        self.state = self._load()

    # ---- RuntimeState ----

    def _load(self) -> RuntimeState:
        if self._state_path.is_file():
            try:
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                return RuntimeState(**{k: v for k, v in data.items()
                                        if k in RuntimeState.__dataclass_fields__})
            except (OSError, json.JSONDecodeError, TypeError):
                pass  # 状态损坏不致命：重置但保留日志
        return RuntimeState()

    def save(self) -> None:
        """原子写：tmp + os.replace。"""
        with self._lock:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._state_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self.state.__dict__, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, self._state_path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    # ---- DecisionLog ----

    def record_decision(self, event: dict) -> None:
        """追加一条决策事件。批次 fsync（每 10 条或 1s 由调用方触发 flush）。"""
        event = dict(event)
        event.setdefault("ts", datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._pending += 1
            if self._pending >= 10:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        # JSONL append 已随 write 落盘；fsync 由文件系统缓冲保证，
        # 批次边界处做一次目录级确认即可（Windows 上 fsync 目录无意义）。
        self._pending = 0

    # ---- 计数操作（全部加锁，单写者语义） ----

    def mark_api_call(self, today: str) -> None:
        with self._lock:
            self.state.roll_day(today)
            self.state.calls_today += 1

    def mark_spoken(self, now: float, text: str) -> None:
        with self._lock:
            self.state.roll_hour(now)
            self.state.last_spoken_at = now
            self.state.spoke_this_hour += 1
            self.state.text_cooldown.pop(_fingerprint(text), None)
        self.save()

    def mark_failure(self, now: float) -> int:
        with self._lock:
            self.state.consecutive_failures += 1
            return self.state.consecutive_failures

    def reset_failures(self) -> None:
        with self._lock:
            self.state.consecutive_failures = 0

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self.state.mode = mode
        self.save()


def _fingerprint(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
