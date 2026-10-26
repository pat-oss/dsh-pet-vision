"""tests/test_pet_menu.py — 宠物窗口右键菜单冒烟测试（offscreen，不弹真菜单）。

背景：Windows 11 会把托盘图标收进溢出区，用户找不到「重新设置…」。
修复：右键宠物窗口弹出同一套菜单。这里验证菜单内容与动作复用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _window():
    from PySide6.QtWidgets import QApplication
    from core.types import PetConfig
    from pet.app import PetWindow
    app = QApplication.instance() or QApplication([])
    return PetWindow(Path(__file__).resolve().parents[1] / "pet" / "assets" / "frames",
                     PetConfig()), app


def test_window_menu_has_setup() -> None:
    win, _app = _window()
    texts = [a.text() for a in win._make_menu().actions()]
    assert "重新设置…" in texts, texts          # 用户投诉的核心：右键要有设置
    assert "退出" in texts, texts
    assert "让它说句话" not in texts             # 窗口菜单保持精简
    print("PASS 右键菜单含设置项")


def test_menu_has_opaque_stylesheet() -> None:
    """菜单必须显式不透明，否则继承窗口的 transparent 会整块发黑。"""
    win, _app = _window()
    qss = win._make_menu().styleSheet()
    assert "#FFFFFF" in qss or "white" in qss.lower(), (
        f"菜单没有不透明背景色，Windows 上会继承透明样式导致黑菜单：{qss!r}"
    )
    print("PASS 菜单显式不透明背景")


def test_window_stylesheet_is_not_transparent() -> None:
    """窗口级不能挂 background:transparent，否则菜单继承后看不见。"""
    win, _app = _window()
    assert "transparent" not in (win.styleSheet() or ""), (
        f"窗口级样式表含 transparent：{win.styleSheet()!r}"
    )
    print("PASS 窗口级无透明样式表")


def test_vision_action_present_and_checkable() -> None:
    """视觉功能开关：可勾选，且托盘/右键共用同一动作对象。"""
    win, _app = _window()
    assert win.act_vision.isCheckable()
    menu = win._make_menu()
    assert win.act_vision in menu.actions()
    tray = win._make_menu(with_test=True)
    assert win.act_vision in tray.actions()
    assert "视觉功能（让她看屏幕）" in [a.text() for a in menu.actions()]
    print("PASS 视觉开关进入菜单且可勾选")


def test_vision_signal_emits_bool() -> None:
    win, _app = _window()
    got = []
    win.vision_toggled.connect(lambda v: got.append(v))
    win.act_vision.setChecked(False)
    win.act_vision.setChecked(True)
    assert got == [False, True], got
    print("PASS 视觉开关信号传出布尔值")


def test_actions_shared_with_tray() -> None:
    """动作对象复用：main.py 只连一次 window.act_setup.triggered。"""
    win, _app = _window()
    menu = win._make_menu()
    assert win.act_setup in menu.actions()
    assert win.act_dnd in menu.actions()
    assert win.act_dnd.isCheckable()
    tray_texts = [a.text() for a in win._make_menu(with_test=True).actions()]
    assert "让它说句话" in tray_texts            # 托盘保留演示项
    print("PASS 托盘/右键共用动作对象")


def main() -> None:
    test_window_menu_has_setup()
    test_menu_has_opaque_stylesheet()
    test_window_stylesheet_is_not_transparent()
    test_vision_action_present_and_checkable()
    test_vision_signal_emits_bool()
    test_actions_shared_with_tray()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
