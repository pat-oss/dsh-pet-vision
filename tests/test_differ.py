"""tests/test_differ.py — dHash 阈值标定 + 变化检测行为验证。

在用户实际屏幕场景上运行，观察"同场景静止 → 距离低、切换场景 → 距离高"，
据此校准 config.yaml 的 observe.diff_threshold（设计文档 §15-4）。
"""

from __future__ import annotations

import time

import pytest

from observe.differ import FrameDiffer
from observe.screen import ScreenSource


def test_same_frame_zero_distance() -> None:
    differ = FrameDiffer()
    screen = ScreenSource()
    frame = screen.capture()
    assert differ.distance(differ.dhash(frame), differ.dhash(frame)) == 0
    screen.close()


@pytest.mark.skip(reason="交互式标定脚本：需人工换场景，请直接运行 python tests/test_differ.py")
def test_threshold_calibration() -> None:
    """交互式标定：每 2s 采样一次，打印与上一帧的汉明距离。Ctrl+C 结束。"""
    differ = FrameDiffer()
    screen = ScreenSource()
    last = differ.dhash(screen.capture())
    print("采样中（2s 一次）……换场景看距离跳变，静止看距离趋零。Ctrl+C 停止。")
    try:
        while True:
            time.sleep(2)
            frame = screen.capture()
            h = differ.dhash(frame)
            d = differ.distance(last, h)
            last = h
            flag = "变化" if d >= 12 else "静止"
            print(f"distance={d:2d}  {flag}")
    except KeyboardInterrupt:
        print("\n标定建议：把 config.yaml 的 diff_threshold 设在'静止典型值'与'变化典型值'之间。")
    finally:
        screen.close()


if __name__ == "__main__":
    test_threshold_calibration()
