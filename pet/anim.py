"""pet/anim.py — 帧序列播放器：状态 → 动画映射 + QTimer 播放。

帧素材由 scripts/prepare_assets.py 从 dsh-pet 的 webm 一次性生成：
解码黑底画面 → 连续 alpha matting（线性光亮度比反解 + 前景色去污染）
→ 预乘线性光空间缩到显示尺寸 320×180@12fps。

素材源是**纯黑底无 alpha** 的 yuv420p，网页版靠 CSS screen 混合抠黑；
桌面窗口做不到，所以必须自己生成 alpha，且必须一次缩放到位——
因此 scale 保持 1.0，避免运行时再重采样把边缘糊掉。
"""

from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QWidget


class FrameAnimator:
    """把一个 QLabel 当画布：按状态循环播放帧序列。"""

    def __init__(self, label: QLabel, frames_root: Path, fps: int = 12,
                 scale: float = 0.5) -> None:
        self.label = label
        self.fps = fps
        self.scale = scale
        self._states: dict[str, list[QPixmap]] = {}
        self._click_pools: dict[str, list[QPixmap]] = {}
        self._state = "idle"
        self._frame_index = 0
        self._once_queue: list[tuple[list[QPixmap], str]] = []
        self._timer = QTimer()
        self._timer.setInterval(int(1000 / max(1, fps)))
        self._timer.timeout.connect(self._tick)
        self._load(frames_root)

    # ---- 素材装载 ----

    def _load(self, root: Path) -> None:
        root = Path(root)
        if not root.is_dir():
            raise FileNotFoundError(f"帧素材目录不存在: {root}（先跑 scripts/prepare_assets.py）")
        for state_dir in sorted(root.iterdir()):
            if not state_dir.is_dir():
                continue
            pixmaps = [self._scaled(p) for p in sorted(state_dir.glob("*.png"))]
            pixmaps = [p for p in pixmaps if p is not None]
            if not pixmaps:
                continue
            if state_dir.name.startswith("click_"):
                self._click_pools[state_dir.name] = pixmaps
            else:
                self._states[state_dir.name] = pixmaps
        if "idle" not in self._states:
            raise FileNotFoundError("缺少 idle 帧素材")

    def _scaled(self, path: Path) -> QPixmap | None:
        """按帧的自然尺寸 × scale（帧已是显示尺寸，scale=1 时原样返回）。"""
        pm = QPixmap(str(path))
        if pm.isNull():
            return None
        if abs(self.scale - 1.0) < 1e-6:
            return pm
        return pm.scaled(int(pm.width() * self.scale), int(pm.height() * self.scale),
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)

    # ---- 播放控制 ----

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        if state in self._states and state != self._state:
            self._state = state
            self._frame_index = 0

    def play_click(self) -> None:
        """随机播一个点击回应动画，播完回到当前状态。"""
        if not self._click_pools:
            return
        pool = random.choice(list(self._click_pools.values()))
        self._once_queue.append((pool, self._state))

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        if self._once_queue:
            frames, back_to = self._once_queue[0]
            if self._frame_index >= len(frames):
                self._once_queue.pop(0)
                self._frame_index = 0
                if self._once_queue:
                    return
                self._state = back_to if back_to in self._states else "idle"
                return
            self.label.setPixmap(frames[self._frame_index])
            self._frame_index += 1
            return
        frames = self._states.get(self._state) or self._states["idle"]
        self.label.setPixmap(frames[self._frame_index % len(frames)])
        self._frame_index += 1
