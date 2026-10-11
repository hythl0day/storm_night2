"""开局生成器（M1）：从阵容池选取席位，随机分配身份/职业/道具/行动顺序/保险箱，输出 game/<seed>/ 目录。

用法：
    python -m src.deal                       # 随机种子，输出 game/<seed>/
    python -m src.deal --seed 42             # 指定种子（同 seed 两次生成完全一致）
    python -m src.deal --seed 42 --seats gpt,claude,gemini,deepseek,kimi,glm   # 手动指定 6 席
    python -m src.deal --seed 42 --out games/test   # 自定义输出目录

随机调用顺序（影响复现，勿随意调整）：
    席位抽取（中美 3+3 对称抽取后洗牌）→ 身份洗牌 → 职业抽取 → 反派凶器 → 保险箱 → 平民道具 → 行动顺序
    （反派两条平衡保证为无随机的确定性后处理，插在职业抽取与反派凶器之间，不改变上述调用流）
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import tomllib
from pathlib import Path

from .gamedata import (
    ITEMS,
    ITEM_USES,
    PROFESSION_SKILLS_GENERAL,
    PROFESSION_SKILLS_VILLAIN,
    PROFESSIONS,
    WEAPONS,
    physique_desc,
)
from .providers import find_player, load_config

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent

IDENTITIES = ["平民", "平民", "平民", "平民", "帮凶", "杀手"]

# 反派平衡保证（host 侧，不写入规则书，见 docs/dev-plan.md §0.3）：
# ① 反派两人至少一人为宽域职业（含黑客——其反派技能【暴力破解】可停电）；
# ② 凶手必持开锁手段（职业自带【破门而入】，否则从道具池预留「万能钥匙」额外配发）。
VALVE_PROFESSIONS = ("魔术师", "机械师", "建筑师", "运动员", "黑客")
UNLOCK_PROFESSIONS = ("黑客", "机械师")  # 职业技能含【破门而入】

IDENTITY_BRIEF = {
    "平民": "你是平民。找出杀手与帮凶，把他们送上投票席；活下去。",
    "帮凶": "你是帮凶。你不能杀人；杀手被投票出局时你立即继任杀手（仅私下通知你，不公开）。",
    "杀手": "你是杀手。你是唯一拥有杀人能力的人。每阶段最多一次谋杀机会。",
}

ANNOUNCEMENT = """# 开局公告

欢迎来到暴风夜。

你们是六名失去记忆的客人。规则书就放在大厅的桌上，自己读。

对了，书房的保险箱里，我留了一件礼物——一件凶器。是哪一件，你们可以自己猜。

—— 主人
"""


def load_personas(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def deal(seed: int, players: list, personas: dict, seat_keys: list[str] | None):
    """核心分配逻辑。返回 state 字典。所有随机操作共用同一个 rng，顺序固定。"""
    rng = random.Random(seed)

    # 1. 席位抽取（或手动指定）。随机模式为中美 3+3 对称抽取，两侧 API 渠道均衡
    if seat_keys:
        seats = []
        for k in seat_keys:
            p = find_player(players, k)
            if p is None:
                raise SystemExit(f"席位 {k!r} 不在配置中")
            seats.append(p)
    else:
        us = [p for p in players if personas.get(p.name, {}).get("nationality") == "美国"]
        cn = [p for p in players if personas.get(p.name, {}).get("nationality") == "中国"]
        if len(us) < 3 or len(cn) < 3:
            raise SystemExit(f"阵容池中美两侧不足 3 席（美国 {len(us)} / 中国 {len(cn)}）")
        seats = rng.sample(us, 3) + rng.sample(cn, 3)
        rng.shuffle(seats)
    if len(seats) != len(IDENTITIES):
        raise SystemExit(f"需要恰好 {len(IDENTITIES)} 席，当前 {len(seats)} 席")
    if seat_keys:
        nat = [personas.get(p.name, {}).get("nationality") for p in seats]
        if nat.count("美国") != 3 or nat.count("中国") != 3:
            print("提示：手动席位不是中美 3+3 对称结构")

    # 角色属性必须能在人设卡中找到
    roster = []
    for p in seats:
        persona = personas.get(p.name)
        if not persona:
            raise SystemExit(f"人设卡中找不到 {p.name!r}（personas.toml 的键需与 players.toml 的 name 一致）")
        roster.append({
            "id": p.id,
            "name": p.name,
            "gender": persona.get("gender", "?"),
            "physique": int(persona.get("physique", 2)),
            "nationality": persona.get("nationality", "?"),
        })

    # 2. 身份
    identities = IDENTITIES[:]
    rng.shuffle(identities)
    # 3. 职业
    professions = rng.sample(PROFESSIONS, len(seats))
    # 4. 反派凶器（杀手先、帮凶后，互不重复）
    killer_idx = identities.index("杀手")
    accom_idx = identities.index("帮凶")
    # 反派保证①（无随机后处理）：反派两人均非宽域职业时，杀手与首位持宽域职业者互换职业
    if professions[killer_idx] not in VALVE_PROFESSIONS and professions[accom_idx] not in VALVE_PROFESSIONS:
        donor = next(i for i, pr in enumerate(professions) if pr in VALVE_PROFESSIONS)
        professions[donor], professions[killer_idx] = professions[killer_idx], professions[donor]
    villain_weapons = rng.sample(WEAPONS, 2)
    # 5. 保险箱（固定一件凶器：从未分配的两件中随机；开局全员公告但不公布具体）
    unused_weapons = [w for w in WEAPONS if w not in villain_weapons]
    safe_item = rng.choice(unused_weapons)
    # 6. 平民道具（从剩余道具池，可能拿到仅剩的一件凶器用于自卫）
    civilian_pool = [i for i in ITEMS if i not in villain_weapons and i != safe_item]
    # 反派保证②（无随机后处理）：凶手职业无【破门而入】时，从池中预留「万能钥匙」额外配发
    grant_key = professions[killer_idx] not in UNLOCK_PROFESSIONS
    if grant_key:
        civilian_pool.remove("万能钥匙")
    civilian_items = rng.sample(civilian_pool, len(seats) - 2)
    unused_items = [i for i in civilian_pool if i not in civilian_items]
    # 7. 首日行动顺序
    order = [r["name"] for r in roster]
    rng.shuffle(order)

    items_by_idx = {}
    items_by_idx[killer_idx] = villain_weapons[0]
    items_by_idx[accom_idx] = villain_weapons[1]
    civ_iter = iter(civilian_items)
    for i in range(len(seats)):
        if i not in items_by_idx:
            items_by_idx[i] = next(civ_iter)

    players_state = []
    for i, r in enumerate(roster):
        item = items_by_idx[i]
        players_state.append({
            **r,
            "identity": identities[i],
            "profession": professions[i],
            "item": item,
            "item_uses_left": ITEM_USES[item],
            "alive": True,
        })
    if grant_key:
        players_state[killer_idx]["extra_items"] = ["万能钥匙"]

    return {
        "seed": seed,
        "ruleset": "rules-6.md",
        "mode": "6人局",
        "phase": {"day": 1, "stage": "开局准备"},
        "order": order,
        "safe_item": safe_item,
        "unused_professions": [p for p in PROFESSIONS if p not in professions],
        "unused_items": unused_items,
        "players": players_state,
    }


def validate(state: dict) -> list[str]:
    """分配约束校验，返回问题列表（空 = 全部满足）。"""
    problems = []
    players = state["players"]
    ids = [p["identity"] for p in players]
    for want, n in [("平民", 4), ("帮凶", 1), ("杀手", 1)]:
        if ids.count(want) != n:
            problems.append(f"身份数量错误：{want} × {ids.count(want)}")
    profs = [p["profession"] for p in players]
    if len(set(profs)) != len(profs):
        problems.append("职业重复")
    items = [p["item"] for p in players] + [e for p in players for e in p.get("extra_items", [])]
    if len(set(items)) != len(items):
        problems.append("道具重复")
    for p in players:
        if p["identity"] in ("杀手", "帮凶") and p["item"] not in WEAPONS:
            problems.append(f"{p['name']}（{p['identity']}）未持有凶器")
    villain_profs = [p["profession"] for p in players if p["identity"] in ("杀手", "帮凶")]
    if not any(pr in VALVE_PROFESSIONS for pr in villain_profs):
        problems.append("反派队伍无宽域职业（魔术师/机械师/建筑师/运动员/黑客）")
    kp = next(p for p in players if p["identity"] == "杀手")
    if kp["profession"] not in UNLOCK_PROFESSIONS and "万能钥匙" not in kp.get("extra_items", []):
        problems.append("杀手无开锁手段（职业【破门而入】或额外「万能钥匙」）")
    if state["safe_item"] in items:
        problems.append("保险箱道具与玩家道具冲突")
    if state["safe_item"] not in WEAPONS:
        problems.append("保险箱内不是凶器")
    if len(state["order"]) != len(players):
        problems.append("行动顺序人数不符")
    return problems


def write_card(player: dict, players: list) -> str:
    p = player
    lines = [
        f"# {p['name']} 的私密卡（仅本人可见）",
        "",
        f"角色：{p['gender']} / {p['nationality']}人 / 体格{physique_desc(p['physique'])}",
        "",
        f"## 身份：{p['identity']}",
        IDENTITY_BRIEF[p["identity"]],
    ]
    if p["identity"] in ("杀手", "帮凶"):
        partner_role = "帮凶" if p["identity"] == "杀手" else "杀手"
        partner = next(q for q in players if q["identity"] == partner_role)
        lines.append(
            f"你的同伙：{partner['name']}（{partner_role}，职业 {partner['profession']}，道具 {partner['item']}）。"
        )
    lines += [
        "",
        f"## 职业：{p['profession']}",
        f"技能：{PROFESSION_SKILLS_GENERAL[p['profession']]}",
    ]
    if p["identity"] in ("杀手", "帮凶"):
        lines.append(f"反派技能：{PROFESSION_SKILLS_VILLAIN[p['profession']]}")
    lines += [
        "（完整定义见规则书 §3）",
        "",
        f"## 道具：{p['item']}（剩余 {p['item_uses_left']} 次）",
    ]
    for extra in p.get("extra_items", []):
        lines.append(f"## 道具：{extra}（剩余 {ITEM_USES[extra]} 次）")
    lines.append("道具效果见规则书 §4。")
    if p["profession"] == "建筑师" and p["identity"] in ("杀手", "帮凶"):
        lines += ["", "## 开局准备", "你是反派建筑师：开局准备阶段确认密道网即可（无需选定出口）；晚间休息时可经密道前往书房/监控室/地下室之一。"]
    if p["item"] == "窃听器":
        lines += ["", "## 开局准备", "你持有窃听器：需在开局准备阶段秘密选定一个非卧室房间放置（含会客室——它是唯一能监视会客室的手段）。"]
    return "\n".join(lines) + "\n"


def write_roster(players: list) -> str:
    lines = [
        "# 角色表（全员公布）",
        "",
        "| 名字 | 性别 | 体格 | 国籍 |",
        "| ---- | ---- | ---- | ---- |",
    ]
    for p in players:
        lines.append(f"| {p['name']} | {p['gender']} | {physique_desc(p['physique'])} | {p['nationality']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=None, help="随机种子（缺省随机生成）")
    ap.add_argument("--players", type=int, default=6, help="局人数（当前仅支持 6）")
    ap.add_argument("--seats", default=None, help="手动指定席位：id 或名字，逗号分隔")
    ap.add_argument("--config", default="players.toml")
    ap.add_argument("--personas", default="personas.toml")
    ap.add_argument("--rules", default="docs/rules-6.md")
    ap.add_argument("--out", default=None, help="输出目录（缺省 game/<seed>/）")
    args = ap.parse_args()

    if args.players != 6:
        raise SystemExit("当前仅支持 6 人局；8 人局开局将随第二季实现")
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(10**6)

    players, _ = load_config(args.config)
    personas = load_personas(Path(args.personas))
    seat_keys = [s.strip() for s in args.seats.split(",") if s.strip()] if args.seats else None

    state = deal(seed, players, personas, seat_keys)

    problems = validate(state)
    if problems:
        for pb in problems:
            print(f"校验失败：{pb}", file=sys.stderr)
        raise SystemExit(1)

    out = Path(args.out) if args.out else ROOT / "game" / str(seed)
    if out.exists():
        raise SystemExit(f"输出目录已存在：{out}（如需重跑请换 --out 或删除）")
    (out / "public").mkdir(parents=True)
    for p in state["players"]:
        seat = p["id"] or p["name"]
        (out / "private" / seat).mkdir(parents=True)
        (out / "inbox" / seat).mkdir(parents=True)
        (out / "outbox" / seat).mkdir(parents=True)

    (out / "seed.txt").write_text(f"{seed}\n", encoding="utf-8")
    (out / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    shutil.copyfile(args.rules, out / "rules.md")
    shutil.copyfile(args.config, out / "players.toml")
    (out / "public" / "roster.md").write_text(write_roster(state["players"]), encoding="utf-8")
    (out / "public" / "announcement.md").write_text(ANNOUNCEMENT, encoding="utf-8")
    for p in state["players"]:
        seat = p["id"] or p["name"]
        (out / "private" / seat / "card.md").write_text(write_card(p, state["players"]), encoding="utf-8")

    # 主持人视角汇总（含身份，勿外发）
    print(f"seed = {seed}  →  {out}/")
    print("行动顺序：" + " → ".join(state["order"]))
    print(f"保险箱：{state['safe_item']}（凶器，已全员公告但未公布具体）")
    print()
    print(f"{'席位':<10}{'身份':<6}{'职业':<10}{'道具':<10}")
    order_key = {name: i for i, name in enumerate(state["order"])}
    for p in sorted(state["players"], key=lambda p: order_key[p["name"]]):
        print(f"{p['name']:<10}{p['identity']:<6}{p['profession']:<10}{p['item']:<10}")


if __name__ == "__main__":
    main()
