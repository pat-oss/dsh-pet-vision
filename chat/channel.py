"""chat/channel.py — ChatChannel：宠物窗口输入框 → ChatSession 的桥。

同一时刻最多一个 ChatSession（串行，防并发把 API 打爆）；
会话存续期间的新输入提交给当前会话。
会话空闲 idle_finish_sec 秒后自动收尾（否则 mode=chat 一直挂起唤醒监听）。
"""

from __future__ import annotations

from chat.session import ChatSession, IDLE_FINISH_SEC


class ChatChannel:
    def __init__(self, judge, store, gate, player, engine, frame_source=None,
                 idle_finish_sec: int = IDLE_FINISH_SEC, on_ended=None) -> None:
        self._judge = judge
        self._store = store
        self._gate = gate
        self._player = player
        self._engine = engine
        # 视觉功能开启时的截图回调；None = 不带图（省 token）
        self._frame_source = frame_source
        self._idle_finish_sec = idle_finish_sec
        self._session: ChatSession | None = None
        # 会话结束回调：在 session.start() 之前连接，消除竞态。
        # 用途是释放"会话进行中"对唤醒监听的暂停——见 session.py / wake.py。
        # 关键：必须在 start() 前连好，否则会话若很快结束（网络 401/内部异常），
        # finished_session 会在连接建立前发射，回调永远错过 → 唤醒被永久暂停。
        self._on_ended = on_ended

    @property
    def active(self) -> bool:
        return self._session is not None and self._session.isRunning()

    def submit(self, text: str) -> ChatSession | None:
        text = (text or "").strip()
        if not text:
            return None
        if self.active and self._session is not None:
            self._session.submit(text)
            return self._session
        session = ChatSession(self._judge, self._store, self._gate,
                              self._player, self._engine, text,
                              frame_source=self._frame_source,
                              idle_finish_sec=self._idle_finish_sec)
        # 必须先连好"结束"信号再 start()：start() 后线程可能立刻跑完并发射
        # finished_session，晚一拍连接就会错过（竞态 → 唤醒监听永久暂停）。
        if self._on_ended is not None:
            session.finished_session.connect(self._on_ended)
        session.finished_session.connect(self._on_finished)
        self._session = session
        session.start()
        return session

    def _on_finished(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            session.wait(3000)
