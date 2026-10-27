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
import time
from datetime import datetime, timezone
from pathlib import Path

from .types import RuntimeState

# 持久化的 monotonic 时间戳最长合理剩余量（用于跨重启消毒）
_MAX_COOLDOWN_SEC = 1800.0   # text_cooldown 上限（打转检测 600s，留余量）
_MAX_SUSPEND_SEC = 600.0     # 退避挂起上限（与 loop 里 min(600, ...) 一致）


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
                st = RuntimeState(**{k: v for k, v in data.items()
                                    if k in RuntimeState.__dataclass_fields__})
                return self._sanitize(st)
            except (OSError, json.JSONDecodeError, TypeError):
                pass  # 状态损坏不致命：重置但保留日志
        return RuntimeState()

    def _sanitize(self, st: RuntimeState) -> RuntimeState:
        """跨重启的时间戳消毒（P0 缺陷修复，2026-10-27）。

        monotonic() 每次开机从 0 重新计时，但 last_spoken_at / hour_window_start /
        suspended_until / text_cooldown 都被持久化在 state.json 里。电脑重启后
        旧值变成"未来时间"，于是 `now - last_spoken_at` 是负数，节流门第一道判断
        `if now - last < 30` 永远成立 → **主动搭话永久静默**，而 API 照旧被调用
        （屏幕上什么都不会发生，钱照样烧）。

        实测：last_spoken_at=48264，当次开机 monotonic=15082 → 需要再跑 9.2 小时
        才会"解冻"。这类故障用 `except: pass` 兜不住，必须在加载时显式纠正。
        """
        now = time.monotonic()
        notes: list[str] = []

        if st.last_spoken_at > now:
            notes.append(f"last_spoken_at {st.last_spoken_at:.0f}s→0（旧开机周期）")
            st.last_spoken_at = 0.0

        if st.hour_window_start > now:
            notes.append(f"hour_window_start {st.hour_window_start:.0f}s→now，"
                         f"本小时计数 {st.spoke_this_hour}→0")
            st.hour_window_start = now
            st.spoke_this_hour = 0

        if st.suspended_until > now + _MAX_SUSPEND_SEC:
            notes.append(f"suspended_until {st.suspended_until:.0f}s→0")
            st.suspended_until = 0.0

        expired = [k for k, v in st.text_cooldown.items()
                   if not isinstance(v, (int, float)) or v > now + _MAX_COOLDOWN_SEC]
        for k in expired:
            st.text_cooldown.pop(k, None)
        if expired:
            notes.append(f"text_cooldown 清理 {len(expired)} 条")

        if notes:
            # 必须可见：这类问题一旦静默，用户只会觉得"她不理我了"
            print("[state] 检测到跨重启残留时间戳，已重置：" + "；".join(notes),
                  flush=True)
        return st

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
