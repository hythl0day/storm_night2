"""API 速度诊断：量化各席位的 TTFT / 生成速度 / 总时长（均走流式）。

每个席位两种请求：
  短：只回一个字 —— 测连接与首 token 延迟（TTFT）；
  长：完整规则书作 system + 要求约 500 字输出 —— 模拟实战负载，测生成速度。

用法：
    python tools/bench.py                      # 全部席位 × 2 种请求（串行）
    python tools/bench.py --seats kimi,deepseek
    python tools/bench.py --skip-long          # 只跑短请求

注意：会真实调用 API（每席位至多 2 次，长请求约 500 字输出），产生少量费用。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.providers import chat, load_config  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SHORT_MSG = [{"role": "user", "content": "你好，请只回复一个字：在。"}]

# 思考强度测试题：3 3 8 8 算 24（答案 8/(3-8/3)），触发推理但可见输出极短，
# 耗时差异主要反映隐藏思考量。只回答算式以压短输出。
EFFORT_MSG = [{"role": "user", "content": (
    "用 3、3、8、8 四个数（每个恰好用一次）算出 24。只回答一个算式，不要解释。")}]


def long_messages() -> list[dict]:
    rules = (ROOT / "docs" / "rules-6.md").read_text(encoding="utf-8")
    return [
        {"role": "system", "content": "你在身份推理游戏《暴风夜》中扮演一名玩家。以下是规则书：\n\n" + rules},
        {"role": "user", "content": (
            "现在是第 1 天公共发言，轮到你。请用中文写一段约 500 字的分析："
            "结合规则书谈谈首夜信息交换的策略、你会重点关注哪些房间与道具、以及你对其他玩家的初步观察角度。"
            "直接输出正文即可，不要 JSON。")},
    ]


def probe(player, messages: list[dict], label: str, reasoning_effort: str | None = None) -> str:
    stats = {"ttft": None, "chars": 0}
    start = time.monotonic()

    def on_token(text: str, is_reasoning: bool) -> None:
        if stats["ttft"] is None:
            stats["ttft"] = time.monotonic() - start
        stats["chars"] += len(text)

    r = chat(player, messages, on_token=on_token, reasoning_effort=reasoning_effort, wall_limit=300.0)
    total = time.monotonic() - start
    if not r.ok:
        return f"{player.id:<9} {label}  失败：{r.error[:80]}"
    ttft = stats["ttft"] or 0.0
    gen = total - ttft
    speed = stats["chars"] / gen if gen > 0 else 0.0
    cached = ((r.usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return (f"{player.id:<9} {label}  TTFT {ttft:6.1f}s  总 {total:6.1f}s  "
            f"{stats['chars']:5d} 字  {speed:5.1f} 字/s  tok={r.usage.get('total_tokens')}"
            f"（缓存命中 {cached}）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "players.toml"))
    ap.add_argument("--seats", default="", help="逗号分隔的席位 id；默认全部")
    ap.add_argument("--skip-long", action="store_true")
    ap.add_argument("--effort", action="store_true",
                    help="思考强度对照：每席位依次测 默认/low/high（--with-medium 加测 medium）")
    ap.add_argument("--with-medium", action="store_true")
    args = ap.parse_args()

    players, _ = load_config(args.config)
    if args.seats:
        keep = set(args.seats.split(","))
        players = [p for p in players if p.id in keep]
    print(f"诊断 {len(players)} 个席位（流式，串行执行）。注意：会产生真实 API 调用。\n")
    print(f"{'席位':<9} 类型   指标")
    for p in players:
        if not p.api_key:
            print(f"{p.id:<9} 跳过：环境变量 {p.api_key_env} 未设置")
            continue
        if args.effort:
            levels = [None, "low", "high"]
            if args.with_medium:
                levels.insert(2, "medium")
            for lvl in levels:
                print(probe(p, EFFORT_MSG, f"effort={lvl or '默认'}", reasoning_effort=lvl), flush=True)
        else:
            print(probe(p, SHORT_MSG, "短"), flush=True)
            if not args.skip_long:
                print(probe(p, long_messages(), "长"), flush=True)


if __name__ == "__main__":
    main()
