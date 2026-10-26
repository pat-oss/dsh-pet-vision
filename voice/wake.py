"""voice/wake.py — 唤醒词监听：像手机助手一样一直听，听到称呼就接话。

链路：
    麦克风 16kHz int16 → 能量门（静音不喂识别器，省 CPU）
    → Vosk 流式 ASR → 滑动窗口关键词匹配（同音/错字容错）
    → 唤醒 → 捕获后续指令（静默超时收尾）→ 信号交还上层

匹配为什么有两层（字符级 + 拼音级）：
    Vosk small 模型对短词经常两个都听错——实测唤醒词「幼楚」(you chu)
    被识别成「标出」(biao chu)，字符编辑距离 2，超出 2 字词只能容 1 处错的
    上限，永远匹配不上。而 ASR 最常见的错误是同音不同字（幼→有、楚→出），
    音节序列往往完全正确。拼音级滑窗按音节比对，堵住这一类漏识别。

防自触发（宠物自己说话时麦克风同样能听到）：
    播放中 / 会话进行中 / 专注模式 → 暂停拾音，恢复时重置识别器。
    暂停原因用 set 管理，多个来源互不干扰。

    暂停分两类，语义完全不同（踩过的坑）：
    - **接管类**（session/dnd）：对话被别的东西接管，唤醒后的半个指令
      直接丢弃；
    - **播放类**（speaking）：只是她自己正在发声。这类暂停**不能**丢弃
      指令捕获——唤醒后要先播"我在，主人。"再听指令，若播个应答就把
      指令窗口毁了，用户每次唤醒后都得重叫一次名字。播放暂停期间
      指令截止时间同步顺延（暂停不计入用户的思考时间）。

线程模型：QThread 独占麦克风采集与 ASR；只通过 Signal 与 Qt 主线程通信。

音频设备自愈合（实测踩坑）：
    MME 输入流会被瞬时故障打断——别的程序抢占麦克风、音频栈重初始化
    （本机还挂着麦克风 AI 降噪）、驱动抽风——read() 直接抛异常。
    早期版本遇到异常就 break 退出线程：**唤醒监听永久假死**，症状是
    "第一次叫有回应，之后再叫没反应"（用户实测反馈，日志里留着
    「麦克风读取中断，唤醒监听停止。」）。现在改为：读异常 / 读卡死
    （read_available 长时间不增长）都走「退避重开流」，恢复后重置捕获
    状态当新的一轮听，绝不永久退出。重开间隔 1s→10s 封顶，全程留痕。
"""

from __future__ import annotations

import json
import re
import time
import traceback
from collections import deque
from functools import lru_cache
from pathlib import Path

import numpy as np
from PySide6.QtCore import QThread, Signal

try:  # 拼音匹配依赖；缺失时自动降级为纯字符匹配（不影响运行）
    from pypinyin import lazy_pinyin as _lazy_pinyin
except ImportError:  # pragma: no cover - 打包遗漏等情况
    _lazy_pinyin = None

# 只保留中英数字（唤醒词匹配用；ASR 输出常带标点/空格）
_KEEP = re.compile(r"[\u4e00-\u9fff0-9a-zA-Z]+")

# 会"接管对话"的暂停原因：出现即丢弃唤醒后的半个指令。
# 播放类（speaking）不在其列——见模块文档。
_ABORT_COMMAND_REASONS = {"session", "dnd"}

# 音频设备自愈合参数（见模块文档「音频设备自愈合」）
STALL_SEC = 8.0            # 多久没有新音频帧到达视为设备卡死
REOPEN_BACKOFF_MAX = 10.0  # 重开退避上限（秒）
POLL_MS = 10               # 轮询间隔（毫秒）


def normalize_text(text: str) -> str:
    """去空白标点、统一小写。"""
    return "".join(_KEEP.findall((text or "").lower()))


def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离（唤醒词很短，O(n*m) 足够快）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def contains_wake(phrase: str, text: str, tolerance: int = 1) -> bool:
    """text 里是否"包含"phrase：先归一化精确子串，再字符级滑窗编辑距离
    容错，最后拼音音节级滑窗容错。

    容错覆盖 ASR 常见错误：同音错字（小鲸于/小于鱼）、多字少字。
    只取长度 n 与 n+1 的滑窗——不含 n-1：任何 n 字词的 2 字子串
    距离都是 1，放行 n-1 会把"小鱼"这类正常词误判成"小鲸鱼"。
    拼音级滑窗处理两个字都听错的短词（实测「幼楚」→「标出」）。
    tolerance 为允许的编辑字数，按唤醒词长度在配置装载时放大。
    """
    p = normalize_text(phrase)
    t = normalize_text(text)
    if not p or not t:
        return False
    if p in t:
        return True
    max_edits = max(1, int(tolerance))
    for length in (len(p), len(p) + 1):
        if length > len(t):
            continue
        for start in range(len(t) - length + 1):
            if edit_distance(p, t[start:start + length]) <= max_edits:
                return True
    # 拼音音节级：you-chu 能匹配 biao-chu / you-chu / you-yu 等同音错字
    if _lazy_pinyin is not None:
        ps = syllables_of(p)
        if ps:
            ts = syllables_of(t)
            for length in (len(ps), len(ps) + 1):
                if length > len(ts):
                    continue
                for start in range(len(ts) - length + 1):
                    if _token_distance(ps, ts[start:start + length]) <= max_edits:
                        return True
    return False


def auto_tolerance(phrase: str, configured: int = 1) -> int:
    """≤4 字唤醒词容 1 处错，>4 字容 2 处；配置值只会放大不会缩小。"""
    n = len(normalize_text(phrase))
    return max(1, int(configured), 2 if n > 4 else 1)


@lru_cache(maxsize=2048)
def syllables_of(text: str) -> tuple[str, ...]:
    """文本 → 拼音音节序列（无声调）。pypinyin 缺失时返回空元组。"""
    if _lazy_pinyin is None:
        return ()
    return tuple(_lazy_pinyin(normalize_text(text)))


def _token_distance(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """音节序列的 Levenshtein 距离（音节作为原子token）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ta in enumerate(a, 1):
        cur = [i]
        for j, tb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ta != tb)))
        prev = cur
    return prev[-1]


def rms_int16(block: np.ndarray) -> float:
    """int16 PCM 块的 RMS（0..1）。"""
    if block.size == 0:
        return 0.0
    x = np.asarray(block, dtype=np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x)))


def _ascii_model_path(model_dir: Path) -> str:
    """把模型目录转成纯 ASCII 路径。

    Vosk 底层是 Kaldi 的 C++ 代码，按 ANSI 窄字节拼路径开文件；
    含中文的绝对路径（中文 Windows 用户名、本项目的 F:\\小鲸鱼\\…）会直接
    "Failed to create a model"。办法是建一个纯 ASCII 的目录联接
    （NTFS junction，不需要管理员权限）指到真实目录，零拷贝。
    """
    raw = str(model_dir)
    if raw.isascii():
        return raw
    import os
    import subprocess
    import tempfile

    link = Path(tempfile.gettempdir()) / "little-whale-vosk"
    probe = link / "am" / "final.mdl"
    if not probe.is_file():
        try:
            if link.exists() or link.is_symlink():
                os.rmdir(link)              # 清理失效/过期联接
        except OSError:
            pass
        try:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), raw],
                           check=True, capture_output=True,
                           creationflags=0x08000000)   # CREATE_NO_WINDOW
        except Exception as exc:  # noqa: BLE001
            print(f"[wake] 建立 ASCII 联接失败({exc})，回退原路径")
            return raw
    return str(link) if probe.is_file() else raw


class WakeListener(QThread):
    """唤醒词监听线程。

    信号：
        woke = Signal(str)      # 匹配到唤醒词（参数=配置的唤醒词）
        command = Signal(str)   # 唤醒后捕获到的指令文本
        log_line = Signal(str)  # 人类可读日志
    """

    woke = Signal(str)
    command = Signal(str)
    log_line = Signal(str)

    IDLE = "idle"          # 等唤醒
    COMMAND = "command"    # 唤醒后捕获指令

    def __init__(self, cfg, store=None) -> None:
        super().__init__()
        self.cfg = cfg
        self.store = store
        self._stopped = False
        self._paused_reasons: set[str] = set()
        self._state = self.IDLE
        # 匹配窗口：最近若干片段（partial 只占一个"活槽位"，避免同句重复堆叠）
        self._fragments: deque[str] = deque(maxlen=max(2, int(cfg.window_fragments)))
        self._partial_live = False
        # 指令捕获
        self._cmd_finals: list[str] = []
        self._cmd_partial = ""
        self._last_speech_at = 0.0
        self._cmd_deadline = 0.0
        self._cmd_ignore_until = 0.0      # 唤醒后短暂忽略（滤掉唤醒词自己的尾音）
        self._cooldown_until = 0.0
        self._silence_blocks = 0
        self._was_paused = False
        self._paused_since = 0.0            # 本次暂停的起点（恢复时顺延指令截止）
        self._last_diag_at = 0.0            # 诊断日志限流（听到什么）
        self._last_audio_at = 0.0           # 最近一次拿到音频帧（卡死检测）
        # 运行时对象（run 内创建）
        self._rec = None
        self._stream = None
        self._blocksize = max(1600, int(cfg.sample_rate) // 10)

    # ---- 暂停管理（主线程调用；set 操作原子，无需额外锁） ----

    @property
    def state(self) -> str:
        return self._state

    def set_paused(self, reason: str, paused: bool) -> None:
        if paused:
            self._paused_reasons.add(reason)
            # 只有"接管对话"的暂停才丢弃半个指令；她自己发声（speaking）
            # 不能毁掉唤醒后的指令捕获窗口
            if reason in _ABORT_COMMAND_REASONS and self._state == self.COMMAND:
                self._abort_command()
        else:
            self._paused_reasons.discard(reason)

    def _is_paused(self) -> bool:
        if self._paused_reasons:
            return True
        # 专注模式 / 会话进行中也不拾音
        return bool(self.store is not None and self.store.state.mode != "observe")

    # ---- 主循环 ----

    def run(self) -> None:
        backoff = 1.0
        ever_ready = False
        while not self._stopped:
            # ---- 流不可用：启动或重开（退避重试，绝不永久放弃）----
            if self._stream is None:
                ok, err, tb = self._setup_safe()
                if not ok:
                    if not ever_ready:
                        # 首次启动失败：完整栈信息（配置/设备问题定位用）
                        self.log_line.emit(f"唤醒监听启动失败: {err}\n{tb}"
                                           f"（不影响点击/打字对话；将重试）")
                    else:
                        self.log_line.emit(f"麦克风重开失败（{err}），"
                                           f"{backoff:.0f}s 后再试")
                    if not self._sleep_stoppable(backoff):
                        break
                    backoff = min(backoff * 2, REOPEN_BACKOFF_MAX)
                    continue
                backoff = 1.0
                self._last_audio_at = time.monotonic()
                if ever_ready:
                    # 设备恢复：丢半截指令、清冷却，当新的一轮听
                    self._reset_after_reopen()
                    self.log_line.emit("麦克风已恢复，继续听。")
                else:
                    ever_ready = True
                    self.log_line.emit(
                        f"唤醒词监听中——说「{self.cfg.phrase}」叫我。")
                continue

            # ---- 流可用：取一块音频（轮询 read_available，不用阻塞 read）----
            # 阻塞 read 在设备被抢占后会永久挂住（静默假死，比抛异常更隐蔽）；
            # 轮询 + 卡死检测让"挂住"也能被发现并重开。
            try:
                if self._stream.read_available < self._blocksize:
                    if time.monotonic() - self._last_audio_at > STALL_SEC:
                        raise RuntimeError("音频流无新数据（设备卡死）")
                    self.msleep(POLL_MS)
                    continue
                data, _overflowed = self._stream.read(self._blocksize)
                self._last_audio_at = time.monotonic()
            except Exception as exc:  # noqa: BLE001 设备被抢占/拔出/卡死 → 重开
                self.log_line.emit(f"麦克风异常（{exc}），正在重开…")
                self._teardown()
                continue

            try:
                self._process(data)
            except Exception as exc:  # noqa: BLE001 单帧异常不杀线程
                self.log_line.emit(f"唤醒识别异常: {exc}")
                self.msleep(200)
        self._teardown()

    def _setup_safe(self) -> tuple[bool, str, str]:
        """尝试建流。返回 (成功?, 错误信息, 栈信息)——栈必须在 except 块内取。"""
        try:
            self._setup()
            return True, "", ""
        except Exception as exc:  # noqa: BLE001 模型缺失/设备占用等
            self._teardown()
            return False, str(exc), traceback.format_exc()

    def _sleep_stoppable(self, seconds: float) -> bool:
        """分段睡眠保证 stop() 及时打断；返回 False = 已被要求停止。"""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stopped:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.msleep(min(POLL_MS * 5, int(remaining * 1000) + 1))
        return not self._stopped

    def _reset_after_reopen(self) -> None:
        """设备重开后重置捕获状态：半截指令接不回去，清空当新一轮。"""
        self._abort_command()
        self._was_paused = False
        self._cooldown_until = 0.0
        self._silence_blocks = 0
        self._last_diag_at = 0.0

    def _setup(self) -> None:
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        model_dir = Path(self.cfg.model_dir)
        if not model_dir.is_dir():
            raise FileNotFoundError(
                f"唤醒模型目录不存在: {model_dir}（下载方式见 README「唤醒词」一节）")
        self._rec = KaldiRecognizer(Model(_ascii_model_path(model_dir)),
                                    self.cfg.sample_rate)
        self._stream = sd.InputStream(samplerate=self.cfg.sample_rate, channels=1,
                                      dtype="int16", blocksize=self._blocksize)
        self._stream.start()

    def _teardown(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
        except Exception:  # noqa: BLE001
            pass
        self._stream = None
        self._rec = None

    def stop(self) -> None:
        self._stopped = True

    # ---- 单帧处理 ----

    def _process(self, block: np.ndarray) -> None:
        now = time.monotonic()

        if self._is_paused():
            if not self._was_paused:
                self._paused_since = now
            self._was_paused = True
            return                        # 不拾音：省 CPU 也防听到自己的声音
        if self._was_paused:
            self._was_paused = False
            paused_for = max(0.0, now - self._paused_since)
            # 暂停期间不消耗用户的指令时间：截止时间与"最后听到人声"
            # 都顺延等长的一段，否则应答播完（>silence_ms 时）会立刻
            # 把才说了一半的指令收掉
            if self._state == self.COMMAND and paused_for > 0:
                self._cmd_deadline += paused_for
                self._last_speech_at += paused_for
            # 恢复拾音后留一点冷静期：宠物自己的尾音/房间余音不算指令
            self._cooldown_until = max(self._cooldown_until, now + 1.5)
            self._reset_recognizer()

        self._note_silence(now)            # 每帧都检查指令收尾

        # 能量门：静音不喂识别器（Vosk 在静音块上仍有计算开销）
        if rms_int16(block) < self.cfg.energy_threshold:
            self._silence_blocks += 1
            if self._silence_blocks > self.cfg.hangover_blocks:
                return
        else:
            self._silence_blocks = 0

        pcm = np.asarray(block, dtype=np.int16).tobytes()
        if self._rec.AcceptWaveform(pcm):
            self._push(json.loads(self._rec.Result()).get("text", ""), final=True, now=now)
        else:
            partial = json.loads(self._rec.PartialResult()).get("partial", "")
            # 诊断：限流记录能量门之上听到的内容（"喊了没反应"时看这行）
            if partial and now - self._last_diag_at > 3.0:
                self._last_diag_at = now
                self.log_line.emit(f"听到: {partial}")
            self._push(partial, final=False, now=now)

    def _note_silence(self, now: float) -> None:
        """指令收尾判断：说完静默 silence_ms 即收；超时也收。"""
        if self._state != self.COMMAND:
            return
        has_text = bool(self._cmd_finals) or bool(self._cmd_partial)
        if has_text:
            if now - self._last_speech_at >= self.cfg.silence_ms / 1000.0:
                self._finish_command()
        elif now >= self._cmd_deadline:
            self._finish_command()

    # ---- 文本进入（音频路径与测试路径共用） ----

    def _push(self, text: str, final: bool, now: float) -> None:
        text = (text or "").strip()
        if text:
            self._last_speech_at = now
        if final:
            if text:
                self._fragments.append(text)
            self._partial_live = False
        else:
            if not text:
                return
            if self._partial_live and self._fragments:
                self._fragments[-1] = text      # partial 覆写上一个 partial 槽
            else:
                self._fragments.append(text)
                self._partial_live = True

        if self._state == self.IDLE:
            if now < self._cooldown_until:
                return
            if contains_wake(self.cfg.phrase, "".join(self._fragments),
                             auto_tolerance(self.cfg.phrase, self.cfg.match_tolerance)):
                self._enter_command(now)
        else:  # COMMAND：累积指令
            if now < self._cmd_ignore_until:
                return          # 唤醒词尾音（如"小鲸鱼"最后一个字的残留）不算指令
            if final:
                if text:
                    self._cmd_finals.append(text)
                self._cmd_partial = ""
            else:
                self._cmd_partial = text

    # ---- 状态迁移 ----

    def _enter_command(self, now: float) -> None:
        self._state = self.COMMAND
        self._cmd_finals = []
        self._cmd_partial = ""
        self._last_speech_at = now
        self._cmd_deadline = now + self.cfg.command_timeout_sec
        self._cmd_ignore_until = now + 0.4      # 滤掉唤醒词自身的尾音
        self._reset_recognizer()
        self._fragments.clear()
        self._partial_live = False
        self.log_line.emit(f"唤醒词命中「{self.cfg.phrase}」，请说指令…")
        self.woke.emit(self.cfg.phrase)

    def _finish_command(self) -> None:
        text = ("".join(self._cmd_finals) + self._cmd_partial).strip()
        self._abort_command()
        self._cooldown_until = time.monotonic() + self.cfg.cooldown_sec
        # 少于 2 个有效字符视为杂音（单个语气词/误识别），不当作指令
        if len(normalize_text(text)) >= 2:
            self.command.emit(text)
        else:
            # 唤醒后 3s 没听到有效指令：本轮结束（应答语在唤醒时已播过）。
            # 必须留痕——"喊了没反应"时这行能区分"没听到"还是"听到了没指令"。
            self.log_line.emit(f"唤醒后 {self.cfg.silence_ms // 1000}s 内没有听到指令，本轮结束")

    def _abort_command(self) -> None:
        """丢弃进行中的指令捕获，回到等待唤醒。"""
        self._state = self.IDLE
        self._cmd_finals = []
        self._cmd_partial = ""
        self._cmd_ignore_until = 0.0
        self._fragments.clear()
        self._partial_live = False

    def _reset_recognizer(self) -> None:
        if self._rec is not None:
            try:
                self._rec.Reset()
            except Exception:  # noqa: BLE001
                pass

    # ---- 测试辅助：绕过音频直喂文本 ----

    def feed(self, text: str, final: bool = False) -> list[str]:
        """把一段识别文本喂进状态机，返回本次触发的事件名（单测用）。"""
        events: list[str] = []
        if self._state == self.IDLE:
            self._push(text, final=final, now=time.monotonic())
            if self._state == self.COMMAND:
                events.append("woke")
        else:
            self._push(text, final=final, now=time.monotonic())
        return events

    def tick_silence(self) -> None:
        """模拟一个静默帧到达（单测用）。"""
        self._note_silence(time.monotonic())
