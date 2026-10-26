"""core/types.py — 全部数据类定义（设计文档 §9 的落地）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


@dataclass(frozen=True)
class Decision:
    """一次观察判断的结构化结果。"""

    speak: bool
    text: str | None            # speak=True 时非空，≤80 字
    reason: str                 # 判断理由，入日志供抽检
    model: str
    prompt_version: str
    latency_ms: int


@dataclass(frozen=True)
class Turn:
    """会话历史的一轮。"""

    role: str                   # user / assistant
    text: str


@dataclass
class JudgeConfig:
    base_url: str = "https://api.deepseek.com"
    vision_model: str = "deepseek-v4-flash-vision-exp"
    chat_model: str = "deepseek-v4-flash-vision-exp"
    detail: str = "low"
    timeout_sec: float = 8.0
    # None = 不限制（不向 API 传 max_tokens，由模型自然停）。可在设置窗里由用户改。
    max_tokens_observe: int | None = None
    max_tokens_chat: int | None = None


@dataclass
class GateConfig:
    proactive_min_interval_sec: float = 30.0
    max_proactive_per_hour: int = 10
    daily_call_budget: int = 300
    session_reserve_calls: int = 60      # 预留被动会话保底额度
    daily_cost_budget_cny: float = 5.0
    # 全屏时是否"完全安静"（连到点搭话也不说）。默认 False：看视频/玩游戏时
    # 仍然按频率陪聊；但全屏下"画面变动触发"永远关闭（视频画面一直在动，
    # 那不是值得开口的信号，放进来会每 5s 烧一次 API）。
    dnd_fullscreen: bool = False
    title_blacklist: tuple[str, ...] = ("网银", "银行", "密码", "Password", "Keychain")


@dataclass
class ObserveConfig:
    interval_idle_sec: float = 10.0
    interval_active_sec: float = 3.0
    active_window_sec: float = 60.0      # 检测到变化后保持跟进的时长
    min_api_interval_sec: float = 5.0
    diff_threshold: int = 12             # dHash 汉明距离阈值（需标定）
    max_sentence_chars: int = 80         # 主动发言单句上限


@dataclass
class ProactiveConfig:
    """主动互动频率：每小时主动开口的次数区间。

    预设（mode）由设置窗选，custom 时用用户自己填的 min/max。
    实际节奏 = 在 [3600/max, 3600/min] 秒之间随机取间隔，逐轮重抽，
    这样既落在区间内，又不会像节拍器一样规律得诡异。
    """

    mode: str = "low"                # off | low | mid | high | custom
    min_per_hour: int = 5
    max_per_hour: int = 10

    PRESETS: ClassVar[dict[str, tuple[int, int]]] = {
        "off": (0, 0),
        "low": (5, 10),
        "mid": (10, 20),
        "high": (20, 30),
    }

    def resolve(self) -> tuple[int, int]:
        """解析成 (最低, 最高) 次/小时；不主动互动返回 (0, 0)。"""
        if self.mode in self.PRESETS:
            return self.PRESETS[self.mode]
        lo = max(1, int(self.min_per_hour or 1))
        hi = max(lo, int(self.max_per_hour or lo))
        return lo, hi

    def is_off(self) -> bool:
        return self.resolve()[1] <= 0


@dataclass
class VoiceConfig:
    engine: str = "moss-nano"           # moss-nano | sapi
    model_dir: Path = Path("models/moss-tts-nano-onnx")
    voices_dir: Path = Path("models/moss-voices")
    voice_name: str = "xiaojingyu"
    threads: int = 6
    fallback: str = "sapi"


@dataclass
class ChatConfig:
    idle_finish_sec: int = 8          # 会话空闲多久自动收尾（恢复唤醒监听）


@dataclass
class PetConfig:
    always_on_top: bool = True
    click_through: bool = False
    corner: str = "bottom-right"
    scale: float = 0.5
    topmost_reassert_sec: int = 30
    fps: int = 12


@dataclass
class WakeConfig:
    """唤醒词监听（像手机助手一样一直听，听到称呼就接话）。"""

    enabled: bool = True
    phrase: str = "小鲸鱼"              # 唤醒词 = 用户对它的称呼，可在首次设置里改
    model_dir: Path = Path("models/vosk-small-cn")
    sample_rate: int = 16000
    energy_threshold: float = 0.012     # RMS 能量门（0..1）：低于视为静音，不喂识别器
    hangover_blocks: int = 5            # 能量掉下后继续喂 5 个块（~0.5s），防切掉词头
    silence_ms: int = 1200              # 唤醒后静默多久认为指令说完
    command_timeout_sec: float = 10.0   # 唤醒后等待指令的最长时间
    cooldown_sec: float = 8.0           # 唤醒后冷却，防连续触发/自触发
    window_fragments: int = 8           # 参与匹配的最近片段数
    match_tolerance: int = 1            # 唤醒词编辑距离容错（≤4 字 1 处，>4 字 2 处）


@dataclass
class RuntimeState:
    """跨轮次持久化状态（单文件 JSON，原子写）。"""

    last_spoken_at: float = 0.0            # 上次主动发言（monotonic）
    spoke_this_hour: int = 0
    hour_window_start: float = 0.0
    calls_today: int = 0
    calls_day: str = ""                    # YYYY-MM-DD，跨天自动重置
    mode: str = "observe"                  # observe / chat / dnd
    consecutive_failures: int = 0
    suspended_until: float = 0.0
    text_cooldown: dict[str, float] = field(default_factory=dict)  # 文本指纹 → 解冻时间

    def roll_hour(self, now: float) -> None:
        if now - self.hour_window_start >= 3600:
            self.hour_window_start = now
            self.spoke_this_hour = 0

    def roll_day(self, today: str) -> None:
        if self.calls_day != today:
            self.calls_day = today
            self.calls_today = 0
