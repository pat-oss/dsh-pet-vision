"""core/prefs.py — 用户偏好覆盖层。

config.yaml 是随程序分发的默认值（带注释、可读可改）；
首次设置弹窗的修改写进 data/user_prefs.json，启动时深合并覆盖默认值。
这样既保留一份人类可读的默认配置，又不用为了改两个字段重写整个 YAML
（yaml.safe_dump 会丢注释）。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

PREFS_VERSION = 1


def prefs_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_prefs.json"


def load_prefs(data_dir: Path) -> dict:
    p = prefs_path(data_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_prefs(data_dir: Path, data: dict) -> None:
    p = prefs_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".prefs-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": PREFS_VERSION, **data}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 的键覆盖 base，嵌套 dict 逐层合并。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out
