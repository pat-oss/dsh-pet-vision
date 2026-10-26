#!/usr/bin env python3
"""register_voice.py — MOSS-TTS-Nano 音色注册（用自己的音频样本复刻音色）。

MOSS-TTS-Nano 是零样本克隆：只需要 3–10s 干净人声参考音频，**不需要转写文本**。
本脚本把参考音频复制到音色目录并写 meta.json，设置窗里即可选用。

用法：
  # 用自己的样本注册
  python scripts/register_voice.py --audio path/to/sample.wav --name xiaojingyu

  # 列出已注册音色
  python scripts/register_voice.py --list

合规提醒（MOSS-TTS-Nano 模型卡）：只克隆自己或已获明确授权的声音；
合成音频应在适当场合标注为合成产物。参考音频太长、太吵都会明显
拉低相似度与稳定性。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MIN_SEC = 3.0
MAX_SEC = 10.0


def register(audio_path: Path, voices_dir: Path, name: str) -> Path:
    import soundfile as sf

    info = sf.info(str(audio_path))
    duration = info.frames / info.samplerate
    if not (MIN_SEC <= duration <= MAX_SEC):
        sys.exit(f"参考音频时长 {duration:.1f}s 不在 {MIN_SEC:.0f}–{MAX_SEC:.0f}s 范围"
                 f"（MOSS-TTS-Nano 零样本克隆推荐区间）")
    if info.channels > 2:
        sys.exit(f"参考音频 {info.channels} 声道，超过 2 声道不支持")

    target = voices_dir / name
    target.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再换位，避免半份文件被当成可用音色
    tmp = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=voices_dir))
    try:
        shutil.copyfile(audio_path, tmp / "reference.wav")
        meta = {
            "name": name,
            "source": audio_path.name,
            "note": "零样本克隆参考音频（MOSS-TTS-Nano，无需转写文本）",
            "sample_rate": info.samplerate,
            "channels": info.channels,
            "duration_sec": round(duration, 2),
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        (tmp / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if (target / "reference.wav").exists():
            shutil.rmtree(target)
        shutil.move(str(tmp / "reference.wav"), target / "reference.wav")
        shutil.move(str(tmp / "meta.json"), target / "meta.json")
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    print(f"已注册音色 {name}: {duration:.1f}s → {target}")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voices-dir", type=Path, default=ROOT / "models/moss-voices")
    parser.add_argument("--audio", type=Path, help="参考音频（wav/flac/ogg，3–10s 干净人声）")
    parser.add_argument("--name", default="xiaojingyu", help="音色名（单个路径成分）")
    parser.add_argument("--list", action="store_true", help="列出已注册音色")
    args = parser.parse_args()

    if args.list:
        if not args.voices_dir.is_dir():
            print(f"（音色目录不存在：{args.voices_dir}）")
            return
        for d in sorted(args.voices_dir.iterdir()):
            if d.is_dir() and (d / "reference.wav").is_file():
                print(f"  {d.name}  ({d / 'reference.wav'})")
        return
    if not args.audio:
        parser.error("需要 --audio（参考音频）；或用 --list 查看已注册音色")
    register(args.audio, args.voices_dir, args.name)


if __name__ == "__main__":
    main()
