"""开局生成器（M1）：从阵容池选取席位，随机分配身份/职业/道具/行动顺序/保险箱/桌上凶器，输出 game/<seed>/ 目录。

用法：
    python -m src.deal                       # 随机种子，输出 game/<seed>/
    python -m src.deal --seed 42             # 指定种子（同 seed 两次生成完全一致）
    python -m src.deal --seed 42 --seats gpt,claude,gemini,deepseek,kimi,glm   # 手动指定 6 席
    python -m src.deal --seed 42 --exclude grok,doubao  # 随机抽席时屏蔽席位（id 或名字，逗号分隔）
    python -m src.deal --seed 42 --out games/test   # 自定义输出目录
    python -m src.deal --seed 42 --lite             # 失忆版精简规则集（rules-6-lite.md）：AI 只知生存须知，
                                                    # 职业/道具全表与分配结构不下发，机制靠菜单临场发现；
                                                    # 裁决规则不变（仍 v3.25），state 记 is_lite_rule=True

随机调用顺序（影响复现，勿随意调整；v3.5 起保底凶器车道改为桌上凶器，不再选持有人）：
    席位抽取（中美 3+3 对称抽取后洗牌）→ 身份洗牌 → 职业抽取 → 反派凶器（3 选 2，左轮手枪固定上桌）
    → 帮凶反派道具 → 保险箱抽池 → 螺丝起子持有人 →（保险箱抽中反派道具时）第四平民凶器 → 平民道具池 → 行动顺序
    （反派功能覆盖保证为无随机的确定性后处理，插在职业抽取与反派凶器之间；万能钥匙预留无随机消耗）
    --exclude 只在席位抽取前从阵容池剔除屏蔽席位，rng 调用次数与顺序不变——同 seed + 同屏蔽名单可复现。
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
    ITEM_DESC,
    ITEMS,
    ITEM_USES,
    ITEMS_GENERAL,
    PROFESSION_SKILLS_GENERAL,
    PROFESSION_SKILLS_VILLAIN,
    PROFESSIONS,
    SKILL_DESC_GENERAL,
    SKILL_DESC_VILLAIN,
    TABLE_WEAPON,
    VILLAIN_ITEMS,
    VILLAIN_WEAPON_POOL,
    WEAPONS,
    physique_desc,
)
from .providers import find_player, load_config

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent

IDENTITIES = ["平民", "平民", "平民", "平民", "帮凶", "杀手"]

# 反派平衡保证（见 docs/v3.2-plan.md §2.9）：
# ① 反派队至少一人持功能性主动技能（舞台魔术/延时装置/记忆植入/信号干扰——破抱团或破票仓；
#    host 侧保密，不写入规则书）；② 杀手固定持有「万能钥匙」（公开规则）。
# 停电源已由万能钥匙保底（v3.2 拉电闸），不再依赖宽域职业。
VALVE_PROFESSIONS = ("魔术师", "机械师", "心理医生", "黑客")

IDENTITY_BRIEF = {
    "平民": "你是平民。找出杀手与帮凶，把他们送上投票席；活下去。",
    "帮凶": "你是帮凶。你不能主动谋杀，但反派职业技能（如【延时装置】【舞台魔术】）不受此限，可正常发动；"
            "杀手被投票出局时你立即继任杀手（仅私下通知你，不公开）。"
            "若你持有任一【暴力破解】手段（职业技能/「万能钥匙」/「螺丝起子」），可在自由活动开始前"
            "消耗一次秘密拉电闸停电：门锁失效、监控缺失、除会客室外全部黑暗（规则书 §7.5）。",
    "杀手": "你是杀手。你是唯一拥有主动谋杀权的人：每阶段最多一次谋杀机会。帮凶不能主动谋杀，但其反派职业技能除外。"
            "你固定持有的「万能钥匙」除开门外，还可在自由活动开始前消耗一次【暴力破解】秘密拉电闸停电"
            "（门锁失效、监控缺失、除会客室外全部黑暗）——停电是抹掉监控记录、免开锁进卧室、撕开守夜的核心手段（规则书 §7.5）。",
}


def announcement(safe_item: str) -> str:
    """开局公告（主人字条）：保险箱内容公开（v3.2 §2.5）+ 桌上凶器公开（v3.5 §2）。"""
    return f"""# 开局公告

欢迎来到暴风夜。

你们是六名失去记忆的客人。规则书就放在大厅的桌上，自己读。

对了，书房保险箱里，我留了一件礼物：「{safe_item}」。谁先打开，就是谁的。

大厅的桌上还有我年轻时的收藏：「{TABLE_WEAPON}」。光天化日之下别去碰它——大家都看着。夜里嘛……看你们的本事。

—— 主人
"""


def load_personas(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def deal(seed: int, players: list, personas: dict, seat_keys: list[str] | None,
         lite: bool = False, exclude: set[str] | None = None):
    """核心分配逻辑。返回 state 字典。所有随机操作共用同一个 rng，顺序固定。
    lite 只改信息口径字段（ruleset/is_lite_rule），不消耗随机序列——同 seed 的分配逐字节一致。
    exclude 为屏蔽席位 id 集：随机抽席前从阵容池剔除（rng 调用序列不变，同 seed + 同名单可复现）；
    与手动 seat_keys 重叠时报错。"""
    rng = random.Random(seed)
    exclude = exclude or set()

    # 1. 席位抽取（或手动指定）。随机模式为中美 3+3 对称抽取，两侧 API 渠道均衡
    if seat_keys:
        seats = []
        for k in seat_keys:
            p = find_player(players, k)
            if p is None:
                raise SystemExit(f"席位 {k!r} 不在配置中")
            seats.append(p)
        clash = [p.id for p in seats if p.id in exclude]
        if clash:
            raise SystemExit(f"手动席位与屏蔽名单冲突：{', '.join(clash)}")
    else:
        pool = [p for p in players if p.id not in exclude]
        us = [p for p in pool if personas.get(p.name, {}).get("nationality") == "美国"]
        cn = [p for p in pool if personas.get(p.name, {}).get("nationality") == "中国"]
        if len(us) < 3 or len(cn) < 3:
            suffix = f"（已屏蔽 {len(exclude)} 席）" if exclude else ""
            raise SystemExit(f"阵容池中美两侧不足 3 席（美国 {len(us)} / 中国 {len(cn)}）{suffix}")
        seats = rng.sample(us, 3) + rng.sample(cn, 3)
        rng.shuffle(seats)
    if len(seats) != len(IDENTITIES):
        raise SystemExit(f"需要恰好 {len(IDENTITIES)} 席，当前有 {len(seats)} 席")
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
    # 4. 反派凶器（杀手先、帮凶后，互不重复；v3.5：左轮手枪固定在桌上，反派池为 刀/棒球棍/不明药剂 3 选 2）
    killer_idx = identities.index("杀手")
    accom_idx = identities.index("帮凶")
    # 反派保证（无随机后处理）：反派队均无功能性主动技能时，杀手与首位持 valve 职业者互换职业
    if professions[killer_idx] not in VALVE_PROFESSIONS and professions[accom_idx] not in VALVE_PROFESSIONS:
        donor = next(i for i, pr in enumerate(professions) if pr in VALVE_PROFESSIONS)
        professions[donor], professions[killer_idx] = professions[killer_idx], professions[donor]
    villain_weapons = rng.sample(VILLAIN_WEAPON_POOL, 2)
    accom_gear = rng.choice(VILLAIN_ITEMS)  # 帮凶固定配发的反派道具（v3.2：裹尸袋/可疑的粉末/病毒U盘 3 选 1）
    # 5. 保险箱抽池：剩余凶器 + 帮凶未抽到的反派道具 抽 1；内容随开局公告公开
    leftover_weapon = next(w for w in VILLAIN_WEAPON_POOL if w not in villain_weapons)
    safe_pool = [leftover_weapon] + [i for i in VILLAIN_ITEMS if i != accom_gear]
    safe_item = rng.choice(safe_pool)
    # 6. 平民车道（v3.5）：螺丝起子 1 人 + 通用池 5 抽 3（保底凶器改为桌上凶器，不再配发给平民）
    civ_idxs = [i for i, idn in enumerate(identities) if idn == "平民"]
    screw_civ = rng.choice(civ_idxs)
    pool_civs = [i for i in civ_idxs if i != screw_civ]
    civilian_pool = list(ITEMS_GENERAL)
    fourth_civ = None
    if safe_item not in WEAPONS:  # 保险箱抽中反派道具 → 剩余凶器直接发给剩余平民之一（四凶器严格必现）
        fourth_civ = rng.choice(pool_civs)
    civilian_items = rng.sample(civilian_pool, len(pool_civs) - (1 if fourth_civ is not None else 0))
    unused_items = ([i for i in civilian_pool if i not in civilian_items]
                    + [i for i in VILLAIN_ITEMS if i not in (accom_gear, safe_item)])
    # 7. 首日行动顺序
    order = [r["name"] for r in roster]
    rng.shuffle(order)

    items_by_idx = {killer_idx: villain_weapons[0], accom_idx: villain_weapons[1],
                    screw_civ: "螺丝起子"}
    if fourth_civ is not None:
        items_by_idx[fourth_civ] = leftover_weapon
    for i, it in zip([i for i in pool_civs if i != fourth_civ], civilian_items):
        items_by_idx[i] = it

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
    players_state[killer_idx]["extra_items"] = ["万能钥匙"]
    players_state[accom_idx]["extra_items"] = [accom_gear]

    return {
        "seed": seed,
        "ruleset": "rules-6-lite.md" if lite else "rules-6.md",
        "rules_version": "v3.25",  # 规则版本（统计脚本按版本分组，跨版本不混算平衡数据）
        "is_lite_rule": lite,  # 失忆版精简规则集标记；旧档缺此字段视为 False（高级规则）
        "excluded_seats": sorted(exclude),  # 本局屏蔽的席位 id（旧档缺此字段 = 无屏蔽）
        "mode": "6人局",
        "phase": {"day": 1, "stage": "开局准备"},
        "order": order,
        "safe_item": safe_item,
        "table_items": [{"name": TABLE_WEAPON, "uses_left": ITEM_USES[TABLE_WEAPON]}],  # v3.5 桌上凶器
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
        if p["identity"] in ("杀手", "帮凶") and p["item"] == TABLE_WEAPON:
            problems.append(f"{p['name']}（{p['identity']}）持有桌上凶器「{TABLE_WEAPON}」（v3.5 不进反派车道）")
    villain_profs = [p["profession"] for p in players if p["identity"] in ("杀手", "帮凶")]
    if not any(pr in VALVE_PROFESSIONS for pr in villain_profs):
        problems.append("反派队无功能性主动技能（舞台魔术/延时装置/记忆植入/信号干扰）")
    kp = next(p for p in players if p["identity"] == "杀手")
    if "万能钥匙" not in kp.get("extra_items", []):
        problems.append("杀手未固定持有「万能钥匙」")
    if any(p["item"] == "万能钥匙" or "万能钥匙" in p.get("extra_items", [])
           for p in players if p["identity"] != "杀手"):
        problems.append("「万能钥匙」只能由杀手持有")
    ap = next(p for p in players if p["identity"] == "帮凶")
    if len(ap.get("extra_items", [])) != 1 or ap["extra_items"][0] not in VILLAIN_ITEMS:
        problems.append("帮凶未固定持有 1 件反派道具")
    for p in players:
        if p["identity"] == "平民" and (p["item"] == "万能钥匙" or p["item"] in VILLAIN_ITEMS):
            problems.append(f"{p['name']}（平民）持有禁入平民车道的道具：{p['item']}")
    # v3.5 平民车道：恰好一名平民持螺丝起子（保底凶器改为桌上凶器，不再配发平民）
    screw = [p for p in players if p["item"] == "螺丝起子"]
    if len(screw) != 1 or screw[0]["identity"] != "平民":
        problems.append("螺丝起子车道错误：应恰好一名平民开局持有")
    if state.get("table_items") != [{"name": TABLE_WEAPON, "uses_left": ITEM_USES[TABLE_WEAPON]}]:
        problems.append(f"桌上凶器缺失：会客室桌上应固定放有「{TABLE_WEAPON}」")
    if state["safe_item"] in items:
        problems.append("保险箱道具与玩家道具冲突")
    if state["safe_item"] not in WEAPONS and state["safe_item"] not in VILLAIN_ITEMS:
        problems.append("保险箱道具非法（应为剩余凶器或未分配的反派道具）")
    # 四凶器必现（v3.5）：反派 2 件 + 桌上 1 件 + 保险箱或第四平民 1 件
    all_weapons = [p["item"] for p in players if p["item"] in WEAPONS]
    if state["safe_item"] in WEAPONS:
        all_weapons.append(state["safe_item"])
    all_weapons.append(TABLE_WEAPON)
    if sorted(all_weapons) != sorted(WEAPONS):
        problems.append("四凶器未全部入场（杀手/帮凶/桌上凶器/保险箱或第四平民）")
    if len(state["order"]) != len(players):
        problems.append("行动顺序人数不符")
    return problems


def write_card(player: dict, players: list, lite: bool = False) -> str:
    """私密卡。lite 模式（rules-6-lite.md 无职业/道具词条）：技能与道具内联一句话定义、
    不引用规则书章节；身份简报中的规则书引用同步剥离。"""
    p = player
    brief = IDENTITY_BRIEF[p["identity"]]
    if lite:
        brief = brief.replace("（规则书 §7.5）", "")
    lines = [
        f"# {p['name']} 的私密卡（仅本人可见）",
        "",
        f"角色：{p['gender']} / {p['nationality']}人 / 体格{physique_desc(p['physique'])}",
        "",
        f"## 身份：{p['identity']}",
        brief,
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
    if lite:
        lines[-1] += f"——{SKILL_DESC_GENERAL[p['profession']]}"
    if p["identity"] in ("杀手", "帮凶"):
        villain_skill = f"反派技能：{PROFESSION_SKILLS_VILLAIN[p['profession']]}"
        if lite:
            villain_skill += f"——{SKILL_DESC_VILLAIN[p['profession']]}"
        lines.append(villain_skill)
    if not lite:
        lines += ["（完整定义见规则书 §3）"]
    lines += [
        "",
        f"## 道具：{p['item']}（剩余 {p['item_uses_left']} 次）",
    ]
    if lite:
        lines[-1] += f"——{ITEM_DESC[p['item']]}"
    for extra in p.get("extra_items", []):
        extra_line = f"## 道具：{extra}（剩余 {ITEM_USES[extra]} 次）"
        if lite:
            extra_line += f"——{ITEM_DESC[extra]}"
        lines.append(extra_line)
    if not lite:
        lines.append("道具效果见规则书 §4。")
    if p["profession"] == "建筑师" and p["identity"] in ("杀手", "帮凶"):
        lines += ["", "## 开局准备", "你是反派建筑师：开局准备阶段确认密道网即可（无需选定出口）；晚间休息时可经密道前往书房/监控室/地下室之一。"]
    if p["item"] == "窃听器":
        lines += ["", "## 开局准备", "你持有窃听器：需在开局准备阶段秘密选定一个非卧室房间放置（含会客室——它是唯一能听到会客室交谈的手段）。"]
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
    ap.add_argument("--seed", type=int, default=None, help="随机种子，缺省随机生成")
    ap.add_argument("--players", type=int, default=6, help="局人数（当前仅支持 6）")
    ap.add_argument("--seats", default=None, help="手动指定席位：id 或名字，逗号分隔")
    ap.add_argument("--exclude", default=None,
                    help="随机抽席时屏蔽席位：id 或名字，逗号分隔（中美两侧各需剩余 ≥3 席；仅影响随机模式）")
    ap.add_argument("--config", default="players.toml")
    ap.add_argument("--personas", default="personas.toml")
    ap.add_argument("--rules", default=None, help="规则书路径（缺省按 --lite 选择 rules-6-lite.md / rules-6.md）")
    ap.add_argument("--lite", action="store_true",
                    help="失忆版精简规则集：AI 只知生存须知（职业/道具全表与分配结构不下发），裁决规则不变")
    ap.add_argument("--out", default=None, help="输出目录（缺省 game/<seed>/）")
    args = ap.parse_args()

    if args.players != 6:
        raise SystemExit("当前仅支持 6 人局；8 人局开局将随第二季实现")
    rules_path = args.rules or ("docs/rules-6-lite.md" if args.lite else "docs/rules-6.md")
    if not Path(rules_path).exists():
        raise SystemExit(f"规则书不存在：{rules_path}")
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(10**6)

    players, _ = load_config(args.config)
    personas = load_personas(Path(args.personas))
    seat_keys = [s.strip() for s in args.seats.split(",") if s.strip()] if args.seats else None
    exclude: set[str] = set()
    exclude_names: list[str] = []
    for k in [s.strip() for s in args.exclude.split(",") if s.strip()] if args.exclude else []:
        p = find_player(players, k)
        if p is None:
            raise SystemExit(f"屏蔽席位 {k!r} 不在配置中")
        exclude.add(p.id)
        exclude_names.append(p.name)

    state = deal(seed, players, personas, seat_keys, lite=args.lite, exclude=exclude)

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
    shutil.copyfile(rules_path, out / "rules.md")
    shutil.copyfile(args.config, out / "players.toml")
    (out / "public" / "roster.md").write_text(write_roster(state["players"]), encoding="utf-8")
    (out / "public" / "announcement.md").write_text(announcement(state["safe_item"]), encoding="utf-8")
    for p in state["players"]:
        seat = p["id"] or p["name"]
        (out / "private" / seat / "card.md").write_text(write_card(p, state["players"], lite=args.lite), encoding="utf-8")

    # 主持人视角汇总（含身份，勿外发）
    print(f"seed = {seed}  →  {out}/" + ("（lite 规则集）" if args.lite else ""))
    if exclude:
        print("屏蔽席位：" + "、".join(exclude_names))
    print("行动顺序：" + " → ".join(state["order"]))
    print(f"保险箱：{state['safe_item']}（开局公告已公开）｜桌上：{TABLE_WEAPON}（开局公告已公开）")
    print()
    print(f"{'席位':<10}{'身份':<6}{'职业':<10}{'道具':<10}")
    order_key = {name: i for i, name in enumerate(state["order"])}
    for p in sorted(state["players"], key=lambda p: order_key[p["name"]]):
        print(f"{p['name']:<10}{p['identity']:<6}{p['profession']:<10}{p['item']:<10}")


if __name__ == "__main__":
    main()
