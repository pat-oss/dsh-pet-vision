"""scripts/download_models.py — 下载运行所需的三个模型到 models/。

模型体积较大（约 800 MB），不随仓库分发，所以首次运行前跑一次本脚本。

用法：
    pip install huggingface_hub      # 只需一次
    python scripts/download_models.py

    # 只下某一个：
    python scripts/download_models.py --only moss
    python scripts/download_models.py --only vosk
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"

HF_REPOS = {
    "moss": ("OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX", "moss-tts-nano-onnx"),
    "codec": ("OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX",
              "moss-audio-tokenizer-nano-onnx"),
}
VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"
VOSK_DIR = "vosk-small-cn"


def _hf(repo: str, target: str) -> bool:
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  ! 缺少 huggingface_hub，先执行： pip install huggingface_hub")
        return False
    dest = MODELS / target
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  → {repo}  →  models/{target}（约 640MB / 87MB，慢一点）")
    snapshot_download(repo_id=repo, local_dir=str(dest),
                      allow_patterns=["*.onnx", "*.data", "*.json", "*.model",
                                      "*.md", "*.txt"])
    return True


def _vosk() -> bool:
    dest = MODELS / VOSK_DIR
    if (dest / "conf").is_dir():
        print(f"  = models/{VOSK_DIR} 已存在，跳过")
        return True
    zip_path = MODELS / "vosk-model-small-cn-0.22.zip"
    MODELS.mkdir(parents=True, exist_ok=True)
    if not zip_path.exists():
        print(f"  → 下载 {VOSK_URL}（约 42MB）")
        urllib.request.urlretrieve(VOSK_URL, zip_path)
    print("  → 解压……")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(MODELS)
    unpacked = MODELS / "vosk-model-small-cn-0.22"
    if unpacked.is_dir() and not dest.exists():
        shutil.move(str(unpacked), str(dest))
    zip_path.unlink(missing_ok=True)
    return dest.is_dir()


def main() -> int:
    ap = argparse.ArgumentParser(description="下载小鲸鱼所需模型")
    ap.add_argument("--only", choices=["moss", "codec", "vosk"], help="只下一个")
    args = ap.parse_args()

    MODELS.mkdir(parents=True, exist_ok=True)
    ok = True
    jobs = [args.only] if args.only else ["moss", "codec", "vosk"]
    for job in jobs:
        print(f"[{job}]")
        if job == "vosk":
            ok &= _vosk()
        else:
            repo, target = HF_REPOS[job]
            ok &= _hf(repo, target)

    print()
    if ok:
        print("模型就绪。下一步：python main.py（首次会弹设置窗要 DeepSeek API 密钥）")
        print("想让声音更好听：python scripts/register_voice.py 注册自己的音色。")
    else:
        print("有模型没下成功，可手动下载：")
        print("  https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX")
        print("  https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX")
        print("  https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip")
        print("分别解到 models/moss-tts-nano-onnx/、models/moss-audio-tokenizer-nano-onnx/、"
              "models/vosk-small-cn/")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
