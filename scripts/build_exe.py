#!/usr/bin env python3
"""build_exe.py — 把小鲸鱼打成可双击的 exe（PyInstaller onedir）。

用法（在 little-whale-pet 目录下、venv 已激活）：
    python scripts/build_exe.py                 # 完整构建
    python scripts/build_exe.py --no-shortcut   # 只构建，不建桌面快捷方式
    python scripts/build_exe.py --clean         # 先删掉旧的 build/dist 再构建

产物：dist/小鲸鱼/小鲸鱼.exe
    ├── 小鲸鱼.exe            # 主程序
    ├── pet/assets/…          # 动画帧 + 图标（随包分发）
    ├── models/…              # MOSS-TTS-Nano TTS + Vosk 唤醒模型（约 780MB）
    ├── vendor/…              # MOSS 运行时（Apache 2.0）
    ├── config.yaml           # 默认配置
    ├── README.md / NOTICE
    └── （PySide6/onnxruntime/vosk 等依赖）

运行期数据（密钥/偏好/状态/日志）写 %LOCALAPPDATA%\\little-whale-pet，
所以整个 dist 目录可以放在任何位置，甚至设为只读。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "小鲸鱼"
BUILD = ROOT / "build" / "pyinstaller"
APP_NAME = "小鲸鱼"

# 需要随包分发的大块资源（源 → 包内相对路径）
DATA_DIRS = [
    ("pet/assets", "pet/assets"),
    ("models/moss-tts-nano-onnx", "models/moss-tts-nano-onnx"),
    ("models/moss-audio-tokenizer-nano-onnx", "models/moss-audio-tokenizer-nano-onnx"),
    ("models/moss-voices", "models/moss-voices"),
    ("models/vosk-small-cn", "models/vosk-small-cn"),
    ("vendor", "vendor"),
]
DATA_FILES = [
    ("config.yaml", "config.yaml"),
    ("README.md", "README.md"),
    ("NOTICE", "NOTICE"),
]

# 只靠 import 分析抓不到的模块/包
HIDDEN_IMPORTS = [
    "vendor.moss_tts_nano",
    "vendor.moss_tts_nano.onnx_tts_runtime",
    "vendor.moss_tts_nano.ort_cpu_runtime",
    "vendor.moss_tts_nano.text_normalization_pipeline",
    "vendor.moss_tts_nano.defaults",
    "sentencepiece",              # MOSS 文本前端（tokenizer.model）
    "soundfile",
    "yaml",
    "vosk",                       # 在 WakeListener._setup() 里延迟导入
]

# 带二进制/数据文件的包：连包带数据一起收（否则运行时 dlopen 失败）
COLLECT_ALL = [
    "vosk",                         # libvosk.dll + libstdc++ 等
    "sounddevice",                  # PortAudio DLL
    "onnxruntime",                  # capi 下的 .pyd/.dll
    "pypinyin",                     # 唤醒词拼音音节级匹配
]

EXCLUDES = [
    "tkinter", "matplotlib", "IPython", "jupyter", "notebook",
    "pytest", "test", "tests",
]


def run(cmd: list[str]) -> None:
    print("+", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True)


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 跳过让位/待删目录：frames.old-*（prepare_assets 改名让位的旧帧）和
    # _TRASH_*（待清理存档）不该进包——曾经裸 copytree 把 150MB 废弃帧
    # 打进了 exe（1272MB 里 120MB 死重）
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns("frames.old-*", "_TRASH_*"))
    print(f"  复制 {src.relative_to(ROOT)} → {dst.relative_to(ROOT / 'dist' / APP_NAME)}"
          f"（{dir_size_mb(dst):.0f} MB）")


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def dir_size_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1024 / 1024


def make_room(path: Path) -> None:
    """给 PyInstaller 让位：它开装前会删掉已存在的输出目录，而环境的
    safe-delete 守卫会拦截 >50 文件的批量删除。这里主动改名（非删除），
    旧包留 `.old-<时间戳>` 后缀，确认新包正常后用户可自行清理。"""
    if not path.is_dir():
        return
    backup = path.with_name(f"{path.name}.old-"
                            + __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S"))
    path.rename(backup)
    print(f"  旧目录让位 → {backup.name}（可自行删除）")


def sync_voices(dst_models: Path) -> None:
    """--fast 模式下单独同步音色目录：体积小但常变（新注册的音色要进包）。"""
    src = ROOT / "models" / "moss-voices"
    dst = dst_models / "moss-voices"
    if not src.is_dir():
        return
    for voice in sorted(src.iterdir()):
        if not voice.is_dir():
            continue
        target = dst / voice.name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(voice, target)
        print(f"  同步音色 {voice.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="先清理 build/dist")
    parser.add_argument("--no-shortcut", action="store_true", help="不创建桌面快捷方式")
    parser.add_argument("--fast", action="store_true",
                        help="跳过已存在的大块资源复制（只改代码时用）")
    args = parser.parse_args()

    if args.clean:
        make_room(ROOT / "build" / "pyinstaller")
        make_room(ROOT / "dist")
        make_room(ROOT / "dist" / APP_NAME)

    if not (ROOT / "models" / "moss-tts-nano-onnx" / "browser_poc_manifest.json").is_file():
        print("缺少 TTS 模型：models/moss-tts-nano-onnx/（见 README「资产获取」）")
        return 1
    if not (ROOT / "models" / "moss-audio-tokenizer-nano-onnx" / "codec_browser_onnx_meta.json").is_file():
        print("缺少音频解码器：models/moss-audio-tokenizer-nano-onnx/（见 README「资产获取」）")
        return 1
    if not (ROOT / "models" / "vosk-small-cn" / "am" / "final.mdl").is_file():
        print("缺少唤醒模型：models/vosk-small-cn/（见 README「唤醒词」）")
        return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",                     # 大应用用目录模式：启动不必解压
        "--windowed",                   # 无控制台黑窗
        "--name", APP_NAME,
        "--icon", str(ROOT / "pet" / "assets" / "icon.png"),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
        "--paths", str(ROOT),           # 让 vendor 包可被 import
    ]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    for pkg in COLLECT_ALL:
        cmd += ["--collect-all", pkg]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    cmd.append(str(ROOT / "main.py"))

    print("== PyInstaller 分析（约 1–3 分钟）==")
    make_room(ROOT / "dist" / APP_NAME)   # 开装前让位旧包（见 make_room 注释）
    run(cmd)

    print("== 复制资源 ==")
    for src_rel, dst_rel in DATA_DIRS:
        dst = ROOT / "dist" / APP_NAME / dst_rel
        if args.fast and dst.is_dir():
            print(f"  跳过 {src_rel}（--fast，已存在 {dir_size_mb(dst):.0f} MB）")
            if src_rel == "models/moss-voices":
                sync_voices(dst)      # 音色目录仍要同步
            continue
        copy_tree(ROOT / src_rel, dst)
    for src_rel, dst_rel in DATA_FILES:
        copy_file(ROOT / src_rel, ROOT / "dist" / APP_NAME / dst_rel)
        print(f"  复制 {src_rel}")

    exe = ROOT / "dist" / APP_NAME / f"{APP_NAME}.exe"
    total = dir_size_mb(ROOT / "dist" / APP_NAME)
    print(f"\n完成：{exe}（整包 {total:.0f} MB）")

    if not args.no_shortcut:
        make_shortcut(exe)
    print("\n双击 exe 或桌面快捷方式即可运行；首次启动会弹设置窗。")
    return 0


def make_shortcut(exe: Path) -> Path:
    """在桌面创建带自定义图标的快捷方式（WScript.Shell）。"""
    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():                     # OneDrive 重定向的桌面
        desktop = Path.home() / "OneDrive" / "Desktop"
    lnk = desktop / f"{APP_NAME}.lnk"
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{lnk}'); "
        f"$sc.TargetPath = '{exe}'; "
        f"$sc.WorkingDirectory = '{exe.parent}'; "
        f"$sc.IconLocation = '{exe},0'; "
        f"$sc.Description = '小鲸鱼桌宠'; "
        "$sc.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   check=True, capture_output=True)
    print(f"桌面快捷方式：{lnk}")
    return lnk


if __name__ == "__main__":
    raise SystemExit(main())
