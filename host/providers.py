"""多协议 API 客户端。

按席位配置 api_type 切换协议：
- "openai"（默认）：OpenAI 兼容 /chat/completions —— 中转站四家、DeepSeek、Kimi 官方均走这里；
- "anthropic"：Anthropic 原生 /v1/messages —— Claude 官方或支持原生协议的中转站。
"""
from __future__ import annotations

import os
import time
import tomllib
from dataclasses import dataclass, field

import httpx


@dataclass
class Player:
    name: str
    model: str
    base_url: str
    api_key_env: str
    api_type: str = "openai"
    api_key: str = field(default="", repr=False)  # 真实 key 不进日志


@dataclass
class ChatResult:
    player: str
    ok: bool
    content: str = ""
    latency: float = 0.0
    usage: dict = field(default_factory=dict)
    error: str = ""


def load_config(path: str) -> tuple[list[Player], dict]:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    settings = cfg.get("settings", {})
    players = [
        Player(
            name=p["name"],
            model=p["model"],
            base_url=p["base_url"].rstrip("/"),
            api_key_env=p["api_key_env"],
            api_type=p.get("api_type", "openai"),
            api_key=os.environ.get(p["api_key_env"], ""),
        )
        for p in cfg["player"]
    ]
    return players, settings


def _chat_openai(player: Player, messages: list[dict], temperature: float, timeout: float):
    headers = {"Authorization": f"Bearer {player.api_key}"}
    payload = {"model": player.model, "messages": messages, "temperature": temperature}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{player.base_url}/chat/completions", headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip(), data.get("usage", {})


def _chat_anthropic(player: Player, messages: list[dict], temperature: float, timeout: float):
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    headers = {
        "x-api-key": player.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload: dict = {
        "model": player.model,
        "max_tokens": 4096,
        "temperature": temperature,
        "messages": turns,
    }
    if system:
        payload["system"] = system
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{player.base_url}/messages", headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()
    content = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    raw = data.get("usage", {})
    usage = dict(raw)
    usage["total_tokens"] = raw.get("input_tokens", 0) + raw.get("output_tokens", 0)
    return content.strip(), usage


def chat(
    player: Player,
    messages: list[dict],
    *,
    temperature: float = 1.0,
    timeout: float = 120.0,
    max_retries: int = 2,
) -> ChatResult:
    if not player.api_key:
        return ChatResult(player.name, ok=False, error=f"环境变量 {player.api_key_env} 未设置")
    handler = _chat_anthropic if player.api_type == "anthropic" else _chat_openai
    start = time.monotonic()
    for attempt in range(max_retries + 1):
        try:
            content, usage = handler(player, messages, temperature, timeout)
            return ChatResult(
                player.name,
                ok=True,
                content=content,
                latency=time.monotonic() - start,
                usage=usage,
            )
        except Exception as e:  # 网络错误、HTTP 错误、格式错误统一走重试
            if attempt == max_retries:
                return ChatResult(
                    player.name,
                    ok=False,
                    latency=time.monotonic() - start,
                    error=f"{type(e).__name__}: {e}",
                )
            time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")
