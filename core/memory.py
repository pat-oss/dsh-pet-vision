"""core/memory.py — 持久笔记（Notes）：跨会话记住关于主人的事实。

设计取舍（2026-10-31，借鉴 Codex 的 Notes 层但按小鲸鱼的约束裁剪）：

1. **刻意不做向量／神经记忆**：只做符号记忆，明文、可读、可精确删除。
   神经向量记忆删不掉单条事实（机器遗忘本身就是难题），而本项目的硬需求
   恰恰是"隐私清除"。可读性 > 模糊检索能力。

2. **两条写入路径，职责分开**：
   - `extract_from()`（规则）：只认「记住…」「我叫…」这类**显式指令**，
     命中即写，当轮就能用上——这是确定性保证，不能省。
   - `apply_consolidation()`（模型整合，2026-11-01 用户要求"自己记住上下文"）：
     会话结束后由 `observe/judge.consolidate()` 提炼要点，并与旧笔记**合并**
     （同义的旧条目在 drop 里给出来，由这里精确删掉再写新的）。
     这一步会多花一次 API 调用，所以做成可关闭（`chat.auto_consolidate`）。

3. **只挂在提示词尾部**: system 前缀必须字节稳定才能命中 DeepSeek 上下文缓存
   （设计文档 §4.3 的纪律，也能被用量面板的缓存命中率验证）。
   笔记随内容变化，所以进 user 尾部，绝不进 system。

4. **存储就在 %LOCALAPPDATA%\\little-whale-pet\\notes.jsonl**，
   和 secrets / state 同级，一键可清。
"""

from __future__ import annotations

import json
import re
import tempfile
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

MAX_NOTES = 200                 # 上限：超了淘汰最旧，控制提示词注入成本
MAX_TEXT_CHARS = 60             # 单条事实的文本上限
_SENSITIVE = ("密码", "passwd", "password", "网银", "银行卡", "验证码",
              "私钥", "token", "密钥", "身份证", "keychain")


@dataclass
class Note:
    ts: str                     # ISO 时间，写盘时生成
    kind: str                   # fact | prefer | task | avoidance
    text: str                   # 事实内容
    source: str = "explicit"    # explicit（用户明说）| manual（设置/接口写入）

    def row(self) -> dict:
        return asdict(self)


def looks_sensitive(text: str) -> bool:
    """命中私密词一律不入库——宁可少记，也不能把密码一类的内容写进明文文件。"""
    low = text.lower()
    return any(word.lower() in low for word in _SENSITIVE)


# ---- 规则提取：只对"用户明确说"的句式生效，误伤范围可控 ----
# Vosk 语音识别输出**没有标点**、词之间有空格（"我 叫 小明"），
# 所以匹配前先压掉空格，正则也不能依赖标点符号收尾。
_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("explicit", "fact", re.compile(r"^(?:请)?记(?:住|下了?)?[:：]?\s*(.{1,40}?)$")),
    ("fact", "fact", re.compile(r"(?:^|。|，)?我叫(.{1,12}?)(?:就好|就行|可以|吧|啊)?$")),
    ("fact", "fact", re.compile(r"^你可以?叫我(.{1,12}?)$")),
    ("prefer", "prefer", re.compile(r"^我(?:喜欢|爱|中意)(.{1,20}?)$")),
    ("prefer", "prefer", re.compile(r"^我(?:不喜欢|讨厌|烦|受不了)(.{1,20}?)$")),
    ("task", "task", re.compile(r"^我(?:在做|在忙|在搞|最近在|正在|想做)(.{1,25}?)$")),
    ("avoidance", "avoidance", re.compile(r"^(?:别|不要|不许|不准)提(.{1,15}?)$")),
]
_FORGET = re.compile(r"^(?:忘记|忘掉|删掉|去掉)(?:关于)?(.{1,20}?)$")

_KIND_LABEL = {
    "fact": "关于主人",
    "prefer": "主人的喜好",
    "task": "主人在做的事",
    "avoidance": "不要提",
}


def _clean(raw: str) -> str:
    """压掉语音识别的空格 + 收尾标点，保留中间语义。"""
    text = raw.replace(" ", "").strip()
    return text.strip("，。！？、,.!?~～", ) or text


_LABEL_PREFIX = re.compile(r"^\[[^\]]{1,12}\]\s*")


def _strip_label(text: str) -> str:
    """剥掉 render() 加的分类前缀（`[关于主人] …`）。

    实测模型会把渲染后的整行原样抄进 drop（`"[主人在做的事] 主人在写论文"`），
    照原样拿去匹配会依赖"子串恰好包含"这种运气。剥掉前缀才是确定的。
    """
    return _LABEL_PREFIX.sub("", (text or "").strip()).strip()


def _loosely_hits(note_text: str, keyword: str) -> bool:
    """双向子串判定：任一方向包含即视为同一条记忆。

    模型给的 drop 常常是旧笔记的改写版，单向匹配会漏；反向也有用
    （旧笔记比关键词短时）。调用方已保证 keyword 不短于 2 字。
    """
    return keyword in note_text or note_text in keyword


def extract_from(text: str) -> list[tuple[str, str]]:
    """从一句用户话里提取事实。返回 [(kind, content)]，没命中就是空列表。"""
    compact = _clean(text)
    if not compact or looks_sensitive(compact):
        return []
    out: list[tuple[str, str]] = []
    for _, kind, pattern in _RULES:
        match = pattern.search(compact)
        if not match:
            continue
        content = match.group(1).strip()
        if not content or len(content) > MAX_TEXT_CHARS:
            continue
        if looks_sensitive(content):
            continue
        out.append((kind, content))
        break                    # 一句最多提取一条，避免把闲聊全记下来
    return out


def forget_target(text: str) -> str | None:
    """「忘记X」指令：返回要删的关键词。"""
    match = _FORGET.search(_clean(text))
    if not match:
        return None
    keyword = match.group(1).strip()
    return keyword or None


class NoteStore:
    """笔记的唯一写入口。线程安全：会话线程提取、UI 线程读/清。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self.notes: list[Note] = self._load()

    # ---- 读 ----

    def all(self) -> list[Note]:
        with self._lock:
            return list(self.notes)

    def count(self) -> int:
        with self._lock:
            return len(self.notes)

    def render(self, max_items: int = 12, max_chars: int = 500) -> str:
        """渲染成给提示词尾部用的一小段（空则返回空串，调用方据此省略）。"""
        with self._lock:
            recent = self.notes[-max_items:]
            lines: list[str] = []
            size = 0
            for note in recent:
                label = _KIND_LABEL.get(note.kind, "备注")
                line = f"- [{label}] {note.text}"
                if size + len(line) > max_chars:
                    break
                lines.append(line)
                size += len(line) + 1
            return "\n".join(lines)

    # ---- 写 ----

    def add(self, text: str, kind: str = "fact", source: str = "explicit") -> Note | None:
        """写入一条笔记。重复的内容不再追加（只覆盖 kind）。"""
        with self._lock:
            note = self._add_locked(text, kind, source)
            if note is None:
                return None
            self._save_locked()
            return note

    def apply_consolidation(self, remember: list[tuple[str, str]],
                            drop: list[str], source: str = "auto") -> tuple[int, int]:
        """应用一次「记忆整理」的结果。返回 (新增条数, 删除条数)。

        顺序很重要：**先删后加**。模型给的新表述可能正好包含旧关键词，
        先加后删会把刚写进去的又删掉（这个顺序坑过别的项目，别调）。

        删除用"双向子串"匹配：模型给的 drop 往往是旧笔记的改写版
        （“主人喜欢喝咖啡”→“喜欢喝咖啡”），单向 `in` 匹配不上就白删了。
        但短于 2 字的关键词一律拒绝，免得 `删"主人"` 把全部笔记清空。
        """
        added = 0
        removed = 0
        with self._lock:
            for keyword in drop or []:
                key = _strip_label(keyword)
                if len(key) < 2:
                    continue
                before = len(self.notes)
                self.notes = [n for n in self.notes if not _loosely_hits(n.text, key)]
                removed += before - len(self.notes)
            for kind, text in remember or []:
                if self._add_locked(text, kind, source) is not None:
                    added += 1
            if added or removed:
                self._save_locked()
        return added, removed

    def remove_matching(self, keyword: str) -> int:
        """删除包含关键词的笔记（「忘记X」用）。返回删掉几条。"""
        key = (keyword or "").strip()
        if not key:
            return 0
        with self._lock:
            before = len(self.notes)
            self.notes = [n for n in self.notes if key not in n.text]
            removed = before - len(self.notes)
            if removed:
                self._save_locked()
            return removed

    def clear(self) -> int:
        """隐私清除：全部删除。返回被删条数（供 UI 提示）。"""
        with self._lock:
            removed = len(self.notes)
            self.notes = []
            self._save_locked()
            return removed

    # ---- 内部 ----

    def _add_locked(self, text: str, kind: str, source: str) -> Note | None:
        """加锁版的写入核心（调用方负责 save）。返回 None = 被拒绝（空/敏感）。"""
        text = (text or "").strip()
        if not text or looks_sensitive(text):
            return None
        text = text[:MAX_TEXT_CHARS]
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        for old in self.notes:
            if old.text == text:        # 去重：同一事实反复说不累积
                old.kind, old.ts = kind, now
                return old
        note = Note(ts=now, kind=kind, text=text, source=source)
        self.notes.append(note)
        if len(self.notes) > MAX_NOTES:
            self.notes = self.notes[-MAX_NOTES:]
        return note

    def _load(self) -> list[Note]:
        if not self._path.is_file():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
            out: list[Note] = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    out.append(Note(**{k: v for k, v in data.items()
                                       if k in Note.__dataclass_fields__}))
                except (json.JSONDecodeError, TypeError):
                    continue        # 单行损坏不影响其余笔记
            return out
        except OSError:
            return []

    def _save_locked(self) -> None:
        """追加语义用全量重写即可（文件很小），原子写避免写一半损坏。"""
        payload = "".join(json.dumps(n.row(), ensure_ascii=False) + "\n"
                          for n in self.notes)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                os.replace(tmp, self._path)
            finally:
                if Path(tmp).exists():
                    Path(tmp).unlink()
        except OSError:
            pass                    # 笔记写不了不该影响对话
