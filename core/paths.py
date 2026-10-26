"""core/paths.py — 运行路径解析。

开发模式：资源与数据都在仓库目录内。
打包后（PyInstaller onedir）：资源（帧/模型/配置）随 exe 分发，只读；
数据（状态/密钥/偏好/日志）写到 %LOCALAPPDATA%\\little-whale-pet，
避免装在 Program Files 下因无写权限而崩。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "little-whale-pet"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """只读资源根目录（config.yaml / pet/assets / models / vendor）。"""
    if is_frozen():
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        # PyInstaller 6.x onedir：捆绑数据在 <dist>/<app>/_internal/，
        # 而 exe 和我们手动复制的资源在上一层 <dist>/<app>/
        if meipass.name == "_internal" and meipass.parent.is_dir():
            return meipass.parent
        return meipass
    return Path(__file__).resolve().parents[1]


def data_root() -> Path:
    """可写数据根目录（state/secrets/prefs/日志）。"""
    override = os.environ.get("LITTLE_WHALE_DATA_DIR")
    if override:                      # 测试/便携模式可用环境变量指定
        base = Path(override)
        base.mkdir(parents=True, exist_ok=True)
        return base
    if is_frozen():
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
    else:
        base = app_root() / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def resource(relative: str) -> Path:
    """资源路径（相对 app_root）。"""
    return app_root() / relative
