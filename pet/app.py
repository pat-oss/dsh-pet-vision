"""pet/app.py — PetWindow：置顶透明无边框桌宠窗口 + 托盘 + 聊天输入。

窗口特性：FramelessWindowHint + WindowStaysOnTopHint + Tool（不进任务栏）
+ WA_TranslucentBackground。多显示器/DPI 风险的已知限制见 README。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QPainter
from PySide6.QtWidgets import (QApplication, QLabel, QLineEdit, QMenu,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from pet.anim import FrameAnimator
from pet.stats import StatsPanel

WINDOW_W, WINDOW_H = 320, 180   # 640×360 帧 × scale 0.5


class PetWindow(QWidget):
    user_text_submitted = Signal(str)
    interrupt_requested = Signal()
    vision_toggled = Signal(bool)          # 视觉功能开关（让她持续看屏幕）
    dnd_toggled = Signal(bool)             # 专注模式（免打扰），默认关视觉

    def __init__(self, frames_root: Path, cfg, usage=None) -> None:
        super().__init__()
        self.cfg = cfg
        self._usage = usage          # core.usage.UsageStore | None
        self._panel = None           # 懒创建：输入框上方的用量面板
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

        # 面板可见时才刷新的定时器：统计是别的线程在累加的，面板静置太久
        # 会显示几分钟前的旧数字（看着像坏了）。不可见就 stop，避免空转。
        self._panel_timer = QTimer(self)
        self._panel_timer.setInterval(2000)
        self._panel_timer.timeout.connect(self._refresh_panel)

        # 输入框里按 Esc 收起（占位提示里一直写着，但之前没实现）
        self.input.installEventFilter(self)

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
        """单击/菜单的统一入口：输入框与用量面板一起出现、一起收起。"""
        if self.input.isVisible():
            self.hide_input()
        else:
            self.show_input()

    def show_input(self) -> None:
        """显示输入框，并把用量面板贴在它上方（不是替代，两者同时可见）。

        顺序有意为之：先把面板摆出来，最后才 setFocus 输入框——
        面板虽然是"不激活"窗口类型，但 raise_ 在某些壳层下仍可能抢走焦点，
        最后一步收回来才稳（否则点开却打不了字）。
        """
        self.input.show()
        self._show_panel()
        # 宠物是 Frameless+Tool 窗口，默认不会自己激活；不显式激活的话
        # setFocus 只是"窗口内焦点"，键盘输入仍然打不进来。
        self.activateWindow()
        self.input.setFocus()

    def hide_input(self) -> None:
        self.input.hide()
        self._hide_panel()

    def _submit_text(self) -> None:
        text = self.input.text().strip()
        if text:
            self.user_text_submitted.emit(text)
            self.input.clear()
            self.hide_input()

    # ---- 用量面板 ----

    def _ensure_panel(self):
        if self._panel is None:
            self._panel = StatsPanel(self._usage)
        return self._panel

    def _show_panel(self) -> None:
        panel = self._ensure_panel()
        panel.refresh()
        panel.show()          # 先显示再定位：adjustSize 需要控件已完成布局
        self._sync_panel()
        panel.raise_()
        self._panel_timer.start()

    def _hide_panel(self) -> None:
        self._panel_timer.stop()
        if self._panel is not None:
            self._panel.hide()

    def _refresh_panel(self) -> None:
        if self._panel is not None and self._panel.isVisible():
            self._panel.refresh()

    def _sync_panel(self) -> None:
        """把面板挪到窗口正上方（输入框就在窗口顶部，视觉上就是"贴输入框上边"）。

        传的是**整个窗口矩形**而不是顶部一条线：屏幕顶端放不下时，
        `place_above` 要按窗口底边把面板翻到下面去，高度传 1 会算错。
        """
        if self._panel is None or not self._panel.isVisible():
            return
        top_left = self.mapToGlobal(QPoint(0, 0))
        self._panel.place_above(QRect(top_left, QSize(self.width(), self.height())))

    def show_bubble(self, text: str, ms: int = 6000) -> None:
        self.bubble.setText(text)
        self.bubble.show()
        self._bubble_timer.start(ms)
        # 气泡把输入框往下推了，面板得跟着重算位置
        self._sync_panel()

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

    def moveEvent(self, event) -> None:      # noqa: N802 Qt 命名
        """拖动宠物时面板要跟着走，否则会孤零零留在原地。"""
        super().moveEvent(event)
        self._sync_panel()

    def eventFilter(self, obj, event):       # noqa: N802 Qt 命名
        """输入框里按 Esc 收起（连面板一起），与占位提示保持一致。"""
        if obj is self.input and event.type() == QEvent.Type.KeyPress \
                and event.key() == Qt.Key.Key_Escape:
            self.hide_input()
            return True
        return super().eventFilter(obj, event)

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
            # 单击 = 输入框 + 上方用量面板（2026-11-01 用户要求：
            # 面板是"加在上边"，不是替代输入框）。
            self.toggle_input()

    def mouseDoubleClickEvent(self, event) -> None:
        self.interrupt_requested.emit()

    def contextMenuEvent(self, event) -> None:
        """右键宠物 = 菜单入口（托盘图标可能被 Windows 收进溢出区）。"""
        self._make_menu().exec(event.globalPos())

    def closeEvent(self, event) -> None:     # noqa: N802 Qt 命名
        """面板是无父顶层窗口，窗口关了要一起收掉，否则会留在桌面上。"""
        self._hide_panel()
        super().closeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 Qt 命名
        """整块清成透明再交给子控件：避免未绘制区域被合成为黑块。"""
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.end()
        super().paintEvent(event)
