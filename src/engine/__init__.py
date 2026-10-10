"""暴风夜 6 人局规则引擎（M3）：纯裁决逻辑库，无 AI/网络/打印。

用法：
    from src.engine import Engine, load_state
    eng = Engine(load_state("game/000042/state.json"))
    ev = eng.advance()          # 推进阶段
    ev = eng.murder("A", "C")   # 谋杀裁决（详见 core.Engine 各方法）

事件结构：{"ok", "type", "public", "private", ...}；驳回（ok=False）不改变状态且不公开。
"""
from __future__ import annotations

from .core import Engine
from .state import load_state, save_state

__all__ = ["Engine", "load_state", "save_state"]
