"""core/usage.py — API 用量与效果统计（持久化，分桶聚合）。

统计口径：
- 桶共四层，一次记账同时落到当前**小时 / 天 / 月 / 总计**；
- **本次启动以来**单独用内存计数，进程退出即归零（不写盘，避免把"上次开机"
  的数字当成"这次"——这正是 2026-10-27 那次 P0 事故的教训：单调/会话边界混在一起）。

线程安全：观察线程、会话线程、UI 线程都会调 record，全部走同一把锁。
落盘节流：每 _SAVE_INTERVAL_SEC 秒最多写一次，退出前调 flush() 兜住最后一段。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

_SAVE_INTERVAL_SEC = 10.0
_MAX_BUCKETS = 4000          # 防御：桶无限增长（每小时一个，够用 5 个月）

_LABELS = (
    ("session", "本次启动"),
    ("hour", "本小时"),
    ("day", "今天"),
    ("month", "本月"),
    ("all", "总计"),
)


def format_number(value: int) -> str:
    return f"{value:,}"


def format_snapshot(snapshot: dict[str, dict]) -> str:
    """渲染成给人看的几行文本（设置窗与点击弹窗共用）。

    口径说明（界面上也照这个写，避免"命中率"含糊其辞）：
    - token = 输入 + 输出；
    - 缓存命中率 = 命中的输入 token / 输入 token，反映提示词前缀是否稳定；
    - 成功率 = 成功调用 / (成功 + 失败)。
    """
    lines: list[str] = []
    for key, label in _LABELS:
        row = snapshot.get(key)
        if row is None:
            continue
        hit = row.get("cache_hit_rate", 0.0)
        tries = row.get("calls", 0) + row.get("failed", 0)
        ok = row.get("calls", 0) / tries if tries else 1.0
        lines.append(
            f"{label}　{row.get('calls', 0)} 次调用 · {format_number(row.get('total', 0))} token"
            f"　（缓存命中 {hit:.0%} · 成功 {ok:.0%}）"
        )
    return "\n".join(lines)


@dataclass
class Counter:
    """一组计数。token 单位按官方 usage 原样累加，不做估算。"""

    calls: int = 0            # 成功返回 usage 的调用次数
    failed: int = 0           # 失败调用次数（拿不到 usage，不计 calls）
    prompt: int = 0           # 输入 token
    completion: int = 0       # 输出 token
    cached: int = 0           # 其中命中上下文缓存的输入 token

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    @property
    def cache_hit_rate(self) -> float:
        """输入 token 的缓存命中率。

        DeepSeek 对命中上下文缓存的输入部分按更低价计费，所以这个数直接影响花销，
        也间接说明提示词前缀是否稳定（前缀一变就命中不了）。
        """
        return self.cached / self.prompt if self.prompt else 0.0

    @property
    def failure_rate(self) -> float:
        total_try = self.calls + self.failed
        return self.failed / total_try if total_try else 0.0

    def row(self) -> dict:
        """给 UI 用的一行数据（含派生比率，避免界面自己再算一遍）。"""
        return {
            "calls": self.calls,
            "failed": self.failed,
            "prompt": self.prompt,
            "completion": self.completion,
            "cached": self.cached,
            "total": self.total,
            "cache_hit_rate": self.cache_hit_rate,
            "failure_rate": self.failure_rate,
        }


def _buckets(now: datetime) -> list[str]:
    """一次记账要落的四个桶 key（同一时刻，保证小时/天/月口径一致）。"""
    return [now.strftime("%Y-%m-%dT%H"), now.strftime("%Y-%m-%d"),
            now.strftime("%Y-%m"), "all"]


class UsageStore:
    """用量统计的唯一写入口（对齐 StateStore 的并发写纪律）。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self.buckets: dict[str, Counter] = self._load()
        self.session = Counter()          # 本次启动以来，不落盘
        self._last_saved = 0.0
        self._dirty = False

    # ---- 读取 ----

    def snapshot(self) -> dict[str, dict]:
        """返回 UI 需要的全部数据：hour / day / month / all / session。"""
        with self._lock:
            now = datetime.now()
            keys = _buckets(now)
            out = {"session": self.session.row()}
            for label, key in zip(("hour", "day", "month", "all"), keys):
                out[label] = self.buckets.get(key, Counter()).row()
            return out

    # ---- 记账 ----

    def record(self, prompt: int = 0, completion: int = 0, cached: int = 0) -> None:
        """一次成功调用。参数缺失时用 0，避免脏数据把比率算歪。"""
        with self._lock:
            self._bump(_buckets(datetime.now()), prompt, completion, cached)
            self.session.calls += 1
            self.session.prompt += prompt
            self.session.completion += completion
            self.session.cached += cached
            self._maybe_save_locked()

    def record_failure(self) -> None:
        """调用失败：没有 usage，只累失败次数。"""
        with self._lock:
            now = datetime.now()
            for key in _buckets(now):
                self.buckets.setdefault(key, Counter()).failed += 1
            self.session.failed += 1
            self._maybe_save_locked()

    # ---- 维护 ----

    def reset_all(self) -> None:
        """清空全部统计（含本次启动以来）。"""
        with self._lock:
            self.buckets = {}
            self.session = Counter()
            self._write_locked()

    def flush(self) -> None:
        """落盘兜底（退出前调用），避免节流窗口内的新数据丢掉。"""
        with self._lock:
            if self._dirty:
                self._write_locked()

    # ---- 内部 ----

    def _bump(self, keys: list[str], prompt: int, completion: int, cached: int) -> None:
        for key in keys:
            counter = self.buckets.setdefault(key, Counter())
            counter.calls += 1
            counter.prompt += prompt
            counter.completion += completion
            counter.cached += cached
        if len(self.buckets) > _MAX_BUCKETS:
            self._prune_locked()

    def _prune_locked(self) -> None:
        """只保留最近的小时桶；天/月/总计桶永远保留。"""
        hours = sorted((k for k in self.buckets if len(k) == len("2026-10-31T23")),
                       reverse=True)
        for key in hours[24:]:
            self.buckets.pop(key, None)

    def _maybe_save_locked(self) -> None:
        self._dirty = True
        now = time.monotonic()
        if now - self._last_saved >= _SAVE_INTERVAL_SEC:
            self._write_locked()
            self._last_saved = now

    def _load(self) -> dict[str, Counter]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw = data.get("buckets", {}) if isinstance(data, dict) else data
            out: dict[str, Counter] = {}
            for key, value in raw.items():
                try:
                    out[key] = Counter(**{k: v for k, v in value.items()
                                          if k in Counter.__dataclass_fields__})
                except TypeError:
                    continue          # 单桶损坏不影响整体
            return out
        except (OSError, json.JSONDecodeError, AttributeError):
            return {}                  # 文件坏了就当没统计过

    def _write_locked(self) -> None:
        """原子写：tmp + replace（与 StateStore 同一个套路）。"""
        import os
        import tempfile

        payload = {"version": 1, "buckets": {k: asdict(v) for k, v in self.buckets.items()}}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=1)
                os.replace(tmp, self._path)
            finally:
                if Path(tmp).exists():
                    Path(tmp).unlink()
            self._dirty = False
        except OSError:
            pass                        # 统计写不了不该影响主流程
