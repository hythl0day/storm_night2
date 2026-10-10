"""只读查看器（M4.5）：读取 game/<id>/ 存档，与跑局解耦——跑完的局、批量跑局旁路查看都用它。

用法：
    python -m src.view status --game game/012345        # 阶段/存活/身份台账/道具技能次数
    python -m src.view log --game game/012345 [-n 30]   # 公开日志末尾 n 行
    python -m src.view peek 杰米娜 --game game/012345   # 私密卡 + 笔记 + 内心独白
    python -m src.view inner 杰米娜 --game game/012345  # 仅内心独白
    python -m src.view host --game game/012345 [-n 10]  # API 调用台账（耗时/成败）
    python -m src.view follow --game game/012345 [--interval 5]   # 轮询刷新追局，Ctrl+C 退出
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import viewer

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(prog="view", description="《暴风夜》对局只读查看器")
    ap.add_argument("command", choices=["status", "log", "peek", "inner", "host", "follow"])
    ap.add_argument("seat", nargs="?", default="", help="席位 id 或角色名（peek/inner 用）")
    ap.add_argument("--game", required=True, help="对局目录，如 game/012345")
    ap.add_argument("-n", type=int, default=20, help="log/host 显示条数")
    ap.add_argument("--interval", type=float, default=5.0, help="follow 轮询间隔秒数")
    args = ap.parse_args()

    game_dir = Path(args.game)
    if not (game_dir / "state.json").exists():
        raise SystemExit(f"找不到 {game_dir / 'state.json'}")

    if args.command == "status":
        print(viewer.render_status(game_dir))
    elif args.command == "log":
        print(viewer.render_log(game_dir, args.n))
    elif args.command == "peek":
        if not args.seat:
            raise SystemExit("peek 需要指定席位 id 或角色名")
        print(viewer.render_peek(game_dir, args.seat))
    elif args.command == "inner":
        if not args.seat:
            raise SystemExit("inner 需要指定席位 id 或角色名")
        print(viewer.render_inner(game_dir, args.seat))
    elif args.command == "host":
        print(viewer.render_host_log(game_dir, args.n))
    elif args.command == "follow":
        try:
            while True:
                print("\x1b[2J\x1b[H", end="")  # 清屏并回左上角
                print(viewer.render_status(game_dir))
                print("\n—— 最新公开日志 ——")
                print(viewer.render_log(game_dir, args.n))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
