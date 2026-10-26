"""pet/app.py — PetWindow：置顶透明无边框桌宠窗口 + 托盘 + 聊天输入。

窗口特性：FramelessWindowHint + WindowStaysOnTopHint + Tool（不进任务栏）
+ WA_TranslucentBackground。多显示器/DPI 风险的已知限制见 README。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QPainter
from PySide6.QtWidgets import (QApplication, QLabel, QLineEdit, QMenu,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from pet.anim import FrameAnimator

WINDOW_W, WINDOW_H = 320, 180   # 640×360 帧 × scale 0.5


class PetWindow(QWidget):
    user_text_submitted = Signal(str)
    interrupt_requested = Signal()
    vision_toggled = Signal(bool)          # 视觉功能开关（让她持续看屏幕）
    dnd_toggled = Signal(bool)             # 专注模式（免打扰），默认关视觉

    def __init__(self, frames_root: Path, cfg) -> None:
        super().__init__()
        self.cfg = cfg
        self._drag_pos = None
        self._click_anim_at = 0.0

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        # 半透明分层窗口：不画系统背景、每次重绘先整块清成透明——
        # 否则 Windows 合成器偶发把未绘制区域画成大面积黑块。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        # 注意：不要把 background:transparent 设在窗口级。QMenu 作为子窗口会
        # **继承**这条样式，导致托盘/右键菜单背景透出桌面深色区、默认黑字
        # 完全看不见（用户实测反馈）。透明只挂在真正要透的画布上。
        self.setFixedSize(WINDOW_W, WINDOW_H)
        self.setWindowTitle("小鲸鱼")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部：聊天气泡（说话内容短暂显示）
        self.bubble = QLabel("")
        self.bubble.setWordWrap(True)
        self.bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bubble.setStyleSheet(
            "background: rgba(255,255,255,235); color: #333; border-radius: 8px;"
            "padding: 4px 8px; font-size: 12px;")
        self.bubble.hide()
        layout.addWidget(self.bubble)

        # 中部：输入框（点击宠物后显示）
        self.input = QLineEdit()
        self.input.setPlaceholderText("跟小鲸鱼说点什么…（Enter 发送，Esc 收起）")
        self.input.setStyleSheet(
            "border: 1px solid #85B7EB; border-radius: 8px; padding: 3px 6px;"
            "font-size: 12px; background: white;")
        self.input.returnPressed.connect(self._submit_text)
        self.input.hide()
        layout.addWidget(self.input)

        # 底部：动画画布（只在这里要透明，别设在窗口级，见上）
        self.canvas = QLabel()
        self.canvas.setStyleSheet("background: transparent;")
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.canvas)

        self.animator = FrameAnimator(self.canvas, frames_root,
                                      fps=cfg.fps, scale=cfg.scale)
        self._place_corner()
        self._build_tray()

        self._reassert = QTimer(self)
        self._reassert.setInterval(max(5, cfg.topmost_reassert_sec) * 1000)
        self._reassert.timeout.connect(self._reassert_topmost)
        if cfg.always_on_top:
            self._reassert.start()

        self._bubble_timer = QTimer(self)
        self._bubble_timer.setSingleShot(True)
        self._bubble_timer.timeout.connect(self.bubble.hide)

    # ---- 位置与置顶 ----

    def _place_corner(self) -> None:
        screen = QGuiApplication.primaryScreen().availableGeometry()
        if self.cfg.corner == "bottom-right":
            x, y = screen.right() - WINDOW_W - 16, screen.bottom() - WINDOW_H - 16
        elif self.cfg.corner == "bottom-left":
            x, y = screen.left() + 16, screen.bottom() - WINDOW_H - 16
        else:
            x, y = screen.right() - WINDOW_W - 16, screen.top() + 16
        self.move(x, y)

    def _reassert_topmost(self) -> None:
        """资源管理器重启/DPI 变更可能丢置顶：定时重新 assert + 重绘。

        顺带 repaint 一次：分层窗口在 DPI 变化/合成器重建后可能残留黑块，
        强制重绘清掉。
        """
        self.raise_()
        self.repaint()

    # ---- 托盘 ----

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        icon_path = Path(__file__).parent / "assets" / "icon.png"
        self.tray.setIcon(QIcon(str(icon_path)) if icon_path.is_file()
                          else QApplication.style().standardIcon(
                              QApplication.style().StandardPixmap.SP_ComputerIcon))
        self.act_setup = QAction("重新设置…", self)
        self.act_vision = QAction("视觉功能（让她看屏幕）", self, checkable=True)
        self.act_dnd = QAction("专注模式（免打扰）", self, checkable=True)
        self.act_vision.setChecked(True)      # 默认开，具体值由 main 同步
        self.act_dnd.setChecked(False)
        self.act_vision.toggled.connect(self.vision_toggled)
        self.act_dnd.toggled.connect(self.dnd_toggled)
        self.tray.setContextMenu(self._make_menu(with_test=True))
        self.tray.setToolTip("小鲸鱼桌宠（右键她也能打开菜单）")
        self.tray.show()

    def _make_menu(self, with_test: bool = False) -> QMenu:
        """通用菜单：托盘与宠物窗口右键共用（Windows 11 会把托盘图标收进
        溢出区，右键宠物是更稳的入口）。动作对象复用，信号只连一次。

        样式必须显式写死：PetWindow 曾是 WA_TranslucentBackground + 透明
        样式表，QMenu 会继承透明背景，在深色桌面上就是"整个菜单发黑、
        文字看不见"。所以这里给出不透明的浅色菜单，覆盖任何继承。
        """
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #FFFFFF; border: 1px solid #D3D1C7;"
            "        border-radius: 6px; padding: 4px 0px; }"
            "QMenu::item { padding: 6px 24px; color: #2C2C2A; }"
            "QMenu::item:selected { background-color: #85B7EB; color: #FFFFFF; }"
            "QMenu::item:disabled { color: #B4B2A9; }"
            "QMenu::separator { height: 1px; background-color: #D3D1C7;"
            "                  margin: 4px 10px; }"
        )
        menu.addAction(self.act_setup)
        menu.addAction(self.act_vision)
        menu.addAction(self.act_dnd)
        if with_test:
            act_test = QAction("让它说句话", menu)
            act_test.triggered.connect(
                lambda: self.user_text_submitted.emit("说句话哄哄主人"))
            menu.addAction(act_test)
        act_input = QAction("跟它说话", menu)
        act_input.triggered.connect(self.toggle_input)
        menu.addAction(act_input)
        menu.addSeparator()
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(QApplication.quit)
        menu.addAction(act_quit)
        return menu

    # ---- 交互 ----

    def toggle_input(self) -> None:
        if self.input.isVisible():
            self.input.hide()
        else:
            self.input.show()
            self.input.setFocus()

    def _submit_text(self) -> None:
        text = self.input.text().strip()
        if text:
            self.user_text_submitted.emit(text)
            self.input.clear()
            self.input.hide()

    def show_bubble(self, text: str, ms: int = 6000) -> None:
        self.bubble.setText(text)
        self.bubble.show()
        self._bubble_timer.start(ms)

    def set_state(self, state: str) -> None:
        self.animator.set_state(state)

    def play_click(self) -> None:
        self.animator.play_click()

    # ---- 鼠标：拖拽 + 点击 ----

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()
            self._press_pos = event.globalPosition().toPoint()
            self._press_time = event.timestamp()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.pos() + delta)
            self._drag_pos = event.globalPosition().toPoint()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_pos = None
        if event.button() != Qt.MouseButton.LeftButton:
            return                      # 右键交给 contextMenuEvent，不开输入框
        moved = (self._press_pos - event.globalPosition().toPoint()).manhattanLength() \
            if hasattr(self, "_press_pos") else 999
        quick = (event.timestamp() - getattr(self, "_press_time", 0)) < 250 \
            if hasattr(event, "timestamp") else True
        if moved < 6 and quick:
            self.play_click()
            self.toggle_input()

    def mouseDoubleClickEvent(self, event) -> None:
        self.interrupt_requested.emit()

    def contextMenuEvent(self, event) -> None:
        """右键宠物 = 菜单入口（托盘图标可能被 Windows 收进溢出区）。"""
        self._make_menu().exec(event.globalPos())

    def paintEvent(self, event) -> None:  # noqa: N802 Qt 命名
        """整块清成透明再交给子控件：避免未绘制区域被合成为黑块。"""
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.end()
        super().paintEvent(event)
