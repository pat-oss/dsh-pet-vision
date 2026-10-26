"""chat/session.py — ChatSession：用户主动搭话的会话执行器（QThread）。

会话进行中置 mode=chat，观察循环让行；结束恢复 observe。
单次会话上限 20 轮或 10 分钟（防长会话把观察无限挂起）。
**空闲收尾**：最后一次输入/回复后 idle_finish_sec 秒内没有新输入就结束
会话——否则 mode=chat 会一直挂起唤醒监听（用户反馈：聊过一次之后
再喊唤醒词没反应，根因就是会话挂着不退）。
TTS 走播放线程的流式通道（合成在播放线程内完成）。

关键稳定性约束（踩坑沉淀）：
- 回复「气泡」必须最先交给 UI，记账/TTS 任何一步异常都不能把它吞掉；
  这两步各自包 try/except，失败只记日志不致命。
- 外层 try/finally 兜底：无论正常结束还是异常崩掉，都必须恢复 observe
  并发出 finished_session，否则 mode=chat + 'session' 暂停原因会永久泄漏，
  唤醒监听被暂停（"聊过一次就再也喊不醒她"）。
- 会话线程内部的异常原本走 log_line 信号，但该信号未接入 main 会丢失，
  故同时写 session_debug.log（文件级），保证真实运行时异常可见。
  **只记异常与关键节点（空回复、兜底、TTS 句数）**，正常路径不写，避免日志灌水。
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.state import StateStore
from core.types import Turn
from observe.judge import VisionJudge
from voice.cache import split_sentences

# ---- 异常/关键节点文件日志（绕过可能未接线的 log_line 信号） ----
_dbg_lock = threading.Lock()


def _dbg(msg: str) -> None:
    try:
        p = Path.home() / "AppData" / "Local" / "little-whale-pet" / "session_debug.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with _dbg_lock:
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


MAX_TURNS = 20
MAX_SESSION_SEC = 600
IDLE_FINISH_SEC = 8          # 空闲多久自动收尾（见模块文档）


class ChatSession(QThread):
    state_changed = Signal(str)
    replied = Signal(str)          # 宠物的回复文本（UI 气泡/聊天记录）
    thinking = Signal()            # 开始思考（UI 显示"思考中…"）
    log_line = Signal(str)
    finished_session = Signal()

    def __init__(self, judge: VisionJudge, store: StateStore, gate, player, engine,
                 first_user_text: str, frame_source=None,
                 idle_finish_sec: int = IDLE_FINISH_SEC) -> None:
        super().__init__()
        self.judge = judge
        self.store = store
        self.gate = gate
        self.player = player
        self.engine = engine
        self.history: list[Turn] = []
        self._pending: list[str] = [first_user_text]
        self._stopped = False
        self._turn = 0
        self._started_at = time.monotonic()
        self._idle_finish_sec = max(2, int(idle_finish_sec))
        self._last_activity_at = self._started_at
        # 取当前屏幕截图的回调（视觉功能开启时由 main 注入）。
        # 不给的话主人说"看看我屏幕"她只能凭文字瞎猜——实测就是这个坑。
        self.frame_source = frame_source

    def _current_frame_b64(self) -> str | None:
        if self.frame_source is None:
            return None
        try:
            return self.frame_source()
        except Exception:  # noqa: BLE001 截屏失败不该让会话崩
            return None

    def submit(self, text: str) -> None:
        self._pending.append(text)
        self._last_activity_at = time.monotonic()

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        self.store.set_mode("chat")
        try:
            while not self._stopped:
                if not self._pending:
                    self.msleep(200)
                    if self._turn >= MAX_TURNS or time.monotonic() - self._started_at > MAX_SESSION_SEC:
                        break
                    # 空闲收尾：不主动退场的话 mode=chat 会一直挂起唤醒监听，
                    # 用户再喊名字没反应（实测反馈）。_turn>0 保证至少答完一轮。
                    if (self._turn > 0 and time.monotonic() - self._last_activity_at
                            > self._idle_finish_sec):
                        break
                    continue
                user_text = self._pending.pop(0)
                self._turn += 1
                self.history.append(Turn(role="user", text=user_text))

                self.state_changed.emit("think")
                self.thinking.emit()          # UI 显示"思考中…"，让人知道她在反应
                try:
                    frame_b64 = self._current_frame_b64()
                    reply = self.judge.reply(user_text, self.history, frame_b64)
                except Exception as exc:
                    self.log_line.emit(f"会话 API 失败: {exc}")
                    self.state_changed.emit("error")
                    reply = "唔……脑子突然打结了，主人再说一遍好不好？"
                # 默认不限制 token（设置窗可收紧）。一旦用户自己设了上限，
                # 推理模型可能把预算全花在"思考"上、正文一个字都不吐
                # （finish_reason=length，content 空）。judge.reply 内部已加大
                # 预算重试一次；仍空则这里兜底——否则 split_sentences('')=0 句
                # （没语音）+ show_bubble('') 空气泡（看着像完全没反应）。
                if not reply or not reply.strip():
                    _dbg("reply 为空 → 使用兜底话术")
                    reply = "唔……我刚刚有点走神，主人再说一遍好不好？"

                # 关键：先把回复交给 UI（气泡），记账/TTS 失败都不能吞掉回复。
                # 哪怕后面记账或语音崩了，主人至少能看到她说的话。
                try:
                    self.replied.emit(reply)
                except Exception as exc:
                    _dbg(f"replied.emit 失败: {exc!r}")

                # ---- 记账（非关键，失败不致命） ----
                try:
                    self._last_activity_at = time.monotonic()
                    self.store.mark_api_call(time.strftime("%Y-%m-%d"))
                    self.history.append(Turn(role="assistant", text=reply))
                    self.store.record_decision({"mode": "chat", "api_called": True,
                                               "user": user_text[:80], "reply": reply[:120]})
                except Exception as exc:
                    _dbg(f"记账失败(忽略): {exc!r}")
                    self.log_line.emit(f"会话记账失败(已忽略): {exc}")

                # ---- 语音（被动回应，失败不致命） ----
                try:
                    allowed, reason = self.gate.allow("reactive", time.monotonic())
                    if allowed:
                        self.gate.mark_reactive(time.monotonic())
                        self.state_changed.emit("work")
                        base_id = f"chat-{int(time.monotonic() * 1000)}"
                        sentences = split_sentences(reply)
                        for index, sentence in enumerate(sentences):
                            stop_event = self.engine.new_stop_event()
                            self.player.enqueue_stream(f"{base_id}-{index}",
                                                       self.engine.stream(sentence, stop_event))
                        self.state_changed.emit("talk")
                        if not sentences:        # 有字却切不出句子：只会有气泡没声音
                            _dbg(f"reply 切句为 0（不会出声）: {reply[:40]!r}")
                    else:
                        self.log_line.emit(f"会话被防连点拦截[{reason}]")
                except Exception as exc:
                    _dbg(f"语音失败(忽略): {exc!r}")
                    self.log_line.emit(f"会话语音失败(已忽略): {exc}")

                if self._turn >= MAX_TURNS or time.monotonic() - self._started_at > MAX_SESSION_SEC:
                    break
        except Exception as exc:  # noqa: BLE001 任何一步异常都不能让 mode 卡在 chat
            _dbg(f"会话线程异常（已兜底收尾，唤醒监听不受影响）: {exc!r}\n{traceback.format_exc()}")
            self.log_line.emit(f"会话线程异常（已兜底收尾，唤醒监听不受影响）: {exc}")
            self.log_line.emit(traceback.format_exc())
        finally:
            # 关键兜底：无论正常结束还是异常崩掉，都必须恢复 observe 并发出
            # 结束信号。否则 mode=chat + 'session' 暂停原因会永久泄漏，唤醒监听
            # 被暂停——表现就是"聊过一次就再也喊不醒她"（用户实测根因）。
            try:
                self.store.set_mode("observe")
            except Exception as exc:
                _dbg(f"set_mode(observe) 失败: {exc!r}")
            try:
                self.finished_session.emit()
            except Exception as exc:
                _dbg(f"finished_session.emit 失败: {exc!r}")
