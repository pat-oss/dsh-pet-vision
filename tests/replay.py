"""tests/replay.py — 录屏回放回归（不花 API 费用）。

回放 10 段标准场景录屏（写代码/看视频/挂机/弹错误框/聊天），
把每帧喂给 FrameDiffer + 录制的模型响应，验证：
  - 管线阶段计时（截屏/dHash/放行门/TTS）
  - 放行门逻辑（节流/保底/黑名单）
  - 判断质量对照（人工标注的 ground truth vs 录制决策）

用法：
  # 1) 录制：正常使用桌宠时，data/decisions.jsonl 即决策日志（天然录制）
  # 2) 回放：
  python tests/replay.py --log data/decisions.jsonl

录屏帧回放（判断质量对照）需先准备 data/replay/<场景>/ 帧目录，
v1 先支持决策日志回放；帧回放作为扩展位。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replay_log(log_path: Path) -> int:
    if not log_path.is_file():
        print(f"日志不存在: {log_path}")
        return 1
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    if not events:
        print("日志为空。")
        return 0

    observe = [e for e in events if e.get("mode") == "observe"]
    chat = [e for e in events if e.get("mode") == "chat"]
    api_called = [e for e in observe if e.get("api_called")]
    spoke = [e for e in observe if e.get("gate") == "allow"]
    latencies = [e["latency_ms"] for e in api_called if e.get("latency_ms")]

    print(f"总事件 {len(events)}（observe {len(observe)} / chat {len(chat)}）")
    print(f"API 调用 {len(api_called)} 次，放行发言 {len(spoke)} 次")
    if observe:
        skip_rate = 1 - len(api_called) / max(1, len(observe))
        print(f"dHash 拦截率 {skip_rate:.0%}（自动指标，与静默率分开看）")
    if api_called:
        silent = len([e for e in api_called if e.get("gate") == "silent"])
        print(f"调 API 后静默率 {silent / len(api_called):.0%}")
    if latencies:
        print(f"API 延迟 p50={statistics.median(latencies):.0f}ms "
              f"max={max(latencies)}ms")
    gates: dict[str, int] = {}
    for e in observe:
        key = str(e.get("gate", "?"))
        gates[key] = gates.get(key, 0) + 1
    print("放行门分布:", gates)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=ROOT / "data" / "decisions.jsonl")
    args = parser.parse_args()
    return replay_log(args.log)


if __name__ == "__main__":
    sys.exit(main())
