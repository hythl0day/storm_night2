"""视图层：读取 game/<id>/ 存档并渲染文本（与跑局解耦，逻辑只有一份）。

被 view.py（只读 CLI）与 console.py（交互驾驶舱）共用。
所有函数只读文件，不修改任何状态。
"""
from __future__ import annotations

import json
from pathlib import Path


def load_state(game_dir: Path) -> dict:
    return json.loads((game_dir / "state.json").read_text(encoding="utf-8"))


def _seat_dir(game_dir: Path, state: dict, key: str) -> Path | None:
    """按席位 id 或角色名定位 private/<id>/ 目录。"""
    for p in state["players"]:
        if p.get("id") == key or p["name"] == key:
            return game_dir / "private" / (p.get("id") or p["name"])
    return None


def render_status(game_dir: Path) -> str:
    """当前阶段 + 存活名单 + 身份台账（上帝视角）+ 道具/技能次数 + 保险箱状态。"""
    s = load_state(game_dir)
    ph = s["phase"]
    head = f"第{ph['day']}天 · {ph['stage']}"
    if s.get("winner"):
        head += f"｜已结束：{s['winner']}（{s.get('win_reason', '')}）"
    safe = f"保险箱：「{s.get('safe_item', '?')}」" + ("（已打开）" if s.get("safe_open") else "（未打开）")
    safe += f"｜侦探：{s.get('detective') or '无'}"
    table = "、".join(f"「{e['name']}」×{e['uses_left']}" for e in s.get("table_items", [])) or "（空）"
    safe += f"｜桌上：{table}"
    if s.get("power_out"):
        safe += "｜停电中"
    lines = [head, safe]
    skills = s.get("skill_uses", {})
    positions = s.get("positions", {})
    for p in s["players"]:
        mark = "活" if p["alive"] else "亡"
        pos = positions.get(p["name"], "？")
        items = "、".join(f"{e['name']}×{e['uses_left']}" for e in p.get("items", [])) or "无"
        sk = skills.get(p["name"], {})
        sk_text = "、".join(f"{k}×{v}" for k, v in sk.items()) or "无"
        lines.append(
            f"  {p['name']}（{mark}）@ {pos}｜{p['identity']}/{p['profession']}｜道具：{items}｜技能：{sk_text}"
        )
    feed = s.get("orch", {}).get("feed", [])
    lines.append(f"公开事件 {len(feed)} 条｜案件 {len(s.get('cases', []))} 起")
    return "\n".join(lines)


def render_log(game_dir: Path, n: int = 20) -> str:
    """public/log.md 末尾 n 行。"""
    path = game_dir / "public" / "log.md"
    if not path.exists():
        return "（公开日志尚不存在）"
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-n:]) if lines else "（公开日志为空）"


def render_peek(game_dir: Path, key: str) -> str:
    """某席私密视角：私密卡 + 私人笔记 + 内心独白。"""
    s = load_state(game_dir)
    d = _seat_dir(game_dir, s, key)
    if d is None:
        names = "、".join(p["name"] for p in s["players"])
        return f"未找到席位 {key!r}，本局玩家：{names}"
    parts = []
    for fname, title in (("card.md", "私密卡"), ("notes.md", "私人笔记"), ("inner.md", "内心独白")):
        path = d / fname
        text = path.read_text(encoding="utf-8").strip() if path.exists() else "（无）"
        parts.append(f"—— {title} ——\n{text}")
    return "\n\n".join(parts)


def render_inner(game_dir: Path, key: str) -> str:
    """某席内心独白全文。"""
    s = load_state(game_dir)
    d = _seat_dir(game_dir, s, key)
    if d is None:
        return f"未找到席位 {key!r}"
    path = d / "inner.md"
    return path.read_text(encoding="utf-8").strip() if path.exists() else "（该席还没有内心独白）"


def render_host_log(game_dir: Path, n: int = 10) -> str:
    """host_log.jsonl 末尾 n 条（调用耗时/成败台账）。"""
    path = game_dir / "host_log.jsonl"
    if not path.exists():
        return "（host_log.jsonl 尚不存在）"
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-n:]:
        try:
            e = json.loads(line)
            flag = "ok" if e.get("ok") else "FAIL"
            extra = f"｜{e['error'][:60]}" if e.get("error") else ""
            out.append(f"{e.get('ts', '?')} {e.get('player', '?')} {e.get('stage', '?')} "
                       f"{flag} {e.get('latency', 0):.1f}s tok={e.get('total_tokens')}{extra}")
        except json.JSONDecodeError:
            out.append(line)
    return "\n".join(out) if out else "（host_log 为空）"
