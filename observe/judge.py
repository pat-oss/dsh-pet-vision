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

from core.prompts import (PROMPT_VERSION, build_chat_messages, build_consolidate_messages,
                          build_observe_messages, build_proactive_messages,
                          parse_consolidate_json, parse_decision_json)
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
    def __init__(self, cfg: JudgeConfig, api_key: str,
                 on_usage=None, on_failure=None, notes=None) -> None:
        self.cfg = cfg
        self._client = OpenAI(api_key=api_key, base_url=cfg.base_url, timeout=cfg.timeout_sec)
        self._json_mode = True  # 首次尝试 response_format；不被支持则自动降级
        # 持久笔记（core/memory.NoteStore）。会话线程也会从 judge 上取它做提取，
        # 避免同一份存储散落在通道里各传一遍。
        self.notes = notes
        # 用量回调做成可选：不给回调时行为与以前完全一致（测试里的假 judge 不受影响）。
        # on_usage(prompt_tokens, completion_tokens, cached_tokens)
        self._on_usage = on_usage
        self._on_failure = on_failure

    def _rendered_notes(self) -> str:
        """给提示词尾部用的笔记片段；没有笔记或渲染失败都返回空串（调用方省略）。"""
        if self.notes is None:
            return ""
        try:
            return self.notes.render()
        except Exception:      # noqa: BLE001 笔记坏了不许影响对话
            return ""

    def _emit_usage(self, resp) -> None:
        """把 resp.usage 交给统计模块。各家字段名不一致，这里做兼容。"""
        if self._on_usage is None:
            return
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        prompt = _as_int(getattr(usage, "prompt_tokens", 0))
        completion = _as_int(getattr(usage, "completion_tokens", 0))
        try:
            self._on_usage(prompt, completion, _cached_tokens(usage))
        except Exception:      # noqa: BLE001 统计出错绝不能拖累主流程
            pass

    def _notify_failure(self) -> None:
        if self._on_failure is None:
            return
        try:
            self._on_failure()
        except Exception:      # noqa: BLE001
            pass

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
        messages = build_proactive_messages(frame_b64, recent, detail=self.cfg.detail,
                                            notes=self._rendered_notes())
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
                                       detail=self.cfg.detail,
                                       notes=self._rendered_notes())
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

    # ---- 记忆整理（会话结束后，让宠物自己把要点并入笔记） ----

    def consolidate(self, transcript: str) -> tuple[list[tuple[str, str]], list[str]]:
        """从一段对话里提炼长期记忆，并与已有笔记合并。返回 (remember, drop)。

        这是唯一一处"模型自动写记忆"的入口，所以：
        - 走 chat_model（纯文本任务，不需要视觉模型）；
        - 已有笔记用 render 的放宽参数（40 条 / 1500 字），比对话注入宽得多，
          否则模型看不到全量笔记，会重复追加而不是合并；
        - 任何异常都向上抛，由调用方（主线程的后台任务）决定怎么记日志——
          这里吞掉就变成"静默失效"，排查成本极高（本工程踩过四次）。
        """
        notes = ""
        if self.notes is not None:
            try:
                notes = self.notes.render(max_items=40, max_chars=1500)
            except Exception:      # noqa: BLE001 笔记坏了不该让整理整体失败
                notes = ""
        messages = build_consolidate_messages(transcript, notes)
        raw = self._chat(messages, self.cfg.chat_model, self.cfg.max_tokens_chat)
        return parse_consolidate_json(raw)

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
            try:
                resp = self._client.chat.completions.create(**kwargs)
            except Exception as exc:
                if self._json_mode and _mentions_response_format(exc):
                    self._json_mode = False  # 实验模型不支持 json_object：永久降级文本协议
                    kwargs.pop("response_format")
                    resp = self._client.chat.completions.create(**kwargs)
                else:
                    raise
        except Exception:
            self._notify_failure()   # 失败也要记账：成功率是体感的一部分
            raise
        self._emit_usage(resp)
        return resp.choices[0].message.content or ""


def _as_int(value) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _cached_tokens(usage) -> int:
    """命中缓存的输入 token 数。

    不同厂商/版本字段不一样，按顺序试，取到就停：
    - OpenAI 风格：usage.prompt_tokens_details.cached_tokens
    - DeepSeek 风格：usage.prompt_cache_hit_tokens
    取不到就当 0（命中率会显示为 0，而不是报错或虚高）。
    """
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        value = getattr(details, "cached_tokens", None)
        if isinstance(value, (int, float)):
            return int(value)
    for name in ("prompt_cache_hit_tokens", "cached_tokens"):
        value = getattr(usage, name, None)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _mentions_response_format(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "response_format" in msg or "json_object" in msg
