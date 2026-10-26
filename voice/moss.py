"""voice/moss.py — MossNanoEngine：MOSS-TTS-Nano ONNX 进程内推理封装。

底层是 vendor/moss_tts_nano（OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX +
MOSS-Audio-Tokenizer-Nano-ONNX，Apache 2.0）的 OnnxTtsRuntime，
纯 CPU onnxruntime，本模块不起它的 HTTP 服务（单进程约束）。

关于"流式"：官方 runtime 支持边生成边解码（streaming=True），但本机实测
合成速度只有 ~0.7x 实时（i7-13700H，threads=6 最快，2/4/8 都更慢）——
**边合成边播必然断流**。所以 stream() 改为"按句合成、整句交出"：
第一句合成完就能开播，后续句在播放期间合成（RTF<1 时播放会追上合成，
表现为句间稍慢，但不会断续）；唤醒应答这类固定短句靠 UtteranceCache
预热，命中即 0 延迟出声。

与 Audio8Engine 的关键差异：
  - 输出 48kHz 立体声 float32（Audio8 是 44.1kHz 单声道）；播放器按
    engine.sample_rate / engine.channels 开流，不做重采样（保音质）。
  - 音色 = 参考音频（3–10s 干净人声）经 codec 编码成的 prompt codes，
    启动时编码一次缓存复用（编码一次 0.43s，每句重编码是浪费）。
    音色目录：voices/<音色名>/reference.wav；没有注册目录时退回内置音色。
  - SentencePiece 在 Windows 上不能直接吃中文路径的 model_file
    （pybind11 传 UTF-8、C++ 侧按 ANSI 代码页开文件 → NOT_FOUND），
    vendor 内已改为 Python 读字节 + model_proto 加载。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterator

import numpy as np

from voice.cache import UtteranceCache, concat_with_pause, split_sentences

SAMPLE_RATE = 48000
CHANNELS = 2
INTER_CHUNK_PAUSE_SEC = 0.20      # 句间静音，防粘字
MAX_SENTENCE_CHARS = 32           # 单句上限（TTS 速度预算）
MAX_PAUSE_SEC = 0.22              # 内部静音压缩上限：MOSS 对短文本会把音节间
                                  # 停顿拉到 0.5s+（5 字应答实测 9.2s），压到 0.22s
MAX_SYNTH_RETRIES = 4             # 采样跑偏重试次数。ONNX fixed 路径的温度等
                                  # 常量烘焙在图里（0.8），短文本有概率不预测
                                  # EOS 继续重复（实测「好的。」单次跑偏率
                                  # 42%——5/12；长句 ~1/8）。换种子重合成绕过：
                                  # 5 次尝试全挂概率 42%^5 ≈ 1.3%。
                                  # 每次重试约 0.6–1.5s（短句），可接受
_EOS_PUNCT = "。！？!?.;；…~～"     # 句尾标点：有它 MOSS 才预测 EOS
_COMMA_TAIL = "，,、；;"           # 逗号/顿号收尾：语义上"没说完"，同样锚不住


def _ensure_eos(sentence: str) -> str:
    """确保句尾有标点——无标点短文本 MOSS 系统性不预测 EOS，重试救不回。

    实测（xiaojingyu 音色，各 3 样本）：
      「小鲸鱼」无标点 → 6/6 跑偏（ASR："高小鲸鱼，小鲸鱼湿身。"）
      「小鲸鱼。」加句号 → 3/3 干净（且句号不发声，音频反而更短）
      「好的」无标点 → 9 次尝试 7 次跑偏，3 个最终样本 2 个仍超限
    逗号收尾（「好的，」）重试能救但 4/6 首次就跑偏，换成句号后一把过。
    LLM 回复偶尔不带标点（"好的"），分句硬切出的尾句也没有——必踩。
    标点本身不发声，补句号不改变音频内容，只锚定结束预测。
    """
    text = sentence.rstrip()
    if not text:
        return sentence
    # 注意：endswith 传字符串是匹配整个后缀，必须用 in 逐字符判断
    # （曾经传错成 endswith(_EOS_PUNCT) 字符串 → 恒 False → 已带标点的
    # 文本被追加第二个句号 → 双句号把模型带崩，6/6 跑偏）
    if text[-1] in _COMMA_TAIL:
        return text[:-1] + "。"
    if text[-1] in _EOS_PUNCT:
        return sentence
    return text + "。"


def tighten_silence(pcm: np.ndarray, sample_rate: int = SAMPLE_RATE,
                    max_pause_sec: float = MAX_PAUSE_SEC,
                    thresh_ratio: float = 0.08, win_sec: float = 0.02) -> np.ndarray:
    """压缩超过 max_pause_sec 的内部静音段（首尾静音同样受益）。

    MOSS-TTS-Nano 是 0.1B 小模型，短文本会把音节间停顿拉得很夸张：
    实测「我在，主人。」5 个字产出 9.2s，其中 3s 以上是 0.5–0.75s 的
    停顿——唤醒应答要干等 9 秒才能开口听话。这里按能量包络把长静音
    压到 max_pause_sec：纯切片、不变调、不碰语音本体，句间自然停顿
    （<上限）原样保留。
    """
    if pcm.size == 0:
        return pcm
    mono = np.abs(pcm.mean(axis=1))
    win = max(1, int(sample_rate * win_sec))
    n = len(mono) // win
    if n < 4:
        return pcm
    env = np.sqrt((mono[: n * win].reshape(n, win) ** 2).mean(axis=1))
    peak = float(np.percentile(env, 95))
    if peak <= 1e-6:
        return pcm
    quiet = env < peak * thresh_ratio
    keep = np.ones(n, dtype=bool)
    max_bins = max(1, int(round(max_pause_sec / win_sec)))
    i = 0
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        if j - i > max_bins:
            half = max_bins // 2
            keep[i + half: j - half] = False   # 首尾各留一半，中间删掉
        i = j
    if keep.all():
        return pcm
    mask = np.repeat(keep, win)
    if len(mask) < len(pcm):                   # 尾部不足一窗的样本原样保留
        mask = np.concatenate([mask, np.ones(len(pcm) - len(mask), dtype=bool)])
    return pcm[mask]


def _duration_cap(sentence: str) -> float:
    """单句合成时长上限（秒），超出判定为采样跑偏，触发重试。

    中文自然语速约 4 字/s，留 ~40% 余量：「我在，主人。」6 字符
    （含标点）正常实测 1.1–2.2s，上限 2.8s；跑偏样本实测最低 3.2s
    （4.9s/14.4s/28.9s 都有），必被抓到。32 字长句上限 10.6s，
    不误伤自然慢速。
    """
    return max(2.2, 0.30 * len(sentence) + 1.0)


class MossNanoEngine:
    def __init__(self, model_dir: Path, voices_dir: Path, voice_name: str = "xiaojingyu",
                 threads: int = 6, work_dir: Path | None = None) -> None:
        self.model_dir = Path(model_dir)
        self.voices_dir = Path(voices_dir)
        self.voice_name = voice_name
        self.threads = threads
        self.work_dir = Path(work_dir) if work_dir else None
        self._runtime = None
        self._stop_event: threading.Event | None = None
        self._cache: UtteranceCache | None = None
        self._codes: list[list[int]] | None = None      # 当前音色 prompt codes
        self._codes_for: str | None = None              # codes 对应的音色名

    # ---- 音频格式（播放器据此开流，不做重采样） ----

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def channels(self) -> int:
        return CHANNELS

    @property
    def loaded(self) -> bool:
        return self._runtime is not None

    def load(self) -> None:
        """加载 ONNX 会话（~1GB 内存，一次性，之后常驻热加载）。

        vendor 的 OnnxTtsRuntime 会在 output_dir 建目录；默认是相对 CWD 的
        generated_audio/，打包成 exe 后会出现在用户启动目录，故显式指到
        work_dir（调用方给 data_root）。本引擎不写 wav 文件，这个目录
        只是runtime 初始化时的副作用。
        """
        if self._runtime is not None:
            return
        from vendor.moss_tts_nano.onnx_tts_runtime import OnnxTtsRuntime

        output_dir = self.work_dir if self.work_dir is not None else Path(".")
        self._runtime = OnnxTtsRuntime(
            model_dir=self.model_dir,
            thread_count=self.threads,
            output_dir=str(output_dir),
        )
        # 注意：不要试图通过 manifest 的 generation_defaults.audio_temperature
        # 调采样温度——sample_mode="fixed"（默认）走 local_fixed_sampled_frame
        # ONNX 图，温度/top_k/rep_penalty 全部烘焙在图里（0.8/25/1.2），
        # Python 侧改了不生效（实测改 0.45 输出逐样本不变）。改
        # sample_mode="full" 虽落到 Python 采样可调温，但实测慢 5–10 倍
        # （RTF 0.12–0.98x）且跑偏更多（2/6），不可用。
        # 短文本跑偏靠 _synth_one 的换种子 + 超时重试解决。
        self._ensure_voice_codes()

    def unload(self) -> None:
        self._runtime = None
        self._codes = None
        self._codes_for = None

    # ---- 音色 ----

    def _reference_wav(self, name: str) -> Path:
        return self.voices_dir / name / "reference.wav"

    def _ensure_voice_codes(self) -> None:
        """确保当前音色的 prompt codes 就绪（换音色后重新编码，仅一次）。"""
        if self._runtime is None:
            raise RuntimeError("MossNanoEngine.load() 未调用")
        if self._codes is not None and self._codes_for == self.voice_name:
            return
        ref = self._reference_wav(self.voice_name)
        try:
            if ref.is_file():
                self._codes = self._runtime.encode_reference_audio(str(ref))
            else:
                builtin = {v["voice"] for v in self._runtime.list_builtin_voices()}
                if self.voice_name in builtin:
                    self._codes = self._runtime.resolve_prompt_audio_codes(
                        voice=self.voice_name, prompt_audio_path=None)
                else:
                    # 前置条件不满足必须出声：静默降回默认音色会让
                    # "她声音变了"很难排查（本工程踩过两次静默失效）
                    print(f"[warn] 音色「{self.voice_name}」未注册"
                          f"（{ref} 不存在），暂用内置音色")
                    self._codes = self._runtime.resolve_prompt_audio_codes(
                        voice="", prompt_audio_path=None)
        except Exception as exc:  # noqa: BLE001 音色坏了也要能发声
            print(f"[warn] 音色「{self.voice_name}」准备失败（{exc}），暂用内置音色")
            try:
                self._codes = self._runtime.resolve_prompt_audio_codes(
                    voice="", prompt_audio_path=None)
            except Exception as exc2:  # noqa: BLE001 内置音色也没有 = 引擎不可用
                print(f"[error] 内置音色也不可用: {exc2}")
                self._codes = None
        self._codes_for = self.voice_name

    # ---- 短句缓存（绕过"合成慢于实时"的等待） ----

    def attach_cache(self, cache: UtteranceCache | None) -> None:
        self._cache = cache

    def warm(self, texts: list[str]) -> int:
        """预热一批固定短句，返回新合成条数（已在缓存里的跳过）。

        前置条件不满足时必须**出声**而不是静默返回：预热失效的表现是
        "唤醒后要等好几秒才出声"，和"音频卡"很难区分，排查成本极高。
        """
        if self._runtime is None:
            print("[warn] 预热跳过：引擎尚未加载")
            return 0
        if self._cache is None:
            print("[warn] 预热跳过：未挂载 UtteranceCache"
                  "（调用方漏了 attach_cache？）唤醒应答将继续等合成")
            return 0
        made = 0
        for text in texts:
            if self._cache.get(text) is not None:
                continue
            try:
                self._cache.put(text, self.synthesize(text))
                made += 1
            except Exception as exc:  # noqa: BLE001 预热失败不影响主流程
                print(f"[tts] 预热失败「{text}」: {exc}")
        self._cache.save()
        return made

    def new_stop_event(self) -> threading.Event:
        self._stop_event = threading.Event()
        return self._stop_event

    def signal_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    # ---- 合成 ----

    def _synth_one(self, sentence: str, stop_event: threading.Event | None) -> np.ndarray:
        """合成单个短句 → (frames, 2) float32 48kHz。

        两个防跑偏机制（详见 MAX_SYNTH_RETRIES / load() 注释）：
        1. 每次合成都重新播种 runtime.rng——vendor 默认固定种子 1234，
           每次引擎加载后第一句必然是同一个跑偏样本（必现陷阱）；
        2. 超过 _duration_cap 判定跑偏，换种子重试。
        """
        if self._runtime is None:
            raise RuntimeError("MossNanoEngine.load() 未调用")
        prepared = self._runtime.prepare_synthesis_text(
            text=sentence, enable_wetext=False)
        text = _ensure_eos(str(prepared["text"]))
        cap = _duration_cap(sentence)
        fallback: np.ndarray | None = None
        for attempt in range(MAX_SYNTH_RETRIES + 1):
            if stop_event is not None and stop_event.is_set():
                # 被打断：宁可交静音也别把跑偏音频播出去
                return np.zeros((0, CHANNELS), dtype=np.float32)
            self._runtime.rng = np.random.default_rng()
            result = self._runtime.synthesize_single_chunk(
                text=text,
                prompt_audio_codes=self._codes,
                streaming=False,
            )
            wave = np.asarray(result["waveform"], dtype=np.float32)
            if wave.ndim == 1:
                wave = wave.reshape(-1, 1)
            wave = tighten_silence(wave, SAMPLE_RATE, MAX_PAUSE_SEC)
            dur = wave.shape[0] / SAMPLE_RATE
            if dur <= cap:
                return wave
            fallback = wave
            print(f"[tts] 「{sentence}」第 {attempt + 1} 次合成 {dur:.1f}s "
                  f"超上限 {cap:.1f}s（采样跑偏），换种子重试")
        # 重试全部超限（实测概率 ~1/64）：交最长的也不是办法，交最后一个，
        # 至少日志上有痕迹
        return fallback if fallback is not None else np.zeros((0, CHANNELS),
                                                              dtype=np.float32)

    def synthesize(self, text: str, stop_event: threading.Event | None = None) -> np.ndarray:
        """整段合成 → (frames, 2) float32 48kHz。分句合成 + 句间静音。"""
        if self._runtime is None:
            raise RuntimeError("MossNanoEngine.load() 未调用")
        self._ensure_voice_codes()
        if self._codes is None:
            raise RuntimeError("没有可用的音色 codes（引擎不可用）")
        sentences = split_sentences(text, max_chars=MAX_SENTENCE_CHARS)
        if not sentences:
            return np.zeros((0, CHANNELS), dtype=np.float32)
        chunks: list[np.ndarray] = []
        for sentence in sentences:
            if stop_event is not None and stop_event.is_set():
                break
            chunks.append(self._synth_one(sentence, stop_event))
        pause = int(round(SAMPLE_RATE * INTER_CHUNK_PAUSE_SEC))
        pcm = concat_with_pause(chunks, pause, CHANNELS)
        if pcm.size and self._cache is not None:
            self._cache.put(text, pcm)
        return pcm

    def stream(self, text: str, stop_event: threading.Event | None = None) -> Iterator[np.ndarray]:
        """产出 PCM 块（每句一块）。命中短句缓存则整段立即返回。

        本机合成只有 ~0.7x 实时，边合成边播同一句会断流（见模块文档），
        所以粒度是"句"：播放期间合成下一句，播放追上时自然等待。
        """
        if self._runtime is None:
            raise RuntimeError("MossNanoEngine.load() 未调用")
        if self._cache is not None:
            cached = self._cache.get(text)
            if cached is not None:
                yield np.asarray(cached, dtype=np.float32)
                return
        self._ensure_voice_codes()
        if self._codes is None:
            raise RuntimeError("没有可用的音色 codes（引擎不可用）")
        sentences = split_sentences(text, max_chars=MAX_SENTENCE_CHARS)
        if not sentences:
            return
        pause = int(round(SAMPLE_RATE * INTER_CHUNK_PAUSE_SEC))
        collected: list[np.ndarray] = []
        first = True
        for sentence in sentences:
            if stop_event is not None and stop_event.is_set():
                return
            wave = self._synth_one(sentence, stop_event)
            if first:
                first = False
            else:
                yield np.zeros((pause, CHANNELS), dtype=np.float32)
            yield wave
            collected.append(wave)
        if self._cache is not None and collected:
            # 整段交完后把完整文本塞缓存（key 与 synthesize 一致，
            # 下次同一句整段命中直接交出，无需再分句）
            self._cache.put(text, concat_with_pause(collected, pause, CHANNELS))
