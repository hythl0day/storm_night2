"""多协议 API 客户端。

按席位配置 api_type 切换协议：
- "openai"（默认）：OpenAI 兼容 /chat/completions —— 中转站、DeepSeek、Kimi 官方均走这里；
- "anthropic"：Anthropic 原生 /v1/messages —— Claude 席位走这里（usage/缓存字段更真实；上游不吐思考块）。

思考落盘：ChatResult.reasoning 汇总隐藏思考（reasoning_content / thinking 块 / <think> 段），
由编排器写入 outbox 的 *-thinking.md；正文 content 不受影响（<think> 标签仍保留在正文里）。

所有请求不发送 temperature 参数（部分中转站上游只接受默认温度；全席统一不传，保证公平）。
max_tokens 解析顺序：单个 [[player]] 的 max_tokens > [settings] 的 max_tokens > DEFAULT_MAX_TOKENS。

流式：chat() 传 on_token 即走 SSE 流式，on_token(text, is_reasoning) 逐块回调（仅用于展示，
落盘/解析只用完整返回值）；上游不支持流式（4xx）时自动回退非流式一次。

超时分层：connect 10s；read 即停滞看门狗（任意两块数据的最长间隔，默认 90s）；
wall_limit 为单次调用总墙钟上限（默认 600s）。重试：4xx（除 429）不重试；
429 尊重 Retry-After；网络错误/5xx/停滞断开按指数退避。
"""
from __future__ import annotations

import json
import os
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

DEFAULT_MAX_TOKENS = 256000  # 单次输出上限默认值（输出预算，非上下文窗口）

CONNECT_TIMEOUT = 10.0
STALL_TIMEOUT = 90.0   # 停滞看门狗：流式任意两块数据之间的最长间隔
WALL_LIMIT = 600.0     # 单次调用总墙钟上限

TokenCallback = Callable[[str, bool], None]  # (文本块, 是否推理/思考内容)


@dataclass
class Player:
    name: str
    model: str
    base_url: str
    api_key_env: str
    id: str = ""
    api_type: str = "openai"
    max_tokens: int = DEFAULT_MAX_TOKENS
    reasoning_effort: str = ""  # "" = 不发参数（走渠道默认）；"low"/"medium"/"high"
    api_key: str = field(default="", repr=False)  # 真实 key 不进日志


@dataclass
class ChatResult:
    player: str
    ok: bool
    content: str = ""
    latency: float = 0.0
    usage: dict = field(default_factory=dict)
    error: str = ""
    finish_reason: str = ""  # 归一化为 OpenAI 风格；"length" = 被 max_tokens 截断
    reasoning: str = ""  # 隐藏思考（reasoning_content / thinking 块 / <think> 段），供落盘素材；正文 content 不受影响


def load_config(path: str) -> tuple[list[Player], dict]:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    settings = cfg.get("settings", {})
    default_max = int(settings.get("max_tokens", DEFAULT_MAX_TOKENS))
    default_effort = str(settings.get("reasoning_effort", "")).lower()
    if default_effort in ("", "none"):
        default_effort = ""
    players = [
        Player(
            name=p["name"],
            model=p["model"],
            base_url=p["base_url"].rstrip("/"),
            api_key_env=p["api_key_env"],
            id=p.get("id", ""),
            api_type=p.get("api_type", "openai"),
            max_tokens=int(p.get("max_tokens", default_max)),
            reasoning_effort=str(p.get("reasoning_effort", default_effort)).lower().replace("none", ""),
            api_key=os.environ.get(p["api_key_env"], ""),
        )
        for p in cfg["player"]
    ]
    return players, settings


def find_player(players: list[Player], key: str) -> Player | None:
    """按 id 或 name 查找席位（id 优先）。"""
    for p in players:
        if p.id == key:
            return p
    for p in players:
        if p.name == key:
            return p
    return None


def _timeout(stall: float) -> httpx.Timeout:
    return httpx.Timeout(connect=CONNECT_TIMEOUT, read=stall, write=30.0, pool=CONNECT_TIMEOUT)


def _check_wall(start: float, wall_limit: float) -> None:
    if time.monotonic() - start > wall_limit:
        raise TimeoutError(f"超过单次调用总墙钟上限 {wall_limit:.0f}s")


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        v = float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None
    return min(v, 30.0) if v > 0 else None


# ---- OpenAI 协议 ----

class _ThinkTagger:
    """OpenAI 兼容流中把 content 里的 <think>…</think> 段识别为推理内容（跨 chunk 安全）。

    部分模型（如 gpt 系）把思维链以 <think> 标签写进正文流而非 reasoning_content 字段；
    区间 token 以 is_reasoning=True 回调（落盘 content 仍保持完整，不受影响）。
    """
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.in_think = False
        self.tail = ""  # 跨 chunk 的标签碎片缓冲（标签最长 8 字符）

    @staticmethod
    def _partial_len(buf: str, tag: str) -> int:
        """buf 尾部与 tag 前缀匹配的最长长度（0..len(tag)-1）。"""
        for k in range(min(len(tag) - 1, len(buf)), 0, -1):
            if buf.endswith(tag[:k]):
                return k
        return 0

    def feed(self, chunk: str) -> list[tuple[str, bool]]:
        """返回 [(文本, is_reasoning)] 段；空段已剔除。碎片留在 tail 待下一块拼合。"""
        buf = self.tail + chunk
        self.tail = ""
        out: list[tuple[str, bool]] = []
        i = 0
        while i < len(buf):
            tag = self.CLOSE if self.in_think else self.OPEN
            j = buf.find(tag, i)
            if j == -1:
                keep = self._partial_len(buf, tag)
                seg = buf[i:len(buf) - keep]
                if seg:
                    out.append((seg, self.in_think))
                self.tail = buf[len(buf) - keep:]
                return out
            if j > i:
                out.append((buf[i:j], self.in_think))
            self.in_think = not self.in_think
            i = j + len(tag)
        return out

    def flush(self) -> list[tuple[str, bool]]:
        """流结束时吐出残余碎片（未闭合的 <think> 残段按当前状态归类）。"""
        out = [(self.tail, self.in_think)] if self.tail else []
        self.tail = ""
        return out


def _chat_openai(player, messages, stall, wall_limit, on_token, reasoning_effort):
    headers = {"Authorization": f"Bearer {player.api_key}"}
    payload: dict = {"model": player.model, "messages": messages, "max_tokens": player.max_tokens}
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    if on_token is not None:
        return _chat_openai_stream(player, payload, headers, stall, wall_limit, on_token)
    with httpx.Client(timeout=_timeout(stall)) as client:
        resp = client.post(f"{player.base_url}/chat/completions", headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    choice = data["choices"][0]
    reasoning = choice["message"].get("reasoning_content") or ""
    return choice["message"]["content"].strip(), data.get("usage", {}), choice.get("finish_reason") or "", reasoning


def _chat_openai_stream(player, payload, headers, stall, wall_limit, on_token):
    payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    parts: list[str] = []
    rparts: list[str] = []
    usage: dict = {}
    finish = ""
    tagger = _ThinkTagger()
    start = time.monotonic()
    with httpx.Client(timeout=_timeout(stall)) as client:
        with client.stream("POST", f"{player.base_url}/chat/completions",
                           headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                resp.read()  # 流式响应需先读取，错误体才可访问
            resp.raise_for_status()
            for line in resp.iter_lines():
                _check_wall(start, wall_limit)
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        parts.append(delta["content"])
                        for seg, is_think in tagger.feed(delta["content"]):
                            if is_think:
                                rparts.append(seg)  # <think> 段同时进思考存档（正文保持完整）
                            on_token(seg, is_think)
                    elif delta.get("reasoning_content"):
                        rparts.append(delta["reasoning_content"])
                        on_token(delta["reasoning_content"], True)
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
    for seg, is_think in tagger.flush():
        if is_think:
            rparts.append(seg)
        on_token(seg, is_think)
    return "".join(parts).strip(), usage, finish, "".join(rparts).strip()


# ---- Anthropic 协议 ----

def _anthropic_usage(raw: dict) -> dict:
    usage = dict(raw)
    # kiro 反代上游：input_tokens 恒 0，真实输入在 kiro_actual_input_tokens（含被缓存的部分）
    inp = raw.get("input_tokens", 0) or raw.get("kiro_actual_input_tokens", 0)
    usage["total_tokens"] = inp + raw.get("output_tokens", 0)
    return usage


def _anthropic_finish(stop_reason: str | None) -> str:
    """归一化为 OpenAI 风格：max_tokens → length。"""
    return {"max_tokens": "length"}.get(stop_reason or "", stop_reason or "")


# Anthropic 思考预算映射（OpenAI 风格 effort → thinking.budget_tokens）
_ANTHROPIC_THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 8192}


def _chat_anthropic(player, messages, stall, wall_limit, on_token, reasoning_effort):
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    headers = {
        "x-api-key": player.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload: dict = {"model": player.model, "max_tokens": player.max_tokens, "messages": turns}
    if system:
        payload["system"] = system
    if reasoning_effort and reasoning_effort in _ANTHROPIC_THINKING_BUDGET:
        payload["thinking"] = {"type": "enabled",
                               "budget_tokens": _ANTHROPIC_THINKING_BUDGET[reasoning_effort]}
    if on_token is not None:
        return _chat_anthropic_stream(player, payload, headers, stall, wall_limit, on_token)
    with httpx.Client(timeout=_timeout(stall)) as client:
        resp = client.post(f"{player.base_url}/messages", headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    content = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    reasoning = "".join(b.get("thinking", "") for b in data.get("content", []) if b.get("type") == "thinking")
    return content.strip(), _anthropic_usage(data.get("usage", {})), _anthropic_finish(data.get("stop_reason")), reasoning.strip()


def _chat_anthropic_stream(player, payload, headers, stall, wall_limit, on_token):
    payload = {**payload, "stream": True}
    parts: list[str] = []
    rparts: list[str] = []
    raw_usage: dict = {}
    finish = ""
    start = time.monotonic()
    with httpx.Client(timeout=_timeout(stall)) as client:
        with client.stream("POST", f"{player.base_url}/messages",
                           headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                resp.read()
            resp.raise_for_status()
            for line in resp.iter_lines():
                _check_wall(start, wall_limit)
                if not line.startswith("data:"):
                    continue
                try:
                    obj = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == "message_start":
                    raw_usage.update(obj.get("message", {}).get("usage", {}))
                elif t == "content_block_delta":
                    delta = obj.get("delta", {})
                    if delta.get("text"):
                        parts.append(delta["text"])
                        on_token(delta["text"], False)
                    elif delta.get("thinking"):
                        rparts.append(delta["thinking"])
                        on_token(delta["thinking"], True)
                elif t == "message_delta":
                    raw_usage.update(obj.get("usage", {}))
                    finish = obj.get("delta", {}).get("stop_reason") or finish
                elif t == "message_stop":
                    break
    return "".join(parts).strip(), _anthropic_usage(raw_usage), _anthropic_finish(finish), "".join(rparts).strip()


def chat(
    player: Player,
    messages: list[dict],
    *,
    on_token: TokenCallback | None = None,
    stall: float = STALL_TIMEOUT,
    wall_limit: float = WALL_LIMIT,
    max_retries: int = 3,
    reasoning_effort: str | None = None,
) -> ChatResult:
    """单次席位对话。on_token 不为 None 时走 SSE 流式（回调仅用于展示，返回值才是完整内容）。

    reasoning_effort（"low"/"medium"/"high"）：思考强度，默认 None = 不发该参数（走渠道默认）。
    OpenAI 协议发 reasoning_effort；Anthropic 协议映射为 thinking.budget_tokens。
    注意：部分上游不识别该参数会报 400——正式启用前应先用 src.bench --effort 验证渠道兼容性。
    """
    if not player.api_key:
        return ChatResult(player.name, ok=False, error=f"环境变量 {player.api_key_env} 未设置")
    handler = _chat_anthropic if player.api_type == "anthropic" else _chat_openai
    start = time.monotonic()
    stream = on_token is not None
    for attempt in range(max_retries + 1):
        try:
            content, usage, finish, reasoning = handler(player, messages, stall, wall_limit, on_token, reasoning_effort)
            return ChatResult(
                player.name,
                ok=True,
                content=content,
                latency=time.monotonic() - start,
                usage=usage,
                finish_reason=finish,
                reasoning=reasoning,
            )
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if stream:
                # 上游可能不支持流式：回退非流式一次，不消耗重试次数
                stream = False
                on_token = None
                continue
            if 400 <= code < 500 and code != 429:
                return ChatResult(
                    player.name, ok=False, latency=time.monotonic() - start,
                    error=f"HTTP {code}（4xx 不重试）: {e.response.text[:200]}",
                )
            if attempt == max_retries:
                return ChatResult(
                    player.name, ok=False, latency=time.monotonic() - start,
                    error=f"{type(e).__name__}: {e}",
                )
            time.sleep(_retry_after(e.response) or 2 * (attempt + 1))
        except Exception as e:  # 网络错误、停滞断开、墙钟超时、格式错误统一走重试
            if attempt == max_retries:
                return ChatResult(
                    player.name,
                    ok=False,
                    latency=time.monotonic() - start,
                    error=f"{type(e).__name__}: {e}",
                )
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")
