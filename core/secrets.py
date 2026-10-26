"""core/secrets.py — API 密钥等敏感项的本地持久化。

首次启动由设置弹窗写入，之后每次启动自动读取，不再询问。
存明文 JSON（本地单用户桌面程序的威胁模型：防的是"每次重启都要输 key"，
不是对抗同机恶意程序——README 已注明，建议用有额度上限的 key）。
写入为原子操作（tmp + os.replace），避免半截文件。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SECRETS_VERSION = 1


def secrets_path(data_dir: Path) -> Path:
    return Path(data_dir) / "secrets.json"


def load_secrets(data_dir: Path) -> dict:
    p = secrets_path(data_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_secrets(data_dir: Path, data: dict) -> None:
    """原子写 + 尽量收紧权限（Windows 上等价于仅当前用户可读的 ACL 由目录继承）。"""
    p = secrets_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".secrets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": SECRETS_VERSION, **data}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass          # Windows 的 chmod 基本是空操作，忽略


def get_api_key(data_dir: Path) -> str:
    return str(load_secrets(data_dir).get("deepseek_api_key") or "")


def set_api_key(data_dir: Path, key: str) -> None:
    data = load_secrets(data_dir)
    data["deepseek_api_key"] = (key or "").strip()
    save_secrets(data_dir, data)
