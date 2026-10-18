"""采访录制器（show-bible §5）：把某席位 AI 拉出古堡进行自由问答采访。

记忆载荷从存档逐字节复原（不经引擎重建）：
- 第一编「你经历的一切」：state.json 的 orch.feed（公共事件流）+ orch.hist[名字]（私密事件流）
  按（天, 阶段序）归并；无时间戳的发言行归入其上一条带戳条目的阶段；反派密谋不进 feed/hist，
  从 private/<席位>/log.md 的（反派密谋）节补入。
- 第二编「你的发言与内心全记录」：outbox/<席位>/dayN-*.md 的 speech/inner 全文 + 最终版 notes。
--at 天:阶段 把记忆冻结在该阶段（含）为止（局中冻结采访："采访死亡前 10 分钟的他"）。

用法：
    python -m src.interview --selected 976488 --seat deepseek                     # 赛后采访（全部记忆）
    python -m src.interview --selected 976488 --seat deepseek --at 1:晚间休息      # 局中冻结采访
    python -m src.interview --selected 976488 --seat deepseek --no-inner          # 记忆不含内心独白
    python -m src.interview --selected 976488 --seat deepseek --dump-memory m.md  # 只导出记忆档案（供人工剪裁）
    python -m src.interview --selected 976488 --seat deepseek --memory m.md       # 用剪裁后的记忆采访（明线注入）

--selected 只接受 selected/ 下的对局 id 或显式路径（采访以精选池为默认目录：先入选，再采访）。

归档：<对局>/interview/<seat>-<时间戳>.md（问答全记录，宿主侧素材，含剧透，不进 public/）；
同目录 <seat>-<时间戳>.memory.md 保存本次实际使用的记忆档案（可追溯）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import ROOT, resolve_game_dir
from .engine.state import load_state
from .prompts import render
from .providers import chat, find_player, load_config
from .smoke import describe, load_personas

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stdin.encoding and sys.stdin.encoding.lower() != "utf-8":
    sys.stdin.reconfigure(encoding="utf-8")

STAGE_ORDER = {"开局准备": 0, "案件调查": 1, "公共发言": 2, "投票": 3, "自由活动": 4, "晚间休息": 5}

STAMP_RE = re.compile(r"^〔第(\d+)天·([^〕]+)〕")
MORNING_RE = re.compile(r"第\s*(\d+)\s*/\s*6\s*天开始")  # 清晨状态行不打戳（见 orchestrator._stamp）
OUTBOX_RE = re.compile(r"^day(\d+)-(.+)\.md$")

# outbox 文件标签 → 所属阶段（用于 --at 过滤与排序）；无法从标签区分阶段的（毁证机会/赠送回应）→ None
LABEL_STAGE = {
    "竞选声明": "公共发言", "竞选退选": "公共发言", "竞选投票": "公共发言", "公共发言": "公共发言",
    "投票": "投票", "武装指认": "投票", "遗言": "投票", "遗嘱回应": "投票",
    "案件调查": "案件调查", "死者字条": "案件调查",
}

SITUATION_POST = (
    "你刚刚经历的这局《暴风夜》已经落幕。现在你被暂时带离古堡，坐在采访间里。对面是这档节目的"
    "「制作人」——这场游戏的设立者。他知道规则与一切答案，但不必把他当成立场上的朋友或敌人："
    "如实回答、隐瞒、撒谎、反问，都是你的自由。"
)
SITUATION_MID = (
    "你正在经历的这局《暴风夜》还在继续。你被短暂地带离古堡，坐在采访间里——制作人告诉你："
    "聊几句就送你回去，游戏里的其他人不会察觉你离开过。注意：你的记忆只到被带离的那一刻为止。"
)


def _stage_key(day: int, stage: str | None) -> tuple[int, int]:
    return (day, STAGE_ORDER.get(stage or "", 99))


def _parse_timeline(lines: list[str]) -> list[tuple[int, str, str]]:
    """把带〔第N天·阶段〕戳的事件流解析为 (天, 阶段, 正文)；无戳发言行继承上一条的阶段。"""
    out: list[tuple[int, str, str]] = []
    cur = (0, "开局准备")
    for line in lines:
        s = line.strip()
        if s.startswith("- "):
            s = s[2:]
        m = STAMP_RE.match(s)
        if m:
            cur = (int(m.group(1)), m.group(2))
            s = s[m.end():].strip()
        else:
            mm = MORNING_RE.search(s)
            if mm:  # "第 N/6 天开始" 是次日清晨状态行（_stamp 不打戳），归入清晨
                cur = (int(mm.group(1)), "案件调查")
        if s:
            out.append((cur[0], cur[1], s))
    return out


def _conspiracy_entries(log_path: Path) -> list[tuple[int, str, str]]:
    """从 private/<席位>/log.md 提取（反派密谋）节（密谋不进 feed/hist，仅此一处存档）。"""
    if not log_path.exists():
        return []
    out = []
    for sec in re.split(r"^## ", log_path.read_text(encoding="utf-8"), flags=re.M):
        m = re.match(r"第(\d+)天 · (.+?)（反派密谋）", sec)
        if m:
            body = sec.split("\n", 1)[1].strip() if "\n" in sec else ""
            for line in body.splitlines():
                if line.strip():
                    out.append((int(m.group(1)), m.group(2), f"（反派密谋）{line.strip()}"))
    return out


def _outbox_entries(game_dir: Path, seat: str, with_inner: bool) -> tuple[list[tuple[int, str, str, str, str]], str]:
    """读取 outbox/<席位>/ payloads → [(天, 阶段|None, 标签, speech, inner)]（按时间排序）；附带最终版 notes。"""
    entries = []
    notes = ""
    notes_path = game_dir / "private" / seat / "notes.md"
    if notes_path.exists():
        notes = notes_path.read_text(encoding="utf-8").strip()
    outbox = game_dir / "outbox" / seat
    if not outbox.is_dir():
        return [], notes
    items = []
    for f in outbox.iterdir():
        m = OUTBOX_RE.match(f.name)
        if m and not f.stem.endswith("-thinking"):
            items.append((int(m.group(1)), m.group(2), f))
    for day, label, f in items:
        try:
            payload = json.loads(f.read_text(encoding="utf-8").strip().removeprefix("```json").removesuffix("```"))
        except (json.JSONDecodeError, OSError):
            continue
        speech = str(payload.get("speech") or "").strip()
        inner = str(payload.get("inner") or "").strip() if with_inner else ""
        if speech or inner:
            entries.append((day, _label_stage(label), label, speech, inner))
    entries.sort(key=lambda e: (_stage_key(e[0], e[1]), e[2]))
    return entries, notes


def _label_stage(label: str) -> str | None:
    head = label.split("-")[0]
    if head in LABEL_STAGE:
        return LABEL_STAGE[head]
    if head in ("自由活动", "晚间休息"):
        return head
    return None  # 毁证机会 / 赠送回应-X：标签不含阶段信息


def _at_filter(day: int, stage: str | None, at: tuple[int, str]) -> bool:
    """--at 冻结：(天, 阶段) 是否 ≤ 冻结点。无法判定阶段的条目仅当冻结在当日末尾（晚间休息）时含入。"""
    if day < at[0]:
        return True
    if day > at[0]:
        return False
    if stage is None:
        return at[1] == "晚间休息"
    return STAGE_ORDER.get(stage, 99) <= STAGE_ORDER.get(at[1], 99)


def _death_cut(state: dict, name: str) -> tuple[int, str] | None:
    """角色的死亡/出局时点（其后的公开事件他不再收到）。存活至终局 → None。"""
    for c in state.get("cases", []):
        if c.get("victim") == name:
            return (c["day"], c["stage"])
    od = state.get("orch", {}).get("out_day", {}).get(name)
    if od:
        return (od, "投票")
    return None


def build_memory(game_dir: Path, state: dict, name: str, seat: str,
                 at: tuple[int, str] | None = None, with_inner: bool = True,
                 full: bool = False) -> tuple[str, tuple[int, str] | None]:
    """组装记忆档案 markdown：第一编（他收到的一切）+ 第二编（他自己的发言与内心全文）。

    返回 (档案, 实际冻结点)。第一编默认在死亡/出局时点截断（此后他不再收到任何信息）；
    --at 显式指定冻结点；--full 不截断（赛后复盘式采访）。第二编只随 --at 截断——
    遗言/死者字条是他自己的话，死亡截断不剔除。"""
    cut = at
    if cut is None and not full:
        cut = _death_cut(state, name)
    orch = state.get("orch", {})
    entries = [(d, st, 0, t) for d, st, t in _parse_timeline(orch.get("feed", []))]
    entries += [(d, st, 1, t) for d, st, t in _parse_timeline(orch.get("hist", {}).get(name, []))]
    entries += [(d, st, 2, t) for d, st, t in _conspiracy_entries(game_dir / "private" / seat / "log.md")]
    if cut:
        entries = [e for e in entries if _at_filter(e[0], e[1], cut)]
    entries.sort(key=lambda e: (_stage_key(e[0], e[1]), e[2]))

    L = [f"# {name} 的记忆档案（对局 {game_dir.name}）", ""]
    if at:
        L += [f"> 记忆截至：第{at[0]}天 · {at[1]}（此刻之后的事你不记得）", ""]
    elif cut and not full:
        L += [f"> 你已于第{cut[0]}天 · {cut[1]}出局；此后的事你不曾经历。", ""]
    L.append("## 第一编 · 你经历的一切（按时间顺序）")
    cur = None
    for day, stage, _src, text in entries:
        if (day, stage) != cur:
            cur = (day, stage)
            L += ["", f"### 第{day}天 · {stage}", ""]
        L.append(text if text.startswith("**") else f"- {text}")

    ob, notes = _outbox_entries(game_dir, seat, with_inner)
    if at:
        ob = [e for e in ob if _at_filter(e[0], e[1], at)]
    L += ["", "## 第二编 · 你的发言与内心全记录"]
    for day, _stage, label, speech, inner in ob:
        L += ["", f"### 第{day}天 · {label}", ""]
        if speech:
            L.append(f"你说：「{speech}」")
        if inner:
            L.append(f"你的内心：{inner}")
    if notes and (at is None):
        L += ["", "## 你的笔记本（最后一版）", "", notes]
    return "\n".join(L) + "\n", cut


def build_system(name: str, personas: dict, rules: str, mid_game: bool) -> str:
    return render("interview_system.md", name=name, profile=describe(name, personas),
                  rules=rules, situation=SITUATION_MID if mid_game else SITUATION_POST)


def run_interview(player, system: str, memory: str, archive: Path,
                  chat_fn=chat, input_fn=input, out=sys.stdout) -> int:
    """多轮采访循环： interviewer（stdin）/ assistant 交替，流式渲染，逐轮落盘。返回采访轮数。"""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": memory + "\n\n（制作人清了清嗓子，采访开始。）"},
    ]
    rounds = 0
    with open(archive, "a", encoding="utf-8") as f:
        while True:
            try:
                q = input_fn("制作人> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q:
                continue
            if q.lower() in ("exit", "quit"):
                break
            messages.append({"role": "user", "content": q})
            f.write(f"\n**制作人**：{q}\n\n")
            print(f"{player.name}> ", end="", flush=True)
            result = chat_fn(player, messages,
                             on_token=lambda text, is_r: (out.write(text), out.flush()) if not is_r else None)
            print()
            if not result.ok:
                print(f"（调用失败：{result.error}；可重问或 exit 结束）")
                messages.pop()
                continue
            messages.append({"role": "assistant", "content": result.content})
            f.write(f"**{player.name}**：{result.content}\n")
            rounds += 1
    return rounds


def main() -> None:
    ap = argparse.ArgumentParser(description="采访录制器：把某席位 AI 拉出古堡进行自由问答采访")
    ap.add_argument("--selected", required=True,
                    help="精选池对局（selected/ 下的 id 或显式路径；采访以精选池为默认目录）")
    ap.add_argument("--seat", required=True, help="席位 id 或角色名")
    ap.add_argument("--at", default=None, metavar="天:阶段",
                    help="局中冻结采访：记忆截至该阶段（含），如 1:晚间休息")
    ap.add_argument("--no-inner", action="store_true", help="记忆档案不含内心独白")
    ap.add_argument("--full", action="store_true",
                    help="赛后复盘式采访：不在死亡/出局时点截断记忆（默认截断——此后的事他没经历过）")
    ap.add_argument("--dump-memory", default=None, metavar="FILE", help="只导出记忆档案到文件，不开始采访")
    ap.add_argument("--memory", default=None, metavar="FILE", help="用指定文件作为记忆档案（人工剪裁后注入）")
    args = ap.parse_args()

    game_dir = resolve_game_dir(args.selected, base=ROOT / "selected")
    if not (game_dir / "state.json").exists():
        raise SystemExit(f"找不到 {game_dir / 'state.json'}（--selected 只接受 selected/ 下的对局 id 或显式路径；"
                         f"先把对局挪入 selected/ 再采访）")
    state = load_state(game_dir / "state.json")
    players, _ = load_config(str(game_dir / "players.toml"))
    player = find_player(players, args.seat)
    if player is None:
        raise SystemExit(f"席位 {args.seat!r} 不在本局阵容中（可选：{', '.join(p.id or p.name for p in players)}）")
    name = player.name
    seat = player.id or name

    at = None
    if args.at:
        m = re.fullmatch(r"(\d+):(.+)", args.at)
        if not m or m.group(2) not in STAGE_ORDER:
            raise SystemExit(f"--at 格式应为 天:阶段（阶段 ∈ {'/'.join(STAGE_ORDER)}），收到 {args.at!r}")
        at = (int(m.group(1)), m.group(2))

    cut = None
    if args.memory:
        memory = Path(args.memory).read_text(encoding="utf-8")
    else:
        memory, cut = build_memory(game_dir, state, name, seat,
                                   at=at, with_inner=not args.no_inner, full=args.full)
    if args.dump_memory:
        Path(args.dump_memory).write_text(memory, encoding="utf-8")
        print(f"记忆档案已导出：{args.dump_memory}（{len(memory)} 字符）")
        if not args.memory:
            return

    personas_path = game_dir / "personas.toml"
    personas = load_personas(personas_path if personas_path.exists() else ROOT / "personas.toml")
    rules = (game_dir / "rules.md").read_text(encoding="utf-8")
    system = build_system(name, personas, rules, mid_game=at is not None)

    out_dir = game_dir / "interview"
    out_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    archive = out_dir / f"{seat}-{ts}.md"
    (out_dir / f"{seat}-{ts}.memory.md").write_text(memory, encoding="utf-8")
    mode = ("局中冻结（截至 第%d天·%s）" % at) if at else (
        "赛后（全程记忆）" if args.full else
        ("赛后（记忆截于出局：第%d天·%s）" % cut) if cut else "赛后（存活至终局）")
    header = (f"# 采访记录 · {name}（{player.id or seat}）\n\n"
              f"- 对局：{game_dir.name}｜模式：{mode}"
              f"｜时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n- 记忆档案：{seat}-{ts}.memory.md\n")
    archive.write_text(header, encoding="utf-8")

    if not player.api_key:
        raise SystemExit(f"环境变量 {player.api_key_env} 未设置（无法调用 {name} 的 API）")
    print(f"采访开始：{name}（{game_dir.name}，{mode}）｜输入问题，exit 结束｜归档：{archive}")
    rounds = run_interview(player, system, memory, archive)
    print(f"采访结束：{rounds} 轮问答，已归档 {archive}")


if __name__ == "__main__":
    main()
