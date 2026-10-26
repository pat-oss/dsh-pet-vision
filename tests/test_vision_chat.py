"""tests/test_vision_chat.py — 会话带图 + 截断 JSON 抢救。

背景（用户实测反馈）：
  1. 主人说"看看我屏幕"，她却答不上来——因为 ChatSession 只把文本发给
     judge.reply()，**从不附当前截图**。她压根收不到图像。
  2. 观察模式偶发整条决策作废：reason 写太长被 max_tokens 截断成半个
     JSON，parse_decision_json 的 rindex("}") 直接抛错，白烧一次 API。

这里钉住：视觉开启时会话必须带帧；截断的 JSON 必须能抢救。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---- 消息构造：带图与不带图 ----

def test_chat_messages_attach_frame() -> None:
    from core.prompts import build_chat_messages

    msgs = build_chat_messages([], "看看我屏幕上是什么", frame_b64="QUJD")
    last = msgs[-1]
    assert isinstance(last["content"], list), (
        "传了 frame_b64 却没把它附进消息——主人问屏幕她仍然看不到"
    )
    assert [p["type"] for p in last["content"]] == ["text", "image_url"]
    assert last["content"][1]["image_url"]["url"].endswith("QUJD")
    assert last["content"][0]["text"] == "看看我屏幕上是什么"


def test_chat_messages_without_frame_stays_plain_text() -> None:
    from core.prompts import build_chat_messages

    msgs = build_chat_messages([], "你好呀")
    assert msgs[-1]["content"] == "你好呀", "无图时不该退化成列表形式"


# ---- 截断 JSON 抢救 ----

def test_truncated_json_is_salvaged() -> None:
    from core.prompts import parse_decision_json

    # 真实日志里抓到的样本：reason 写一半被 max_tokens 切断，没有闭合括号
    raw = ('{"speak": false, "reason": "画面是在查看 AI 智能体配置和聊天窗口的'
           '日常办公内容，时间 2')
    speak, text, reason = parse_decision_json(raw)
    assert speak is False
    assert text is None
    assert "salvage" in reason, reason


def test_truncated_json_keeps_text_when_present() -> None:
    from core.prompts import parse_decision_json

    raw = '{"speak": true, "text": "主人又在加班啦", "reason": "深夜还在工作，值'
    speak, text, reason = parse_decision_json(raw)
    assert speak is True
    assert text == "主人又在加班啦", f"text 丢了：{text!r}"
    assert "salvage" in reason


def test_valid_json_still_parses_normally() -> None:
    from core.prompts import parse_decision_json

    speak, text, reason = parse_decision_json(
        '{"speak": true, "text": "哇，主人吃饭啦", "reason": "美食画面"}')
    assert (speak, text) == (True, "哇，主人吃饭啦")
    assert reason == "美食画面"
    assert "salvage" not in reason


def test_empty_output_is_not_salvaged() -> None:
    from core.prompts import parse_decision_json

    speak, text, reason = parse_decision_json("")
    assert speak is False and text is None
    assert "unparseable" in reason


# ---- 会话真的把帧取回来 ----

def _session(frame_source):
    from chat.session import ChatSession
    return ChatSession(None, None, None, None, None, "你好",
                       frame_source=frame_source)


def test_session_uses_frame_source() -> None:
    assert _session(lambda: "FRAME-ABC")._current_frame_b64() == "FRAME-ABC"


def test_session_without_frame_source_returns_none() -> None:
    assert _session(None)._current_frame_b64() is None


def test_session_survives_failing_frame_source() -> None:
    def boom() -> str:
        raise RuntimeError("截屏挂了")

    assert _session(boom)._current_frame_b64() is None, (
        "截屏失败不能让会话崩——要静默降级成纯文本"
    )


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("\n全部通过。")


if __name__ == "__main__":
    main()
