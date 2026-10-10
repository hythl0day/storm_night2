"""连通性测试：选定若干席位，各用角色口吻回答同一个问题，并汇总结果。

用法：
    python -m host.smoke                        # 交互式选择席位（可追加真人席位）
    python -m host.smoke --all                  # 全部席位，不询问
    python -m host.smoke --random 6             # 随机抽 6 席
    python -m host.smoke --only 狄西,奇米        # 按名字指定席位
    python -m host.smoke --human 老陈            # 追加真人席位（回答从键盘输入）
    python -m host.smoke --config host/players.example.toml --prompt "你为什么被困在古堡里？"
"""
from __future__ import annotations

import argparse
import random
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .providers import ChatResult, chat, load_config

# Windows 控制台默认 GBK，强制 UTF-8 输入输出（errors=replace 防止畸形字节崩溃）
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
if sys.stdin.encoding and sys.stdin.encoding.lower() != "utf-8":
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

PERSONAS_PATH = Path(__file__).parent / "personas.toml"

DEFAULT_PROMPT = "用一到两句话做自我介绍，要符合你的角色气质。"


def load_personas(path: Path) -> dict:
    if not path.exists():
        print(f"提示：未找到 {path}，所有人设将按'普通人'处理")
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def describe(name: str, personas: dict) -> str:
    p = personas.get(name)
    if not p:
        return "普通人"
    return (
        f"{p.get('gender', '?')}，{p.get('ethnicity', '?')}裔，"
        f"体格 {p.get('physique', '?')}。{p.get('persona', '')}"
    )


def ask(player, prompt: str, temperature: float, personas: dict) -> ChatResult:
    messages = [
        {
            "role": "system",
            "content": (
                f"你在身份推理游戏《暴风夜》中扮演{player.name}。"
                f"角色设定：{describe(player.name, personas)}。"
                "全程用中文、第一人称说话，保持角色气质。"
                "注意：以上人设只用于塑造台词风格与人物形象，不涉及你的推理能力——"
                "分析、判断与策略请完全发挥你的真实水平。"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    return chat(player, messages, temperature=temperature)


def ask_human(name: str, prompt: str) -> ChatResult:
    print(f"\n【{name}】（真人席位）请回答：{prompt}")
    text = input("  > ").strip()
    return ChatResult(f"{name}（真人）", ok=True, content=text or "（沉默）")


def pick_seats(players, args, personas):
    """返回 (选中的 AI 席位列表, 真人席位名或 None)。"""
    if args.all:
        return players, args.human
    if args.random is not None:
        n = min(args.random, len(players))
        return random.sample(players, n), args.human
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        chosen = [p for p in players if p.name in wanted]
        missing = wanted - {p.name for p in chosen}
        if missing:
            print(f"警告：配置中未找到席位 {sorted(missing)}")
        return chosen, args.human

    # 交互式选择
    print("可用席位：")
    for i, p in enumerate(players, 1):
        print(f"  {i}. {p.name}（{describe(p.name, personas)}）")
    raw = input("选择：all = 全部；r N = 随机 N 席；逗号分隔序号（如 1,3,5）：").strip().lower()
    if raw == "all":
        chosen = players
    elif raw.startswith("r"):
        num = raw[1:].strip()
        n = int(num) if num.isdigit() else len(players)
        chosen = random.sample(players, min(n, len(players)))
    else:
        idx = [int(s) - 1 for s in raw.split(",") if s.strip().isdigit()]
        chosen = [players[i] for i in idx if 0 <= i < len(players)]
    if not chosen:
        print("警告：未选择任何 AI 席位。")
    else:
        print("本局席位：" + "、".join(p.name for p in chosen))
    name = input("追加真人席位？输入真人角色名，直接回车跳过：").strip()
    return chosen, (name or None)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="host/players.toml")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--all", action="store_true", help="全部席位，不询问")
    ap.add_argument("--random", type=int, metavar="N", help="随机抽 N 席")
    ap.add_argument("--only", metavar="名字1,名字2", help="按名字指定席位")
    ap.add_argument("--human", metavar="名字", help="追加真人席位（可与以上组合）")
    args = ap.parse_args()

    players, settings = load_config(args.config)
    personas = load_personas(PERSONAS_PATH)
    temperature = float(settings.get("temperature", 1.0))

    chosen, human_name = pick_seats(players, args, personas)

    results: list[ChatResult] = []
    if human_name:
        results.append(ask_human(human_name, args.prompt))

    with ThreadPoolExecutor(max_workers=max(1, len(chosen))) as pool:
        results += list(pool.map(lambda p: ask(p, args.prompt, temperature, personas), chosen))

    ok = 0
    for r in results:
        if r.ok:
            ok += 1
            tokens = r.usage.get("total_tokens", "—")
            print(f"\n【{r.player}】({r.latency:.1f}s, {tokens} tokens)\n  {r.content}")
        else:
            print(f"\n【{r.player}】调用失败：{r.error}")
    print(f"\n== 汇总：{ok}/{len(results)} 席连通 ==")


if __name__ == "__main__":
    main()
