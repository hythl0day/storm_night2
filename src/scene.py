"""补拍演出器：选定存档与冻结时点，让角色"重聚"补演一场戏（台词由 API 真实生成）。

与采访录制器（interview.py）同族：记忆用 interview.build_memory 从存档逐字节复原并冻结在
指定阶段；区别在于补拍是**多角色多轮对话**——同场角色按行动顺序依次开口，后发言者能看到
本场此前已说的台词。补拍不进游戏流程、不改存档状态：产出纯属宿主侧剪辑素材。

纪律（写进 prompt/scene_system.md）：台词只展现人设与场景氛围，不涉及规则交流与推理——
信息闸门是冻结时点（--at），不是事后的自觉。

用法：
    python -m src.scene --selected 976488 --scene 开场惊魂                 # 预设场景（自带冻结点）
    python -m src.scene --selected 976488 --scene 轮回开场                 # 既视感版：模糊轮回记忆+册子改称主神
    python -m src.scene --selected 976488 --scene 开场惊魂 --rounds 2      # 两轮对话
    python -m src.scene --selected 976488 --desc "清晨，众人在会客室看到……" --at 2:案件调查
    python -m src.scene --selected 976488 --scene 终局谢幕 --seats claude,deepseek

--selected 只接受 selected/ 下的对局 id 或显式路径（与采访一致：先入选，再补拍）。
归档：<对局>/scene/<场景>-<时间戳>.md（对话全记录）+ 同目录 .memory-<席位>.md（各席当次记忆档案）。
文件头注明"补拍素材"——它是赛后生成，不属于原始跑局时间线。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from . import ROOT, resolve_game_dir
from .engine.state import load_state
from .interview import STAGE_ORDER, _death_cut, _stage_key, build_memory
from .prompts import render
from .providers import chat, find_player, load_config
from .smoke import describe, load_personas

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# 预设场景：default_at=None 表示赛后（记忆截于各自出局时点）
SCENES = {
    "开场惊魂": {
        "at": (1, "开局准备"),
        "desc": ("暴风雨之夜。你们在一座陌生古堡的石厅中醒来——大门与窗户全部紧闭，怎么推都纹丝不动；"
                 "窗外雷电交加，暴雨倾盆。没有人记得自己是谁、从何而来。长桌上放着一本陌生的册子，"
                 "和一张主人留下的字条。现在，惊魂未定的你们围坐在会客室的壁炉旁，试着弄清楚处境。"),
    },
    "终局谢幕": {
        "at": None,
        "desc": ("一切结束了。暴风雨停了，古堡沉重的大门终于缓缓打开，清晨的光照进大厅。"
                 "幸存者们站在门口，回头望向这座困住你们数日的古堡——每人说一句此刻最想说的话。"),
    },
    "轮回开场": {
        "at": (1, "开局准备"),
        "desc": ("暴风雨之夜。你们在一座陌生古堡的石厅中醒来——大门与窗户全部紧闭，窗外雷电交加。"
                 "但这一次，有什么不一样了：你模模糊糊地觉得，这一幕你经历过无数次。围坐在壁炉旁的这些人，"
                 "每一张脸都熟悉得让你心里发毛；脑海深处还浮着碎片般的恐怖回忆——你记得杀过人，也记得被杀。"
                 "细节全都抓不住，只有情绪是真的。"
                 "然后你注意到：长桌上那本册子的措辞变了——它不再自称来自「主持人」，而是「主神」。"
                 "其他人显然也注意到了。"),
        "note": ("本场你可以谈论两件事：对这一切的「似曾相识」，以及册子措辞的变化与「主神」这个称谓——"
                 "但都只限于情绪、直觉与恐惧层面的反应，不要逐条分析规则内容。"
                 "你所谓的「回忆」只有模糊的情绪与碎片：不要编造具体的往局细节（谁杀了谁、谁是什么身份、"
                 "具体哪天发生了什么）——你说不出细节，只有熟悉感与恐惧。"),
    },
}


def participants(state: dict, at: tuple[int, str] | None) -> list[str]:
    """默认参演名单：冻结时点仍在场者（按行动顺序）。--at 之后死亡/出局的角色不在场。"""
    out = []
    for n in state["order"]:
        p = next((q for q in state["players"] if q["name"] == n), None)
        if p is None:
            continue
        if at is None:
            if p["alive"]:
                out.append(n)
        else:
            cut = _death_cut(state, n)
            if cut is None or _stage_key(*cut) > _stage_key(*at):
                out.append(n)
    return out


def run_scene(casts: list[dict], rounds: int, archive, chat_fn=chat, out=sys.stdout) -> int:
    """多轮对话循环。casts[i] = {player, system, memory}。返回落盘的台词数。

    每席每次调用都是无状态重建：system（人设+场景+纪律）+ memory + 本场已说台词 + 轮到你了。
    调用失败跳过该席本轮（记"（补拍失败）"），不中断整段场景。"""
    lines: list[str] = []
    written = 0
    with open(archive, "a", encoding="utf-8") as f:
        for r in range(1, rounds + 1):
            if rounds > 1:
                f.write(f"\n## 第{r}轮\n")
                print(f"—— 第{r}轮 ——")
            for cast in casts:
                player = cast["player"]
                so_far = "\n\n".join(lines) if lines else "（还没有人开口——你是第一个。）"
                user = (cast["memory"]
                        + f"\n\n——— 补拍现场 ———\n\n本场已经发生的台词：\n{so_far}\n\n"
                        + f"轮到你（{player.name}）了：只说你此刻会说出口的话。")
                messages = [{"role": "system", "content": cast["system"]},
                            {"role": "user", "content": user}]
                print(f"{player.name}> ", end="", flush=True)
                result = chat_fn(player, messages,
                                 on_token=lambda t, is_r: (out.write(t), out.flush()) if not is_r else None)
                print()
                if not result.ok:
                    print(f"（{player.name} 补拍失败：{result.error}；跳过该席本轮）")
                    f.write(f"\n**{player.name}**：（补拍失败：{result.error}）\n")
                    continue
                line = result.content.strip()
                lines.append(f"**{player.name}**：{line}")
                f.write(f"\n**{player.name}**：{line}\n")
                written += 1
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="补拍演出器：让存档中的角色重聚补演一场戏（台词真实生成）")
    ap.add_argument("--selected", required=True,
                    help="精选池对局（selected/ 下的 id 或显式路径；先入选，再补拍）")
    ap.add_argument("--scene", default=None, help=f"预设场景：{'、'.join(SCENES)}")
    ap.add_argument("--desc", default=None, help="自由场景描述（与 --scene 二选一；配 --at 指定冻结点）")
    ap.add_argument("--at", default=None, metavar="天:阶段",
                    help="记忆冻结时点（如 1:开局准备、2:案件调查）；预设场景自带默认值，自由描述缺省为 1:开局准备")
    ap.add_argument("--seats", default=None, help="参演席位（逗号分隔，id 或角色名）；缺省为冻结时点仍在场者")
    ap.add_argument("--rounds", type=int, default=1, help="对话轮数（默认 1；每轮每人一句）")
    args = ap.parse_args()

    if bool(args.scene) == bool(args.desc):
        raise SystemExit("(--scene 预设场景）与（--desc 自由描述）二选一，且必须给一个")
    if args.scene:
        if args.scene not in SCENES:
            raise SystemExit(f"未知预设场景 {args.scene!r}（可选：{'、'.join(SCENES)}）")
        preset = SCENES[args.scene]
        desc, default_at, label = preset["desc"], preset["at"], args.scene
        if preset.get("note"):  # 场景级的纪律豁免/边界说明（如轮回开场允许谈论册子变化与「主神」称谓）
            desc += "\n\n本场特别提示：" + preset["note"]
    else:
        desc, default_at, label = args.desc.strip(), (1, "开局准备"), "自由场景"

    at = None
    if args.at:
        m = re.fullmatch(r"(\d+):(.+)", args.at)
        if not m or m.group(2) not in STAGE_ORDER:
            raise SystemExit(f"--at 格式应为 天:阶段（阶段 ∈ {'/'.join(STAGE_ORDER)}），收到 {args.at!r}")
        at = (int(m.group(1)), m.group(2))
    elif default_at is not None:
        at = default_at

    game_dir = resolve_game_dir(args.selected, base=ROOT / "selected")
    if not (game_dir / "state.json").exists():
        raise SystemExit(f"找不到 {game_dir / 'state.json'}（--selected 只接受 selected/ 下的对局 id 或显式路径；"
                         f"先把对局挪入 selected/ 再补拍）")
    state = load_state(game_dir / "state.json")
    players, _ = load_config(str(game_dir / "players.toml"))

    if args.seats:
        wanted = [s.strip() for s in args.seats.split(",") if s.strip()]
        casts_players = []
        for w in wanted:
            p = find_player(players, w)
            if p is None:
                raise SystemExit(f"席位 {w!r} 不在本局阵容中（可选：{', '.join(p.id or p.name for p in players)}）")
            casts_players.append(p)
    else:
        on_stage = participants(state, at)
        casts_players = [p for n in on_stage for p in players if p.name == n]
        if not casts_players:
            raise SystemExit("冻结时点没有在场角色（检查 --at）")

    personas_path = game_dir / "personas.toml"
    personas = load_personas(personas_path if personas_path.exists() else ROOT / "personas.toml")

    out_dir = game_dir / "scene"
    out_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_label = re.sub(r"[^\w一-鿿-]+", "_", label)
    archive = out_dir / f"{safe_label}-{ts}.md"

    freeze_txt = ("第%d天·%s" % at) if at else "赛后（记忆截于各自出局时点）"
    casts = []
    for p in casts_players:
        name, seat = p.name, p.id or p.name
        memory, _ = build_memory(game_dir, state, name, seat, at=at, with_inner=True,
                                 full=at is None)
        (out_dir / f"{safe_label}-{ts}.memory-{seat}.md").write_text(memory, encoding="utf-8")
        system = render("scene_system.md", name=name, profile=describe(name, personas), scene=desc)
        casts.append({"player": p, "system": system, "memory": memory})

    header = (f"# 补拍素材 · {label}（对局 {game_dir.name}）\n\n"
              f"> 本场台词为赛后补拍：记忆冻结于 {freeze_txt}，由 API 真实生成，"
              f"不属于原始跑局时间线。参演：{'、'.join(p.name for p in casts_players)}"
              f"｜轮数：{args.rounds}｜生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
              f"## 场景设定\n\n{desc}\n\n## 台词\n")
    archive.write_text(header, encoding="utf-8")

    missing = [p.name for p in casts_players if not p.api_key]
    if missing:
        raise SystemExit(f"环境变量未设置，无法调用：{'、'.join(missing)}")
    print(f"补拍开始：{label}（{game_dir.name}，冻结于 {freeze_txt}）"
          f"｜参演 {'、'.join(p.name for p in casts_players)}｜归档：{archive}")
    written = run_scene(casts, args.rounds, archive)
    print(f"补拍结束：{written} 句台词，已归档 {archive}")


if __name__ == "__main__":
    main()
