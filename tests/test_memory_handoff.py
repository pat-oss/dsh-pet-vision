"""tests/test_memory_handoff.py — 会话收尾 → 记忆整理的交接。

背景（2026-11-01 用户要求）：「宠物要能自己记住上下文、整合笔记，
而不是我手动输入『记住X』」。

实现方式是会话收尾时把对话文本交给 main 起后台线程去整理。这里钉住三件事：

1. **有信息量的对话必须交出去**，且 transcript 带角色标注（主人/我）；
2. **一句话寒暄不该交出去**（不值得多烧一次 API）；
3. **回调抛异常不能影响收尾**——finished_session 必须照常发出，
   否则 mode 卡在 chat、唤醒监听被永久挂起（本工程最贵的一类事故）。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必须显式建一个 QApplication：跨线程 signal 需要 Qt 的事件分发器，
# 没有应用对象时 finished_session 投递不到主线程，QEventLoop 永远等不到 quit。
# （旧测试是靠"先跑的 test_pet_menu 建过 QApplication"隐式满足的——
#   单独跑本文件就会挂死，属于典型的脆弱依赖，这里显式化。）
from PySide6.QtWidgets import QApplication      # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _fakes():
    class Judge:
        def reply(self, text, history, frame_b64=None):
            return "好的主人～"

    class Store:
        def set_mode(self, mode):
            pass

        def mark_api_call(self, day):
            pass

        def record_decision(self, row):
            pass

    class Gate:
        def allow(self, kind, now):
            return False, "test"        # 不出声，测试只关心交接

        def mark_reactive(self, now):
            pass

    class Player:
        def enqueue_stream(self, utt_id, chunks):
            pass

    class Engine:
        def new_stop_event(self):
            return threading.Event()

        def stream(self, text, stop_event=None):
            return iter(())

    return Judge(), Store(), Gate(), Player(), Engine()


def _run_session(first_text: str, on_transcript) -> tuple[list[str], list]:
    """跑一次会话直到收尾，返回 (交给回调的 transcript 列表, 收尾事件列表)。"""
    from PySide6.QtCore import QEventLoop, QTimer

    from chat.session import ChatSession

    judge, store, gate, player, engine = _fakes()
    session = ChatSession(judge, store, gate, player, engine, first_text,
                          idle_finish_sec=2, on_transcript=on_transcript)
    handed: list[str] = []
    finished: list[str] = []
    session.finished_session.connect(lambda: finished.append("done"))
    loop = QEventLoop()
    session.finished_session.connect(loop.quit)
    session.start()
    QTimer.singleShot(12000, loop.quit)     # 兜底：绝不挂死
    loop.exec()
    session.wait(3000)                      # 等 finally 里的交接跑完
    return handed, finished


def test_transcript_handed_off() -> None:
    """正经对话必须交给整理，且带角色标注。"""
    handed: list[str] = []

    def on_transcript(text: str) -> None:
        handed.append(text)

    _, finished = _run_session("我最近在赶毕业论文，下周三要交", on_transcript)

    assert finished, "会话没有正常收尾"
    assert handed, "对话没有被交给记忆整理"
    transcript = handed[0]
    assert "主人：" in transcript, transcript
    assert "我：" in transcript, transcript
    assert "毕业论文" in transcript, transcript
    print("PASS 会话收尾时交出 transcript")


def test_short_chitchat_skipped() -> None:
    """寒暄不值得多花一次 API：总字数不足门槛就不交。"""
    handed: list[str] = []

    def on_transcript(text: str) -> None:
        handed.append(text)

    _run_session("你好", on_transcript)

    assert not handed, f"一句话寒暄不该触发整理，实际交出了：{handed}"
    print("PASS 短寒暄不触发整理")


def test_callback_failure_does_not_break_finish() -> None:
    """回调炸了也必须收尾——否则 mode 卡 chat、唤醒永久挂起。"""
    def on_transcript(text: str) -> None:
        raise RuntimeError("模拟整理线程启动失败")

    _, finished = _run_session("我在做一个桌宠项目，用 Python 写的", on_transcript)

    assert finished, "回调异常把会话收尾吞掉了（会导致唤醒监听永久挂起）"
    print("PASS 回调异常不影响收尾")
