"""《暴风夜》跑局管线包。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def resolve_game_dir(arg: str, base: Path | None = None) -> Path:
    """--game 参数解析：完整路径直接用；仅 id/目录名时自动到 game/ 下找（"42" → game/42）。
    两处都不存在时原样返回，由下游报"不是对局存档"。base 仅供测试注入。"""
    g = Path(arg)
    if g.is_dir():
        return g
    under = (base or ROOT / "game") / arg
    return under if under.is_dir() else g
