"""最小连通性测试：只测一个席位，输出详细错误（HTTP 状态码 + 响应体），用于定位连接问题。

用法：
    python -m src.ping deepseek             # 按席位 id 点名
    python -m src.ping 狄西                 # 按席位名点名
    python -m src.ping gpt --timeout 120    # 调整超时（默认 60s）
    python -m src.ping gpt --insecure       # 跳过 TLS 证书校验（仅调试用）
    python -m src.ping kimi --config src/players.example.toml
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

from .providers import find_player, load_config

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

TEST_MESSAGE = "你好，请只回复一个字：在。"


def probe(player, timeout: float, insecure: bool = False) -> None:
    key = os.environ.get(player.api_key_env, "")
    print(f"席位：{player.name}（id={player.id or '无'}）")
    print(f"  api_type = {player.api_type}")
    print(f"  base_url = {player.base_url}")
    print(f"  model    = {player.model}")
    print(f"  max_tokens = {player.max_tokens}")
    print(f"  key      = {player.api_key_env}（{'已设置，长度 ' + str(len(key)) if key else '未设置！'}）")
    if not key:
        return
    if insecure:
        print("  警告：--insecure 已跳过 TLS 证书校验（仅用于调试）")

    if player.api_type == "anthropic":
        url = f"{player.base_url}/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        payload = {
            "model": player.model,
            "max_tokens": player.max_tokens,
            "messages": [{"role": "user", "content": TEST_MESSAGE}],
        }
    else:
        url = f"{player.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": player.model,
            "max_tokens": player.max_tokens,
            "messages": [{"role": "user", "content": TEST_MESSAGE}],
        }

    print(f"  请求 POST {url}")
    start = time.monotonic()
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=timeout, verify=not insecure)
    except Exception as e:
        print(f"  结果：连接层失败（{type(e).__name__}: {e}）")
        print("  → 网络/DNS/代理问题，请求根本没拿到 HTTP 响应")
        return
    latency = time.monotonic() - start

    print(f"  HTTP {resp.status_code}（{latency:.1f}s）")
    if resp.status_code == 200:
        data = resp.json()
        if player.api_type == "anthropic":
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        else:
            msg = data["choices"][0]["message"]
            text = msg.get("content") or ""
            reasoning = msg.get("reasoning_content")
            if reasoning:
                print(f"  （推理摘要：{reasoning[:200]!r}）")
        print(f"  结果：成功，回复：{text.strip()!r}，usage={data.get('usage', {})}")
    else:
        print(f"  结果：HTTP 错误，响应体（前 800 字符）：\n{resp.text[:800]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("seat", help="players.toml 中的席位 id 或名字")
    ap.add_argument("--config", default="src/players.toml")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（仅调试）")
    args = ap.parse_args()

    players, _ = load_config(args.config)
    player = find_player(players, args.seat)
    if player is None:
        names = "、".join(f"{p.id}|{p.name}" if p.id else p.name for p in players)
        print(f"未找到席位 {args.seat!r}，配置中只有：{names}")
        sys.exit(1)
    probe(player, args.timeout, args.insecure)


if __name__ == "__main__":
    main()
