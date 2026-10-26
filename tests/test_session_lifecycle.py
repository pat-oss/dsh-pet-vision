"""tests/test_session_lifecycle.py — 会话生命周期：空闲收尾 + 思考中信号。

背景（用户实测反馈）：
  1. "第一次叫小鲸鱼有反应，紧接着再叫就没反应"——两个真 bug：
     a. 唤醒后播应答时 speaking 暂停原因泄漏，唤醒监听永久假死
        （钉子测试在 test_wake.py / test_wiring.py）；
     b. 会话跑完不退出，mode=chat 挂起唤醒监听最长 10 分钟。
  2. "增加思考中的提示"——API 要 1–3s，没反馈像她没反应。

这里钉住：会话空闲必须收尾（恢复唤醒），思考信号必须先于回复。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fakes():
    events: list[tuple] = []

    class Judge:
        def reply(self, text, history, frame_b64=None):
            events.append(("reply", text))
            return "好的主人～"

    class Store:
        def set_mode(self, mode):
            events.append(("mode", mode))

        def mark_api_call(self, day):
            pass

        def record_decision(self, row):
            pass

    class Gate:
        def allow(self, kind, now):
            return True, ""

        def mark_reactive(self, now):
            pass

    class Player:
        def enqueue_stream(self, utt_id, chunks):
            events.append(("enqueue", utt_id))

    class Engine:
        def new_stop_event(self):
            return threading.Event()

        def stream(self, text, stop_event=None):
            return iter(())

    return events, Judge(), Store(), Gate(), Player(), Engine()


def test_session_finishes_after_idle() -> None:
    """空闲超过 idle_finish_sec 必须收尾，且 mode 恢复 observe。"""
    from PySide6.QtCore import QEventLoop, QTimer

    from chat.session import ChatSession

    events, judge, store, gate, player, engine = _fakes()
    session = ChatSession(judge, store, gate, player, engine, "你好",
                          idle_finish_sec=2)
    seen: list[tuple] = []
    session.replied.connect(lambda t: seen.append(("replied", t)))
    session.thinking.connect(lambda: seen.append(("thinking",)))
    loop = QEventLoop()
    session.finished_session.connect(loop.quit)
    session.start()
    QTimer.singleShot(9000, loop.quit)          # 兜底：绝不挂死
    loop.exec()
    session.wait(3000)

    assert ("replied", "好的主人～") in seen, seen
    # 思考中必须先于回复出现（用户要的"她在反应"反馈）
    assert seen.index(("thinking",)) < seen.index(("replied", "好的主人～")), seen
    # 收尾时恢复 observe——否则唤醒监听一直被挂起
    assert ("mode", "chat") in events and events[-1] == ("mode", "observe"), events
    print("PASS 会话空闲自动收尾并恢复 observe")


def test_session_stays_alive_while_active() -> None:
    """ idle_finish_sec 内持续有输入 → 会话不能提前收尾。"""
    from PySide6.QtCore import QEventLoop, QTimer

    from chat.session import ChatSession

    events, judge, store, gate, player, engine = _fakes()
    session = ChatSession(judge, store, gate, player, engine, "你好",
                          idle_finish_sec=3)
    finished = []
    session.finished_session.connect(lambda: finished.append(True))
    session.start()
    loop = QEventLoop()
    # 1s / 2s 时各追问一句（都 < 3s 空闲窗），3.5s 时检查还活着
    QTimer.singleShot(1000, lambda: session.submit("再来一句"))
    QTimer.singleShot(2000, lambda: session.submit("还有一句"))
    QTimer.singleShot(3500, loop.quit)
    loop.exec()
    assert not finished, "活跃输入中会话被提前收尾了"
    session.stop()
    session.wait(3000)
    print("PASS 活跃输入中会话保持")


def main() -> None:
    test_session_finishes_after_idle()
    test_session_stays_alive_while_active()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
