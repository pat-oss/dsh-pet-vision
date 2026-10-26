"""scripts/verify_wake.py — 真实链路探测：麦克风采集 → Vosk → 唤醒匹配。

用途：诊断"喊了唤醒词没反应"。做三件事：
  1. 打印默认输入设备的环境 RMS（判断麦克风是否真的在收声）
  2. 用 MOSS-TTS-Nano 合成唤醒词并通过扬声器播放（模拟用户说话）
  3. 实时打印 Vosk 的 partial/final 识别文本与 RMS，报告是否触发唤醒

运行： .venv/Scripts/python.exe scripts/verify_wake.py [唤醒词] [能量门] [播放增益] [设备号]
例：   .venv/Scripts/python.exe scripts/verify_wake.py 幼楚 0.002 2.5
注意：戴耳机时声音进耳机、麦克风收不到播放，探测 RMS 会偏低——
      那是声学路径问题，不代表用户真人说话的效果。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import sounddevice as sd

from voice.wake import (_ascii_model_path, auto_tolerance, contains_wake,
                        normalize_text, rms_int16)


def main() -> int:
    phrase = sys.argv[1] if len(sys.argv) > 1 else "幼楚"
    gate = float(sys.argv[2]) if len(sys.argv) > 2 else 0.012
    gain = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    device = sys.argv[4] if len(sys.argv) > 4 else None
    dev_idx = int(device) if device is not None else sd.default.device[0]
    print(f"== 探测唤醒词「{phrase}」 gate={gate} gain={gain} device={dev_idx} ==")

    # ---- 环境噪声 ----
    stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16",
                            blocksize=1600, device=dev_idx)
    stream.start()
    time.sleep(0.3)
    levels = []
    for _ in range(10):
        data, _ = stream.read(1600)
        levels.append(rms_int16(data))
    amb = max(levels)
    print(f"默认输入设备: {sd.query_devices(dev_idx)['name']}")
    print(f"环境噪声峰值 RMS = {amb:.4f}（能量门 0.012；若说话时也低于此值则永远喂不进识别器）")

    from vosk import KaldiRecognizer, Model, SetLogLevel
    SetLogLevel(-1)
    model_dir = Path(__file__).resolve().parent.parent / "models" / "vosk-small-cn"
    rec = KaldiRecognizer(Model(_ascii_model_path(model_dir)), 16000)
    print("Vosk 模型已加载。")

    # ---- 后台线程：合成并播放唤醒词 ----
    def speak(text: str) -> None:
        try:
            from voice.moss import MossNanoEngine
            from core.types import VoiceConfig
            import yaml
            cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml")
                                 .read_text(encoding="utf-8"))
            vc = VoiceConfig(**{**cfg["voice"],
                                "model_dir": str(Path(__file__).resolve().parent.parent /
                                                 cfg["voice"]["model_dir"]),
                                "voices_dir": str(Path(__file__).resolve().parent.parent /
                                                  cfg["voice"]["voices_dir"])})
            eng = MossNanoEngine(vc.model_dir, vc.voices_dir, vc.voice_name,
                                 vc.threads, work_dir=Path(__file__).resolve().parent.parent / "data")
            t0 = time.time()
            eng.load()
            print(f"[probe] MOSS 加载 {time.time() - t0:.1f}s，开始播放「{text}」")
            audio = eng.synthesize(text)
            if gain != 1.0:
                audio = np.clip(audio * gain, -1.0, 1.0).astype(np.float32)
            sd.play(audio, eng.sample_rate, blocking=True)
            print(f"[probe] 播放完成（{audio.shape[0] / eng.sample_rate:.1f}s 音频）")
        except Exception as exc:  # noqa: BLE001
            print(f"[probe] 播放失败: {exc}")

    fired = {"hit": False}

    def watch() -> None:
        deadline = time.monotonic() + 25
        last_print = 0.0
        peak = 0.0
        while time.monotonic() < deadline:
            data, _ = stream.read(1600)
            rms = rms_int16(data)
            peak = max(peak, rms)
            if rms < gate:
                continue
            pcm = np.asarray(data, dtype=np.int16).tobytes()
            if rec.AcceptWaveform(pcm):
                text = json.loads(rec.Result()).get("text", "")
                if text:
                    print(f"  [final ] rms={rms:.3f}  {text!r}")
                    if contains_wake(phrase, text,
                                     auto_tolerance(phrase, 1)):
                        print(f"  >>> 唤醒命中！ final={text!r}")
                        fired["hit"] = True
            else:
                partial = json.loads(rec.PartialResult()).get("partial", "")
                now = time.monotonic()
                if partial and now - last_print > 0.4:
                    last_print = now
                    print(f"  [partial] rms={rms:.3f}  {partial!r}")
                    if contains_wake(phrase, partial,
                                     auto_tolerance(phrase, 1)):
                        print(f"  >>> 唤醒命中！ partial={partial!r}")
                        fired["hit"] = True
        print(f"[probe] 监听窗口内 RMS 峰值 = {peak:.4f}")

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    time.sleep(1.0)
    speak(phrase)
    t.join(timeout=30)
    stream.close()

    print("== 结论 ==")
    print(f"触发唤醒: {'是' if fired['hit'] else '否'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
