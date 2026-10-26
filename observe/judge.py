"""observe/judge.py — VisionJudge：DeepSeek OpenAI 兼容客户端封装。

观察与会话两模式共用同一 system 前缀（core/prompts.py），吃上下文缓存。
模型名只从配置来，不散落进代码（官方注明旧模型名会转由新模型服务）。
"""

from __future__ import annotations

import base64
import io
import time
from typing import Iterable

from openai import OpenAI
from PIL import Image

from core.prompts import (PROMPT_VERSION, build_chat_messages, build_observe_messages,
                          build_proactive_messages, parse_decision_json)
from core.types import Decision, JudgeConfig, Turn


def encode_frame(img: Image.Image, quality: int = 85) -> str:
    """PIL 图像 → base64 JPEG（DeepSeek 视觉 API 支持 JPEG，按内容识别格式）。"""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def classify_error(exc: Exception) -> str:
    """错误分类：transient / quota / permanent。

    注意：本函数在 except 分支里被调用，自己再抛异常会把整个观察线程带崩，
    所以保持纯字符串操作、不碰 exc 的可选属性。
    """
    name = type(exc).__name__
    msg = str(exc).lower()
    if "ratelimit" in name.lower() or "rate limit" in msg or "429" in msg:
        return "quota"
    if "timeout" in name.lower() or "timed out" in msg or "connection" in msg:
        return "transient"
    if "auth" in msg or "api_key" in msg or "401" in msg or "403" in msg:
        return "permanent"
    if "400" in msg or "invalid" in msg:
        return "permanent"
    return "transient"


class VisionJudge:
    def __init__(self, cfg: JudgeConfig, api_key: str) -> None:
        self.cfg = cfg
        self._client = OpenAI(api_key=api_key, base_url=cfg.base_url, timeout=cfg.timeout_sec)
        self._json_mode = True  # 首次尝试 response_format；不被支持则自动降级

    # ---- 观察模式 ----

    def judge(self, frame_b64: str, recent: str) -> Decision:
        started = time.monotonic()
        messages = build_observe_messages(frame_b64, recent, detail=self.cfg.detail)
        raw = self._chat(messages, self.cfg.vision_model, self.cfg.max_tokens_observe)
        latency_ms = int((time.monotonic() - started) * 1000)
        speak, text, reason = parse_decision_json(raw)
        if speak and text and len(text) > 80:
            text = text[:80]
        return Decision(speak=speak, text=text, reason=reason,
                        model=self.cfg.vision_model, prompt_version=PROMPT_VERSION,
                        latency_ms=latency_ms)

    # ---- 主动搭话（到点自己开口，视觉开着就结合画面） ----

    def proactive(self, frame_b64: str | None, recent: str) -> Decision:
        """主动互动：不管画面有没有变，都请她说一句话。

        与 judge() 的区别是任务不同——judge 是"要不要说"，这里是"说什么"，
        所以用独立的 system 提示（prompts.proactive_system_prompt）。
        """
        started = time.monotonic()
        messages = build_proactive_messages(frame_b64, recent, detail=self.cfg.detail)
        raw = self._chat(messages, self.cfg.chat_model, self.cfg.max_tokens_chat)
        latency_ms = int((time.monotonic() - started) * 1000)
        speak, text, reason = parse_decision_json(raw)
        if not speak or not text:
            # 兜底：主动开口的时机不该静默——把正文当话说（模型偶尔直接吐纯文本）
            plain = (raw or "").strip()
            if plain and not plain.startswith("{") and len(plain) <= 60:
                speak, text, reason = True, plain, reason or "plain-text"
        if speak and text and len(text) > 60:
            text = text[:60]
        return Decision(speak=bool(speak and text), text=text, reason=reason,
                        model=self.cfg.chat_model, prompt_version=PROMPT_VERSION,
                        latency_ms=latency_ms)

    # ---- 会话模式 ----

    def reply(self, user_text: str, history: Iterable[Turn],
              frame_b64: str | None = None) -> str:
        """会话回复。frame_b64 非空时附带当前屏幕截图——
        否则主人说"看看我屏幕"她收不到图，只能凭文字瞎猜。"""
        messages = build_chat_messages(list(history), user_text, frame_b64,
                                       detail=self.cfg.detail)
        budget = self.cfg.max_tokens_chat
        text = self._chat(messages, self.cfg.chat_model, budget).strip()
        if not text and budget is not None:
            # 只有用户自己设了上限时才可能"预算被思考吃光、正文为空"
            # （finish_reason=length）。默认无上限走不到这里。
            # 加大预算重试一次，给推理收束空间；仍空则返回空串，
            # 由 ChatSession 兜底（保证气泡/语音不消失）。
            try:
                text = self._chat(messages, self.cfg.chat_model,
                                  max(int(budget) * 3, 2400)).strip()
            except Exception:  # noqa: BLE001 重试失败不致命
                text = ""
        return text

    # ---- 内部 ----

    def _chat(self, messages: list[dict], model: str,
              max_tokens: int | None = None) -> str:
        """max_tokens=None 表示不限制：不向 API 传该字段，由模型自然停。"""
        kwargs: dict = {"model": model, "messages": messages}
        if max_tokens is not None:
            kwargs["max_tokens"] = int(max_tokens)
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            if self._json_mode and _mentions_response_format(exc):
                self._json_mode = False  # 实验模型不支持 json_object：永久降级文本协议
                kwargs.pop("response_format")
                resp = self._client.chat.completions.create(**kwargs)
            else:
                raise
        return resp.choices[0].message.content or ""


def _mentions_response_format(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "response_format" in msg or "json_object" in msg
