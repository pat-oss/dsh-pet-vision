"""pet/stats.py — 贴在输入框上方的用量面板。

三个刻意的选择：

1. **不替代输入框，而是叠在它上方**（2026-11-01 用户要求）。点一下宠物，
   输入框照旧出现，用量面板浮在它上边——想看数字时还顺便能打字。

2. **独立顶层窗口，不是宠物窗口的子控件**。宠物窗口被 setFixedSize(320×180)
   钉死了，塞进去只能挤掉角色画布；做成无边框顶层窗口后可以自由放在窗口外，
   还能跟随宠物窗口移动（见 PetWindow._sync_panel）。

3. **不抢焦点**：Tool + WindowDoesNotAcceptFocus + WA_ShowWithoutActivating。
   桌宠是"一直在旁边"的东西，一抢焦点就等于每次点它都打断当前输入。

刷新由 PetWindow 的定时器驱动（只在可见时跑）：统计是别的线程在累加的，
面板躺着几分钟前的旧数字会让人以为坏了。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget)

PANEL_W = 320          # 与宠物窗口同宽，视觉上是一根竖轴
_GAP = 6               # 面板与输入框之间的间隙

_ROWS = (
    ("session", "本次启动"),
    ("day", "今天"),
    ("month", "本月"),
    ("all", "总计"),
)


class StatsPanel(QWidget):
    """用量速览：四档口径 × (调用数 / token / 缓存命中)。"""

    def __init__(self, usage=None) -> None:
        # 刻意不传 parent：作为独立顶层窗口，才能浮到宠物窗口外面去
        super().__init__(None)
        self._usage = usage
        self._cells: dict[str, tuple[QLabel, QLabel, QLabel]] = {}

        self.setWindowFlags(Qt.WindowType.Tool
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedWidth(PANEL_W)

        card = QFrame(self)
        card.setObjectName("card")
        card.setStyleSheet(
            "#card { background: rgba(255,255,255,246);"
            "        border: 1px solid #D3D1C7; border-radius: 10px; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        inner = QVBoxLayout(card)
        inner.setContentsMargins(12, 9, 12, 10)
        inner.setSpacing(5)

        title = QLabel("用量 · 发出去的每句话都在这里")
        title.setStyleSheet("color: #5F5E5A; font-size: 11px; border: none;")
        inner.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        grid.setContentsMargins(0, 0, 0, 0)
        heads = ("", "调用", "token", "命中")
        for col, text in enumerate(heads):
            head = QLabel(text)
            head.setStyleSheet("color: #888780; font-size: 11px; border: none;")
            head.setAlignment(Qt.AlignmentFlag.AlignRight
                              if col else Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(head, 0, col)

        for row, (key, label) in enumerate(_ROWS, start=1):
            name = QLabel(label)
            name.setStyleSheet("color: #2C2C2A; font-size: 12px; border: none;")
            calls = QLabel("—")
            calls.setStyleSheet("color: #2C2C2A; font-size: 12px; border: none;")
            calls.setAlignment(Qt.AlignmentFlag.AlignRight)
            tokens = QLabel("—")
            tokens.setStyleSheet("color: #2C2C2A; font-size: 12px; border: none;")
            tokens.setAlignment(Qt.AlignmentFlag.AlignRight)
            hit = QLabel("—")
            hit.setStyleSheet("color: #0F6E56; font-size: 12px; border: none;")
            hit.setAlignment(Qt.AlignmentFlag.AlignRight)
            grid.addWidget(name, row, 0)
            grid.addWidget(calls, row, 1)
            grid.addWidget(tokens, row, 2)
            grid.addWidget(hit, row, 3)
            self._cells[key] = (calls, tokens, hit)

        # 标签列吃掉多余宽度，数值列贴右；命中列给固定宽度，
        # 免得百分比位数变化时整行左右抖动。
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(2, 0)
        grid.setColumnStretch(3, 0)
        grid.setColumnMinimumWidth(3, 44)
        inner.addLayout(grid)

        # 单行短说明 + 显式最小高度：10px 字号在布局里容易被压扁成一条糊字
        # （第一版就这样，截图一眼就看出来了）。
        hint = QLabel("命中 = 输入 token 复用缓存的占比")
        hint.setStyleSheet("color: #B4B2A9; font-size: 11px; border: none;")
        hint.setMinimumHeight(16)
        inner.addWidget(hint)

        self.refresh()

    # ---- 数据 ----

    def refresh(self) -> None:
        if self._usage is None:
            return
        try:
            snap = self._usage.snapshot()
        except Exception:      # noqa: BLE001 统计坏了不许影响面板显示
            return
        for key, (calls, tokens, hit) in self._cells.items():
            row = snap.get(key) or {}
            n = int(row.get("calls", 0))
            calls.setText(f"{n} 次" if n else "—")
            total = int(row.get("total", 0))
            tokens.setText(f"{total:,}" if total else "—")
            hit.setText(f"{row.get('cache_hit_rate', 0.0):.0%}" if total else "—")

    # ---- 定位 ----

    def place_above(self, anchor: QRect) -> None:
        """把面板放到 anchor 正上方；上方空间不够就翻到下方。"""
        self.adjustSize()
        width, height = self.width(), self.height()
        screen = QGuiApplication.screenAt(anchor.center()) \
            or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()

        x = anchor.left() + (anchor.width() - width) // 2
        y = anchor.top() - height - _GAP
        if y < area.top():
            y = anchor.bottom() + _GAP          # 贴着屏幕顶端时翻到下面
        x = max(area.left(), min(x, area.right() - width))
        self.move(x, y)
