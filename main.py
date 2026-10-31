"""main.py — 装配入口：单进程串起 截屏 → DeepSeek → TTS → 宠物窗口 → 唤醒监听。

运行：
    python main.py                # 正常启动（首次会弹设置窗要 API 密钥）
    python main.py --setup        # 强制打开设置窗（改密钥/唤醒词/音色）
    python main.py --self-test    # 离线自检：截屏/dHash/节流门/TTS 合成，不起 GUI

模块装配走 core/container.py（依赖注入）；本文件只做"接线"。
路径约定见 core/paths.py：开发模式资源与数据都在仓库内；打包后资源只读、
数据写 %LOCALAPPDATA%\\little-whale-pet。
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time
from pathlib import Path

import yaml
from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication

from chat.channel import ChatChannel
from core.container import Container
from core.gate import SpeechGate
from core import paths
from core.prefs import deep_merge, load_prefs, save_prefs
from core.prompts import configure_persona
from core.secrets import get_api_key, set_api_key
from core.memory import NoteStore
from core.state import StateStore
from core.usage import UsageStore
from core.types import (ChatConfig, GateConfig, JudgeConfig, ObserveConfig,
                        PetConfig, ProactiveConfig, VoiceConfig, WakeConfig)
from observe.differ import FrameDiffer
from observe.judge import VisionJudge, encode_frame
from observe.loop import ObserveLoop
from observe.screen import ScreenSource
from pet.app import PetWindow
from pet.setup import SetupDialog
from voice.cache import UtteranceCache
from voice.moss import MossNanoEngine
from voice.player import SpeechPlayer
from voice.router import VoiceRouter
from voice.wake import WakeListener

# 唤醒后的应答语（{addr} = 用户配置的称呼）
ACK_LINES = ["我在，{addr}。", "嗯？{addr}叫我吗～", "来啦来啦，{addr}想说什么？"]


class _Tee:
    """把 stdout/stderr 同时写一份到日志文件（windowed 打包后排错用）。"""

    def __init__(self, *streams) -> None:
        self._streams = streams
        self._lock = __import__("threading").Lock()

    def write(self, data: str) -> int:
        with self._lock:
            for s in self._streams:
                try:
                    s.write(data)
                except Exception:  # noqa: BLE001 控制台已关闭等情况
                    pass
        return len(data)

    def flush(self) -> None:
        with self._lock:
            for s in self._streams:
                try:
                    s.flush()
                except Exception:  # noqa: BLE001
                    pass


def setup_file_logging() -> None:
    """打包运行时把输出镜像到 data/run.log（保留最近约 1MB）。"""
    if not paths.is_frozen():
        return
    try:
        log_path = paths.data_root() / "run.log"
        if log_path.is_file() and log_path.stat().st_size > 1_000_000:
            log_path.unlink()
        stream = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = _Tee(sys.__stdout__, stream)
        sys.stderr = _Tee(sys.__stderr__, stream)
    except Exception:  # noqa: BLE001 日志失败不影响主流程
        pass


def load_config(path: Path, data_dir: Path):
    """读 config.yaml 并用 data/user_prefs.json 深合并覆盖。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    data = deep_merge(data, load_prefs(data_dir))
    pet = PetConfig(**data.get("pet", {}))
    observe = ObserveConfig(**data.get("observe", {}))
    gate_cfg_data = dict(data.get("gate", {}))
    blacklist = gate_cfg_data.pop("title_blacklist", None)
    gate = GateConfig(**gate_cfg_data)
    if blacklist is not None:
        gate.title_blacklist = tuple(blacklist)
    judge = JudgeConfig(**data.get("model", {}))
    # 主动互动频率是唯一真源：gate 的每小时硬上限从它推导，避免两处配置打架
    proactive = ProactiveConfig(**data.get("proactive", {}))
    gate.max_proactive_per_hour = max(1, proactive.resolve()[1])
    voice_data = dict(data.get("voice", {}))
    voice = VoiceConfig(**voice_data)
    voice.model_dir = (paths.app_root() / voice.model_dir).resolve()
    voice.voices_dir = (paths.app_root() / voice.voices_dir).resolve()
    wake_data = dict(data.get("wake", {}))
    wake = WakeConfig(**wake_data)
    wake.model_dir = (paths.app_root() / wake.model_dir).resolve()
    chat = ChatConfig(**data.get("chat", {}))
    persona = data.get("persona", {})
    return (pet, observe, gate, judge, voice, wake, chat, proactive,
            str(persona.get("name", "小鲸鱼")),
            str(persona.get("prompt_version", "v2")),
            str(persona.get("style", "")),
            str(persona.get("user_address", "主人")))


def self_test(judge_cfg: JudgeConfig, voice_cfg: VoiceConfig, observe_cfg: ObserveConfig,
              gate_cfg: GateConfig) -> int:
    """离线自检：不依赖 GUI 与 API key 的冒烟测试。"""
    print("== 1. 截屏 + dHash ==")
    screen = ScreenSource()
    frame = screen.capture()
    differ = FrameDiffer()
    h1 = differ.dhash(frame)
    h2 = differ.dhash(frame)
    print(f"   分辨率 {frame.size}，同帧距离 {differ.distance(h1, h2)}（应为 0）")

    print("== 2. 节流门（时间注入） ==")
    store = StateStore(paths.data_root() / "state.json",
                       paths.data_root() / "decisions.jsonl")
    gate = SpeechGate(gate_cfg, store)
    now = time.monotonic()
    store.state.last_spoken_at = now  # 刚说过话
    store.save()
    print(f"   30s 内再请求 → {gate.allow('proactive', now)}（应为 (False, 'throttle')）")
    print(f"   被动请求 → {gate.allow('reactive', now)}（应为 (True, '')）")

    print("== 3. MOSS-TTS-Nano 合成 ==")
    engine = MossNanoEngine(voice_cfg.model_dir, voice_cfg.voices_dir,
                           voice_cfg.voice_name, voice_cfg.threads,
                           work_dir=paths.data_root())
    started = time.monotonic()
    engine.load()
    print(f"   模型加载 {time.monotonic() - started:.1f}s")
    text = "主人你好呀，我是小鲸鱼，今天也要元气满满地陪你哦。"
    started = time.monotonic()
    audio = engine.synthesize(text)
    took = time.monotonic() - started
    import soundfile as sf
    out = paths.data_root() / "selftest_tts.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out, audio, engine.sample_rate)
    print(f"   合成 {len(text)} 字 → {audio.shape[0] / engine.sample_rate:.2f}s 音频"
          f"（{engine.sample_rate}Hz {audio.shape[1]}ch），耗时 {took:.1f}s")
    print(f"   已存 {out}")

    print("== 4. DeepSeek 客户端装配 ==")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "") or get_api_key(paths.data_root())
    if api_key:
        judge = VisionJudge(judge_cfg, api_key)
        print(f"   客户端 OK（model={judge_cfg.vision_model}，json_mode={judge._json_mode}）")
        print("   注意：真实 API 调用未在自检中执行（避免花费），启动后由观察循环触发。")
    else:
        print("   未设置 DEEPSEEK_API_KEY 且无已存密钥，跳过（首次启动时会弹设置窗）")
    print("\n自检完成。")
    return 0


class PetApp:
    """把装配过程包成对象，避免 main() 里几十个局部变量互相引用。"""

    def __init__(self, args) -> None:
        self.args = args
        self.data_dir = paths.data_root()
        (self.pet_cfg, self.observe_cfg, self.gate_cfg, self.judge_cfg,
         self.voice_cfg, self.wake_cfg, self.chat_cfg, self.proactive_cfg,
         self.name, self.prompt_version, self.persona_style, self.user_address) = \
            load_config(args.config, self.data_dir)
        configure_persona(self.name, self.persona_style, self.user_address)

        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.container = Container()
        self.store = StateStore(self.data_dir / "state.json",
                                self.data_dir / "decisions.jsonl")
        # 用量统计（分小时/天/月/总计 + 本次启动以来）。
        # 磁盘位置与 state 同级，清隐私时一并处理。
        self.usage = UsageStore(self.data_dir / "usage.json")
        # 持久笔记：跨会话记住关于主人的事实（明文、可读、可精确删除）
        self.memory = NoteStore(self.data_dir / "notes.jsonl")
        # 退出前兜住还没落盘的最后一段记账
        self.app.aboutToQuit.connect(self.usage.flush)
        self.screen = ScreenSource()
        self.differ = FrameDiffer()
        self.gate = SpeechGate(self.gate_cfg, self.store)
        self.container.register("store", lambda: self.store)
        self.container.register("gate", lambda: self.gate)
        self.container.register("screen", lambda: self.screen)
        self.container.register("differ", lambda: self.differ)
        self.container.register("usage", lambda: self.usage)
        self.container.register("memory", lambda: self.memory)

        self._setup_offered = False
        # 记忆整理是否在跑（同一时刻只允许一个，免得几次会话收尾叠着打 API）
        self._consolidating = False
        # 后台线程的强引用（QThread 被 GC 掉而线程仍在跑 = 直接崩，见 _warm_async）
        self._bg_threads: list = []
        # 视觉功能开关（托盘可切）。默认开：与 observe 模式一致，
        # 免打扰开启时由 on_dnd 联动关闭。
        self._vision_on = True
        # 装配期才创建的对象，先占位避免提前访问报 AttributeError
        self.engine = None
        self.player = None
        self.judge = None
        self.channel = None
        self.loop = None
        self.wake = None
        self.window = None

    # ---- 首次设置 ----

    def resolve_api_key(self) -> str:
        """环境变量优先，其次已存密钥，都没有则弹设置窗。"""
        key = os.environ.get("DEEPSEEK_API_KEY", "") or get_api_key(self.data_dir)
        if key:
            return key
        if self.open_setup(first_run=True):
            return get_api_key(self.data_dir)
        return ""

    def open_setup(self, first_run: bool = False) -> bool:
        """打开设置窗；保存成功返回 True。可被托盘菜单/密钥失效重复调用。"""
        dlg = SetupDialog(voices_dir=self.voice_cfg.voices_dir,
                          current_key=get_api_key(self.data_dir),
                          current_wake=self.wake_cfg.phrase,
                          current_address=self.user_address,
                          current_voice=self.voice_cfg.voice_name,
                          current_max_tokens=self.judge_cfg.max_tokens_chat,
                          current_proactive_mode=self.proactive_cfg.mode,
                          current_proactive_min=self.proactive_cfg.min_per_hour,
                          current_proactive_max=self.proactive_cfg.max_per_hour,
                          current_fullscreen_quiet=self.gate_cfg.dnd_fullscreen,
                          current_auto_memory=self.chat_cfg.auto_consolidate,
                          usage=self.usage, memory=self.memory,
                          first_run=first_run,
                          parent=getattr(self, "window", None))
        if dlg.exec() != SetupDialog.DialogCode.Accepted:
            return False
        self._setup_offered = False
        self._apply_values(dlg.values())
        return True

    def _apply_values(self, values: dict) -> None:
        """设置窗保存：密钥进 secrets，其余进 user_prefs，并尽量热生效。"""
        set_api_key(self.data_dir, values["api_key"])
        prefs = load_prefs(self.data_dir)
        prefs["wake"] = {**(prefs.get("wake") or {}), "phrase": values["wake_phrase"]}
        prefs["persona"] = {**(prefs.get("persona") or {}),
                            "user_address": values["user_address"]}
        prefs["voice"] = {**(prefs.get("voice") or {}), "voice_name": values["voice_name"]}
        # 输出上限（None = 不限制）：观察与会话共用一个值，用户填一次就够
        max_tokens = values.get("max_tokens")
        prefs["model"] = {**(prefs.get("model") or {}),
                          "max_tokens_chat": max_tokens,
                          "max_tokens_observe": max_tokens}
        # 主动互动频率（到点搭话 + 画面变动仍照旧触发）
        proactive = {"mode": values.get("proactive_mode", "low"),
                     "min_per_hour": int(values.get("proactive_min", 5)),
                     "max_per_hour": int(values.get("proactive_max", 10))}
        prefs["proactive"] = {**(prefs.get("proactive") or {}), **proactive}
        # 全屏时是否完全安静（看视频/玩游戏时要不要打扰）
        fullscreen_quiet = bool(values.get("fullscreen_quiet", False))
        prefs["gate"] = {**(prefs.get("gate") or {}), "dnd_fullscreen": fullscreen_quiet}
        # 自动整理记忆（会话结束后自己提炼要点）
        auto_memory = bool(values.get("auto_memory", True))
        prefs["chat"] = {**(prefs.get("chat") or {}), "auto_consolidate": auto_memory}
        save_prefs(self.data_dir, prefs)
        # 热生效：唤醒词/音色/人设称呼/输出上限
        old_voice, old_addr = self.voice_cfg.voice_name, self.user_address
        self.wake_cfg.phrase = values["wake_phrase"]
        self.user_address = values["user_address"]
        self.voice_cfg.voice_name = values["voice_name"]
        self.judge_cfg.max_tokens_chat = max_tokens
        self.judge_cfg.max_tokens_observe = max_tokens
        if self.judge is not None and self.judge.cfg is not self.judge_cfg:
            self.judge.cfg = self.judge_cfg      # 防御：确保判官吃到新上限
        # 主动互动频率热生效：gate 的小时上限同步 + 观察循环重抽下一次时间
        self.proactive_cfg.mode = proactive["mode"]
        self.proactive_cfg.min_per_hour = proactive["min_per_hour"]
        self.proactive_cfg.max_per_hour = proactive["max_per_hour"]
        self.gate_cfg.max_proactive_per_hour = max(1, self.proactive_cfg.resolve()[1])
        self.gate_cfg.dnd_fullscreen = fullscreen_quiet
        if self.loop is not None:
            self.loop.proactive_cfg = self.proactive_cfg
            self.loop.reschedule_proactive()
        # 自动整理记忆热生效：开关关掉时把回调摘掉，下一次会话收尾就不再调 API
        self.chat_cfg.auto_consolidate = auto_memory
        if self.channel is not None:
            self.channel._on_transcript = (self._schedule_consolidation
                                           if auto_memory else None)
        if self.engine is not None:
            self.engine.set_voice(values["voice_name"])
        # 音色或称呼变了 → 缓存作废（音频里带着旧音色/旧称呼），后台重预热
        if values["voice_name"] != old_voice or values["user_address"] != old_addr:
            self._revoice(values["voice_name"])
            self._warm_async()
        configure_persona(self.name, self.persona_style, self.user_address)
        tok_desc = "不限制" if max_tokens is None else str(max_tokens)
        lo, hi = self.proactive_cfg.resolve()
        pro_desc = "不主动互动" if hi <= 0 else f"{lo}–{hi} 次/小时"
        pro_desc += "，全屏静音" if fullscreen_quiet else "，全屏也说话"
        print(f"[setup] 已保存：唤醒词「{values['wake_phrase']}」，"
              f"称呼「{values['user_address']}」，音色 {values['voice_name']}，"
              f"最大输出 token={tok_desc}，主动互动={pro_desc}")

    # ---- 装配 ----

    @property
    def _ack_texts(self) -> list[str]:
        """唤醒应答语（含用户称呼）——预热缓存与播报必须用同一批文本。"""
        return [line.format(addr=self.user_address) for line in ACK_LINES]

    def _warm_ack_cache(self) -> None:
        """TTS 加载完成后预热唤醒应答：本机合成慢于实时，预热后唤醒即出声。"""
        if self.engine is None or not getattr(self.engine, "loaded", False):
            return
        try:
            cache = UtteranceCache(paths.data_root() / "tts_cache.npz",
                                   f"moss-nano-v4:{self.voice_cfg.voice_name}")
            cache.load()
            self.engine.attach_cache(cache)
            made = self.engine.warm(self._ack_texts)
            if made:
                print(f"[tts] 已预热 {made} 条唤醒应答（命中缓存的出声 0 延迟）")
        except Exception as exc:  # noqa: BLE001 预热失败不影响主流程
            print(f"[warn] 应答预热失败: {exc}")

    def _warm_async(self) -> None:
        """后台重预热（换音色/改称呼后），不卡 UI。

        注意：QThread 必须是**有人持有引用**的对象——局部变量出了作用域被 GC 掉，
        线程还在跑就会 "QThread: Destroyed while thread is still running" 直接崩。
        所以统一挂到 self._bg_threads，结束再摘掉。
        """
        def run() -> None:
            try:
                self._warm_ack_cache()
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] 后台预热失败: {exc}")
        worker = _CallableWorker(run)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(thread.quit)
        worker.done.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._bg_threads = [(t, w) for (t, w) in self._bg_threads if t.isRunning()]
        self._bg_threads.append((thread, worker))
        thread.start()

    def _revoice(self, voice_name: str) -> None:
        """设置窗换音色后：缓存按音色隔离，换掉再按需预热。"""
        if self.engine is None:
            return
        try:
            cache = UtteranceCache(paths.data_root() / "tts_cache.npz",
                                   f"moss-nano-v4:{voice_name}")
            cache.load()
            self.engine.attach_cache(cache)
        except Exception:  # noqa: BLE001
            pass

    def _hint_entry_points(self) -> None:
        """静默启动时提示一次入口：右键宠物 = 菜单（托盘可能被收进溢出区），
        唤醒词 = 免按键对话。"""
        def show() -> None:
            if self.window is not None:
                self.window.show_bubble(
                    f"已待命～右键我改设置；说「{self.wake_cfg.phrase}」叫我",
                    ms=9000)
        QTimer.singleShot(1500, show)

    def build(self) -> int:
        # ---- TTS 引擎（后台加载，失败自动降级 SAPI） ----
        # 先建引擎对象（不加载），播放器按引擎的音频格式开流：
        # MOSS 输出 48kHz 立体声，播放器不做重采样，保音质。
        self.engine = VoiceRouter(
            MossNanoEngine(self.voice_cfg.model_dir, self.voice_cfg.voices_dir,
                           self.voice_cfg.voice_name, self.voice_cfg.threads,
                           work_dir=self.data_dir),
            fallback=self.voice_cfg.fallback)

        # ---- 播放线程 ----
        self.player = SpeechPlayer(sample_rate=self.engine.sample_rate,
                                   channels=self.engine.channels)
        self.player_thread = QThread()
        self.player.moveToThread(self.player_thread)
        self.player_thread.started.connect(self.player.run)
        self.player_thread.start()

        tts_ok = {"ok": False, "err": ""}

        def load_tts() -> None:
            try:
                self.engine.load()
                tts_ok["ok"] = True
                self._warm_ack_cache()
            except Exception as exc:  # noqa: BLE001
                tts_ok["err"] = str(exc)

        loader = QThread()
        worker = _CallableWorker(load_tts)
        worker.moveToThread(loader)
        loader.started.connect(worker.run)
        worker.done.connect(loader.quit)
        worker.done.connect(worker.deleteLater)
        loader.finished.connect(loader.deleteLater)
        self._bg_threads.append((loader, worker))   # 防 GC（见 _warm_async 注释）
        loader.start()
        self._tts_ok = tts_ok

        # ---- 宠物窗口（先建好并显示，设置窗压在它上面） ----
        self.window = PetWindow(paths.app_root() / "pet" / "assets" / "frames",
                                self.pet_cfg, usage=self.usage)
        self.window.animator.start()
        self.window.show()

        # ---- 首次设置：没有密钥就弹窗；--setup 强制弹 ----
        if self.args.setup:
            self.open_setup(first_run=False)
            api_key = os.environ.get("DEEPSEEK_API_KEY", "") or get_api_key(self.data_dir)
        else:
            had_key = bool(os.environ.get("DEEPSEEK_API_KEY", "")
                           or get_api_key(self.data_dir))
            api_key = self.resolve_api_key()
            if had_key:
                self._hint_entry_points()   # 静默启动：提示右键与唤醒词入口

        if not api_key:
            self.window.tray.showMessage(
                "小鲸鱼", "还没设置 API 密钥：主动观察和对话都用不了。"
                          "点托盘「重新设置…」补上。", msecs=10000)

        # ---- DeepSeek 客户端 ----
        # 用量回调接到 UsageStore：三条路径（观察/主动/会话）都经过这里，
        # 只需要在这一个地方记账，不用每个调用点各写一遍。
        self.judge = VisionJudge(
            self.judge_cfg, api_key,
            on_usage=lambda p, c, cached: self.usage.record(p, c, cached),
            on_failure=self.usage.record_failure,
            notes=self.memory,
        ) if api_key else None
        self.channel = ChatChannel(self.judge, self.store, self.gate,
                                   self.player, self.engine,
                                   frame_source=self._frame_b64,
                                   idle_finish_sec=self.chat_cfg.idle_finish_sec,
                                   # 会话结束 → 释放"会话进行中"对唤醒监听的暂停。
                                   # 在 start() 前连接（见 ChatChannel.submit），
                                   # 消除竞态，避免"聊一次就再也喊不醒"。
                                   on_ended=self._on_session_finished,
                                   # 会话收尾后自动整理记忆（chat.auto_consolidate）。
                                   # 关掉时传 None，只剩「记住X」的规则提取。
                                   on_transcript=(self._schedule_consolidation
                                                  if self.chat_cfg.auto_consolidate
                                                  else None),
                                   ) if self.judge else None

        # ---- 观察循环 ----
        # 重启后统一从 observe 起：上一轮可能停在 off（关了视觉）或 dnd，
        # 留着会让观察循环永远不截屏，而托盘里的"视觉"勾选却是开的。
        self.store.set_mode("observe")
        self.loop = ObserveLoop(self.observe_cfg, self.screen, self.differ,
                                self.judge, self.gate, self.store,
                                self.player, self.engine,
                                proactive_cfg=self.proactive_cfg) if self.judge else None
        if self.loop:
            self.loop.state_changed.connect(self.window.set_state)
            self.loop.spoke.connect(self.window.show_bubble)
            self.loop.log_line.connect(self._on_log_line)
            self.loop.start()
            print(f"[proactive] 主动互动：{self.loop.proactive_hint()}")

        # ---- 唤醒词监听 ----
        self.wake = None
        if self.wake_cfg.enabled:
            self.wake = WakeListener(self.wake_cfg, self.store)
            self.wake.log_line.connect(self._on_log_line)
            self.wake.woke.connect(self._on_woke)
            self.wake.command.connect(self._on_command)
            self.wake.start()

        # ---- 信号接线 ----
        self._wire()

        print(f"{self.name}已启动（pid={os.getpid()}）。")
        if not tts_ok["ok"] and tts_ok["err"]:
            print(f"[warn] TTS 引擎加载失败: {tts_ok['err']}"
                  f"（{'已降级 SAPI5 保底' if self.engine.using_fallback else '无保底，将静默'}）")
        return self.app.exec()

    def _wire(self) -> None:
        def on_user_text(text: str) -> None:
            if self.channel is None:
                self.window.show_bubble("（还没设置 API 密钥，小鲸鱼说不了话……）")
                return
            session = self.channel.submit(text)
            if session is not None:
                session.state_changed.connect(self.window.set_state)
                session.replied.connect(self.window.show_bubble)
                session.thinking.connect(self._on_thinking)
                session.log_line.connect(self._on_log_line)

        self.window.user_text_submitted.connect(on_user_text)

        def on_interrupt() -> None:
            self.player.stop_all()
            self.engine.signal_stop()

        self.window.interrupt_requested.connect(on_interrupt)

        # 播放中暂停唤醒拾音（防她听到自己的声音）
        # 注意：started 是 Signal(str) 只发 utt_id——曾误写两个形参，
        # 导致每次开口都抛 TypeError、暂停逻辑静默失效（见 tests/test_wiring.py）
        self.player.started.connect(
            lambda utt_id: self._set_wake_paused("speaking", True))
        self.player.finished.connect(self._on_player_finished)
        self.player.first_audio.connect(
            lambda utt_id, ms: print(f"[tts] 首音 {ms}ms ({utt_id})"))

        def on_dnd(checked: bool) -> None:
            self.store.set_mode("dnd" if checked else "observe")
            self.window.set_state("nap" if checked else "idle")
            self._set_wake_paused("dnd", checked)
            # 用户明确要求：开启免打扰时默认关闭视觉功能
            if checked and self.window.act_vision.isChecked():
                self.window.act_vision.setChecked(False)   # 联动触发 on_vision

        self.window.act_dnd.toggled.connect(on_dnd)

        def on_vision(checked: bool) -> None:
            """视觉功能：开 = 她持续看屏幕（且会话能附当前帧）；
            关 = 完全不看。免打扰中不允许开，避免打扰失效。"""
            if checked and self.window.act_dnd.isChecked():
                # 已勾选状态下再设同值不会重发信号，不会递归
                self.window.act_vision.setChecked(False)
                self.window.show_bubble("专注模式中，先取消免打扰才能让我看屏幕～")
                return
            self._vision_on = checked
            self.store.set_mode("observe" if checked else "off")
            if checked:
                print("[vision] 视觉功能已开启：持续留意屏幕，会话也会带上当前画面")
                self._on_log_line("[vision] 视觉功能已开启")
            else:
                print("[vision] 视觉功能已关闭：不再看屏幕")
                self._on_log_line("[vision] 视觉功能已关闭")

        self.window.vision_toggled.connect(on_vision)

        # 托盘：重新设置
        if hasattr(self.window, "act_setup"):
            self.window.act_setup.triggered.connect(
                lambda: self.open_setup(first_run=False))

        # ---- 优雅退出 ----
        def shutdown() -> None:
            if self.wake is not None:
                self.wake.stop()
                self.wake.wait(3000)
            if self.loop:
                self.loop.stop()
                self.loop.wait(3000)
            if self.channel and self.channel.active:
                self.channel._session.stop()
            self.player.stop_all()
            self.player.shutdown()
            self.player_thread.quit()
            self.player_thread.wait(2000)
            self.store.flush()
            self.store.save()
            self.screen.close()

        self.app.aboutToQuit.connect(shutdown)

    # ---- 唤醒相关 ----

    def _frame_b64(self) -> str | None:
        """视觉功能开启时，供会话抓「当前这一帧」发给 DeepSeek。

        这就是主人说"看看我屏幕"她能看见的前提——之前会话只发文本，
        不附任何图像，所以她只能瞎猜（用户实测反馈的根因）。
        视觉关闭时返回 None：既省 token 也少一次网络往返。
        """
        if not self._vision_on:
            return None
        try:
            return encode_frame(self.screen.capture())
        except Exception:  # noqa: BLE001 截屏失败不该让会话崩
            return None

    def _set_wake_paused(self, reason: str, paused: bool) -> None:
        if self.wake is not None:
            self.wake.set_paused(reason, paused)

    def _on_player_finished(self, utt_id: str) -> None:
        # 播放结束一律恢复拾音。曾经对应答类语音只释放 "ack" 而漏掉
        # player.started 设的 "speaking"——泄漏一个暂停原因 = 唤醒监听
        # 永久假死（用户实测：聊过一轮就再也喊不醒她）。
        self._set_wake_paused("speaking", False)

    def _on_woke(self, phrase: str) -> None:
        """听到唤醒词：先应一声，然后等她说完指令（3s 静默收尾）。"""
        print(f"[wake] 唤醒词命中: {phrase}")
        self.window.set_state("talk")
        self.window.show_bubble(f"我在，{self.user_address}。")
        # 应答语走播放器；播放期间 player.started/finished 会自动暂停/恢复
        # 唤醒拾音（防她听到自己的声音）——不要在这里再加暂停原因，
        # 播放类暂停不能丢弃唤醒后的指令捕获（见 wake.py 模块文档）
        if self.engine is not None:
            utt_id = f"ack-{int(time.time() * 1000)}-{random.randint(0, 999)}"
            stop_event = self.engine.new_stop_event()
            ack = random.choice(ACK_LINES).format(addr=self.user_address)
            try:
                self.player.enqueue_stream(utt_id, self.engine.stream(ack, stop_event))
            except Exception as exc:  # noqa: BLE001 引擎加载中/异常不应吃掉唤醒反馈
                print(f"[warn] 应答发声失败: {exc}")

    def _on_command(self, text: str) -> None:
        """唤醒后捕获到指令 → 直接进会话（被动回应，不受主动节流限制）。"""
        print(f"[wake] 指令: {text}")
        if self.channel is None:
            self.window.show_bubble("（还没设置 API 密钥，小鲸鱼听不懂……）")
            return
        self._set_wake_paused("session", True)
        # 注意：会话结束 → 释放"session"暂停 已由 ChatChannel(on_ended) 在
        # session.start() 之前连好，这里不必再连 finished_session（晚一拍会竞态错过）。
        session = self.channel.submit(text)
        if session is not None:
            session.state_changed.connect(self.window.set_state)
            session.replied.connect(self.window.show_bubble)
            session.thinking.connect(self._on_thinking)
            # 会话线程内的异常/traceback 走 log_line，必须接进 run.log——
            # 这条线原本没接，导致"会话崩了但日志里一片空白"（排查噩梦）。
            session.log_line.connect(self._on_log_line)
        else:
            self._set_wake_paused("session", False)

    def _on_session_finished(self) -> None:
        self._set_wake_paused("session", False)

    def _schedule_consolidation(self, transcript: str) -> None:
        """会话收尾 → 起后台线程整理记忆（提炼要点 + 与旧笔记合并）。

        三条铁律：
        1. **本函数必须立刻返回**。它由 ChatSession 在 finally 里调用，而主线程
           处理 finished_session 时会对会话线程 `wait(3000)`——同步调 API 就是
           卡 3 秒 UI。
        2. **串行**：同一时刻只允许一个整理任务。连续几次短会话各自收尾时，
           并发打 API 既浪费又容易撞限流。
        3. **失败只记日志**：整理是锦上添花，任何异常都不能影响下一次对话。
           这里刻意 print 而不是静默 pass（本工程四次静默失效的教训）。

        关于线程引用：这里用原生 threading.Thread 而不是 QThread，所以**不需要**
        挂进 _bg_threads——Python 的 threading 模块内部持有运行中线程的引用，
        不存在"QThread 被 GC 掉而线程还在跑"那类硬崩。
        """
        if not transcript or self.judge is None or self.memory is None:
            return
        if self._consolidating:
            print("[memory] 上一次整理还没结束，跳过本次会话")
            return
        self._consolidating = True

        def work() -> None:
            try:
                remember, drop = self.judge.consolidate(transcript)
                if not remember and not drop:
                    print("[memory] 本次对话没有值得长期记的内容")
                    return
                added, removed = self.memory.apply_consolidation(remember, drop)
                print(f"[memory] 整理完成：新增 {added} 条 / 合并删除 {removed} 条"
                      f"（现有 {self.memory.count()} 条）")
                for kind, text in remember:
                    print(f"[memory]   + [{kind}] {text}")
            except Exception as exc:      # noqa: BLE001 整理失败绝不影响对话
                print(f"[memory] 整理失败（已忽略）：{exc}")
            finally:
                self._consolidating = False

        threading.Thread(target=work, name="memory-consolidate", daemon=True).start()

    def _on_thinking(self) -> None:
        """思考中的可见反馈：API 要 1–3s，没提示的话像她没反应。"""
        self.window.set_state("think")
        self.window.show_bubble("思考中…", ms=15000)

    # ---- 日志 / 密钥失效 ----

    def _on_log_line(self, line: str) -> None:
        print(f"[log] {line}")
        # 密钥失效/永久错误：只提醒一次，重设后重置标记
        low = line.lower()
        if ("permanent" in line or "401" in line or "api_key" in low) \
                and not self._setup_offered:
            self._setup_offered = True
            self.window.tray.showMessage(
                "小鲸鱼", "API 密钥可能失效了，点托盘「重新设置…」更新。", msecs=8000)


class _CallableWorker(QObject):
    """把普通函数搬进 QThread 执行的极小适配器。"""

    done = Signal()

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self._fn()
        finally:
            self.done.emit()


def main() -> int:
    setup_file_logging()
    parser = argparse.ArgumentParser(description="小鲸鱼桌宠")
    parser.add_argument("--config", type=Path,
                        default=paths.app_root() / "config.yaml")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--setup", action="store_true", help="强制打开设置窗")
    args = parser.parse_args()

    if args.self_test:
        pet_cfg, observe_cfg, gate_cfg, judge_cfg, voice_cfg, _wake, _chat, \
            _pro, _n, _pv, _st, _ua = load_config(args.config, paths.data_root())
        return self_test(judge_cfg, voice_cfg, observe_cfg, gate_cfg)

    pet = PetApp(args)
    return pet.build()


if __name__ == "__main__":
    raise SystemExit(main())
