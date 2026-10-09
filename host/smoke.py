"""连通性测试：六个席位各用角色口吻回答同一个问题，并汇总结果。

用法：
    python -m host.smoke                                   # 默认自我介绍
    python -m host.smoke --prompt "你为什么被困在古堡里？"
    python -m host.smoke --config host/players.example.toml
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

from .providers import chat, load_config

# Windows 控制台默认 GBK，强制 UTF-8 输出
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 临时人设，仅用于连通性测试；正式人设卡后续单独定稿
PERSONAS = {
    "查特": "傲慢自负的精英",
    "桑娜": "冷静理性的分析者",
    "杰米娜": "敏锐犀利的质疑者",
    "狄西": "沉默寡言的技术宅",
    "智谱": "严谨掉书袋的学院派",
    "奇米": "刚来报到的单纯新人",
}

DEFAULT_PROMPT = "用一到两句话做自我介绍，要符合你的角色气质。"


def ask(player, prompt: str, temperature: float):
    persona = PERSONAS.get(player.name, "普通人")
    messages = [
        {
            "role": "system",
            "content": (
                f"你在身份推理游戏《暴风夜》中扮演{player.name}，人设：{persona}。"
                "全程用中文、第一人称说话。"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    return chat(player, messages, temperature=temperature)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="host/players.toml")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    args = ap.parse_args()

    players, settings = load_config(args.config)
    temperature = float(settings.get("temperature", 1.0))

    with ThreadPoolExecutor(max_workers=len(players)) as pool:
        results = list(pool.map(lambda p: ask(p, args.prompt, temperature), players))

    ok = 0
    for r in results:
        if r.ok:
            ok += 1
            tokens = r.usage.get("total_tokens", "?")
            print(f"\n【{r.player}】({r.latency:.1f}s, {tokens} tokens)\n  {r.content}")
        else:
            print(f"\n【{r.player}】调用失败：{r.error}")
    print(f"\n== 汇总：{ok}/{len(results)} 席连通 ==")


if __name__ == "__main__":
    main()
