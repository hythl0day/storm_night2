"""开局生成器（M1）：从阵容池选取席位，随机分配身份/职业/道具/行动顺序/保险箱/桌上凶器，输出 game/<seed>/ 目录。

用法：
    python -m src.deal                       # 随机种子，输出 game/<seed>/
    python -m src.deal --seed 42             # 指定种子（同 seed 两次生成完全一致）
    python -m src.deal --seed 42 --seats gpt,claude,gemini,deepseek,kimi,glm   # 手动指定 6 席
    python -m src.deal --seed 42 --seats gpt,claude    # 部分名单：指名 2 席，其余随机补足（尽量维持中美 3+3）
    python -m src.deal --seed 42 --seats gpt:杀手,claude,glm:平民   # 席位后可附身份（平民/杀手）
    python -m src.deal --seed 42 --exclude grok,doubao  # 随机抽席时屏蔽席位（id 或名字，逗号分隔）
    python -m src.deal --seed 42 --out games/test   # 自定义输出目录
    python -m src.deal --seed 42 --lite             # 失忆版精简规则集（rules-6-lite.md）：AI 只知生存须知，
                                                    # 职业/道具全表与分配结构不下发，机制靠菜单临场发现；
                                                    # 裁决规则不变（仍 v4.0），state 记 is_lite_rule=True
    python -m src.deal --seed 42 --lite --seats claude[full]       # lite 局特权席：claude 独持完整规则书
                                                    # （rules-6.md，其余席位仍是 lite），state 记 full_rule_seats
    python -m src.deal --seed 42 --lite --seats claude:杀手[full]  # 特权席可同时锁定身份；非 lite 局忽略 [full]

--seats 名单项语法：「id或名字」「id或名字:身份」，可再附 [full] 特权席标记（位置任意）。名单可全可部分（1~6 项）：
部分名单指名者保证入选，剩余席位随机补足——固定席位允许时维持中美 3+3 对称（两侧各抽差额），
不允许（如固定席位已超一侧 3 席）则提示后从剩余阵容池合并抽取。身份指定先落位，
剩余身份（多重集）洗牌后依次补给未指定的席位；指定人数超身份配额（杀手 2、平民 4）报错。

随机调用顺序（影响复现，勿随意调整）：
    席位（纯随机=中美两侧 sample+总洗牌；部分名单=补足 sample（能对称则分侧、否则合并池）+总洗牌；
          全员手动=无随机）→ 身份洗牌（有指定则洗的是剩余身份多重集）→ 职业抽取 → 反派凶器（3 选 2，左轮手枪固定上桌）
    → 万能钥匙持有人（两名杀手随机其一）→ 另一名杀手的反派道具（粉末/木马U盘 2 选 1）→ 保险箱抽池
    → 螺丝起子持有人 →（保险箱抽中反派道具时）第四平民凶器 → 平民道具池 → 行动顺序
    --exclude 只在席位抽取/补足前从阵容池剔除屏蔽席位；同 seed + 同参数（含名单与身份指定）可复现。
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
    KILLER_GUIDE_LITE,
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
from .prompts import load_prompt, render
from .providers import find_player, load_config

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
HOST = "主神"  # 措辞统一（v4.0.10）：与 Orchestrator.host_term / game_system.md 的 {host} 占位符同源
RULES_VERSION = "v4.0.10"  # v4.0.7：看破伪装技能声明改验"是否拥有"；providers 空 200 响应改可重试错误
                           # v4.0.8：看破伪装行踪改按事实判定（visits 台账），监控空白/抹除/密道隐匿不再影响
                           # v4.0.9：看破伪装道具声明改验"是否曾经持有"（ever_items 台账），消耗/转手不再误判谎言
                           # v4.0.10：措辞统一——lite 时代主持人改称主神；lite 局支持特权席独持完整规则书（--seats 名单项 [full] 标记）

IDENTITIES = ["平民", "平民", "平民", "平民", "杀手", "杀手"]

IDENTITY_BRIEF_FILE = {  # 身份简报文案：prompt/deal_identity_*.md
    "平民": "deal_identity_civilian.md",
    "杀手": "deal_identity_killer.md",
}


def _identity_brief(identity: str, lite: bool) -> str:
    """身份简报文案（prompt/deal_identity_*.md）；lite 局杀手换用无 § 引用版。"""
    name = IDENTITY_BRIEF_FILE[identity]
    if lite and identity == "杀手":
        name = name.replace(".md", "_lite.md")
    return load_prompt(name)


def announcement(safe_item: str) -> str:
    """开局公告（主神字条）：保险箱内容公开 + 桌上凶器公开。文案：prompt/deal_announcement.md。"""
    return render("deal_announcement.md",
                  safe_item=safe_item, table_weapon=TABLE_WEAPON, host=HOST) + "\n"


def load_personas(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _seat_key(p) -> str:
    """席位主键：id 优先，无 id 回退名字（与 find_player 口径一致）。"""
    return p.id or p.name


def _parse_seat_specs(specs: list[str], players: list) -> tuple[list, dict[str, str], set[str]]:
    """解析 --seats 名单：每项「id或名字」「id或名字:身份」，可再附 [full] 特权席标记
    （lite 局中独持完整规则书；位置任意，如 claude:杀手[full] 或 claude[full]:杀手）。

    返回 (席位列表, {席位key: 指定身份}, 特权席key集)。重复席位、未知席位、非法身份、身份超额均 SystemExit。
    """
    seats: list = []
    pins: dict[str, str] = {}
    privileged: set[str] = set()
    for spec in specs:
        priv = "[full]" in spec
        key, _, ident = spec.replace("[full]", "").partition(":")
        key, ident = key.strip(), ident.strip()
        p = find_player(players, key)
        if p is None:
            raise SystemExit(f"席位 {key!r} 不在配置中")
        sk = _seat_key(p)
        if sk in {_seat_key(q) for q in seats}:
            raise SystemExit(f"席位重复指定：{p.name}")
        if ident:
            if ident not in set(IDENTITIES):
                raise SystemExit(f"非法身份 {ident!r}（可选：{'/'.join(dict.fromkeys(IDENTITIES))}）")
            pins[sk] = ident
        if priv:
            privileged.add(sk)
        seats.append(p)
    for want, n in (("杀手", 2), ("平民", 4)):
        c = sum(1 for v in pins.values() if v == want)
        if c > n:
            raise SystemExit(f"身份指定超限：{want} 全局仅 {n} 人，当前指定了 {c} 人")
    return seats, pins, privileged


def deal(seed: int, players: list, personas: dict, seat_keys: list[str] | None,
         lite: bool = False, exclude: set[str] | None = None, privileged: set[str] | None = None):
    """核心分配逻辑。返回 state 字典。所有随机操作共用同一个 rng，顺序固定。
    lite 只改信息口径字段（ruleset/is_lite_rule），不消耗随机序列——同 seed 的分配逐字节一致；
    特权席（lite 局独持完整规则书）由 seat_keys 名单项的 [full] 标记指定，或由 privileged 参数
    直接传入（replay 重建旧档用）；只落 state 字段 full_rule_seats，不消耗随机序列；非 lite 局忽略。
    exclude 为屏蔽席位 id 集：随机抽席/补足前从阵容池剔除（rng 调用序列不变，同 seed + 同名单可复现）；
    与手动 seat_keys 重叠时报错。
    seat_keys 每项可为「id或名字」（纯指名）或「id或名字:身份」（指名+锁定身份），可再附 [full] 标记；
    名单可部分（1~6 项），不足 6 席时剩余随机补足（见模块 docstring）。"""
    rng = random.Random(seed)
    exclude = exclude or set()
    pins: dict[str, str] = {}
    privileged: set[str] = set(privileged or ())

    def nat(p) -> str | None:
        return personas.get(p.name, {}).get("nationality")

    # 1. 席位：手动名单（可部分、可带身份锁定/[full] 标记）或随机抽取（中美 3+3 对称）
    fixed: list = []
    if seat_keys:
        fixed, pins, priv_specs = _parse_seat_specs(seat_keys, players)
        privileged |= priv_specs
    if fixed:
        clash = [_seat_key(p) for p in fixed if p.id in exclude]
        if clash:
            raise SystemExit(f"手动席位与屏蔽名单冲突：{', '.join(clash)}")
        if len(fixed) > len(IDENTITIES):
            raise SystemExit(f"需要恰好 {len(IDENTITIES)} 席，当前指定了 {len(fixed)} 席")
        if len(fixed) == len(IDENTITIES):
            seats = fixed
            nats = [nat(p) for p in seats]
            if nats.count("美国") != 3 or nats.count("中国") != 3:
                print("提示：手动席位不是中美 3+3 对称结构")
        else:
            # 部分名单：剩余席位随机补足。固定席位允许对称时按两侧差额分抽，否则合并池抽并提示
            fixed_keys = {_seat_key(p) for p in fixed}
            pool = [p for p in players if _seat_key(p) not in fixed_keys and p.id not in exclude]
            need_us, need_cn = 3 - sum(1 for p in fixed if nat(p) == "美国"), \
                               3 - sum(1 for p in fixed if nat(p) == "中国")
            need = len(IDENTITIES) - len(fixed)
            us_pool = [p for p in pool if nat(p) == "美国"]
            cn_pool = [p for p in pool if nat(p) == "中国"]
            if need_us >= 0 and need_cn >= 0 and need_us + need_cn == need \
                    and len(us_pool) >= need_us and len(cn_pool) >= need_cn:
                seats = fixed + rng.sample(us_pool, need_us) + rng.sample(cn_pool, need_cn)
            else:
                print("提示：手动席位无法纳入中美 3+3 对称结构，剩余席位合并随机补足（不对称仅提示）")
                if len(pool) < need:
                    raise SystemExit(f"阵容池剩余席位不足：需补足 {need} 席，仅剩 {len(pool)} 席"
                                     f"（已指定 {len(fixed)} 席，屏蔽 {len(exclude)} 席）")
                seats = fixed + rng.sample(pool, need)
            rng.shuffle(seats)
    else:
        pool = [p for p in players if p.id not in exclude]
        us = [p for p in pool if nat(p) == "美国"]
        cn = [p for p in pool if nat(p) == "中国"]
        if len(us) < 3 or len(cn) < 3:
            suffix = f"（已屏蔽 {len(exclude)} 席）" if exclude else ""
            raise SystemExit(f"阵容池中美两侧不足 3 席（美国 {len(us)} / 中国 {len(cn)}）{suffix}")
        seats = rng.sample(us, 3) + rng.sample(cn, 3)
        rng.shuffle(seats)
    if len(seats) != len(IDENTITIES):
        raise SystemExit(f"需要恰好 {len(IDENTITIES)} 席，当前有 {len(seats)} 席")
    unknown_privileged = privileged - {_seat_key(p) for p in seats}
    if unknown_privileged:
        raise SystemExit(f"特权席不在本局阵容中：{'、'.join(sorted(unknown_privileged))}")

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

    # 2. 身份：锁定身份先落位，剩余身份（多重集）洗牌后依次补给未指定的席位
    remaining_idents = IDENTITIES[:]
    for p in seats:
        if pins.get(_seat_key(p)):
            remaining_idents.remove(pins[_seat_key(p)])
    rng.shuffle(remaining_idents)
    rest = iter(remaining_idents)
    identities = [pins.get(_seat_key(p)) or next(rest) for p in seats]
    # 3. 职业（v4.0：8 选 6 纯随机，无保底后处理）
    professions = rng.sample(PROFESSIONS, len(seats))
    # 4. 反派道具池（v4.0 双杀手）：两杀手各得凶器×1（刀/棒球棍/不明药剂 3 选 2，互不重复；左轮手枪固定上桌）；
    #    随机一名杀手另得「万能钥匙」，另一名另得一件反派道具（可疑的粉末/木马程序U盘 2 选 1）
    killer_idxs = [i for i, idn in enumerate(identities) if idn == "杀手"]
    villain_weapons = rng.sample(VILLAIN_WEAPON_POOL, 2)
    key_idx = rng.choice(killer_idxs)  # 万能钥匙持有人
    gear_idx = next(i for i in killer_idxs if i != key_idx)
    villain_gear = rng.choice(VILLAIN_ITEMS)
    # 5. 保险箱抽池：剩余凶器 + 未分出的反派道具 抽 1；内容随开局公告公开
    leftover_weapon = next(w for w in VILLAIN_WEAPON_POOL if w not in villain_weapons)
    safe_pool = [leftover_weapon] + [i for i in VILLAIN_ITEMS if i != villain_gear]
    safe_item = rng.choice(safe_pool)
    # 6. 平民道具池（v4.0）：螺丝起子 1 人 + 其余 3 人从「通用池 5 件 + 未分出的那件反派道具」共 6 件各抽 1
    civ_idxs = [i for i, idn in enumerate(identities) if idn == "平民"]
    screw_civ = rng.choice(civ_idxs)
    pool_civs = [i for i in civ_idxs if i != screw_civ]
    civilian_pool = list(ITEMS_GENERAL) + [i for i in VILLAIN_ITEMS if i not in (villain_gear, safe_item)]
    fourth_civ = None
    if safe_item not in WEAPONS:  # 保险箱抽中反派道具 → 剩余凶器直接发给剩余平民之一（四凶器严格必现）
        fourth_civ = rng.choice(pool_civs)
    civilian_items = rng.sample(civilian_pool, len(pool_civs) - (1 if fourth_civ is not None else 0))
    unused_items = [i for i in civilian_pool if i not in civilian_items]
    # 7. 首日行动顺序
    order = [r["name"] for r in roster]
    rng.shuffle(order)

    items_by_idx = {killer_idxs[0]: villain_weapons[0], killer_idxs[1]: villain_weapons[1],
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
    players_state[key_idx]["extra_items"] = ["万能钥匙"]
    players_state[gear_idx]["extra_items"] = [villain_gear]

    return {
        "seed": seed,
        "ruleset": "rules-6-lite.md" if lite else "rules-6.md",
        "rules_version": RULES_VERSION,  # 规则版本（统计脚本按版本分组，跨版本不混算平衡数据）
        "is_lite_rule": lite,  # 失忆版精简规则集标记；旧档缺此字段视为 False（高级规则）
        "full_rule_seats": sorted(privileged) if lite else [],  # lite 局特权席：独持完整规则书（rules-6.md）；非 lite 局忽略 [full] 标记；旧档缺此字段 = 无特权席
        "excluded_seats": sorted(exclude),  # 本局屏蔽的席位 id（旧档缺此字段 = 无屏蔽）
        "pinned_identities": dict(sorted(pins.items())),  # 手动锁定的席位身份（旧档缺此字段 = 无锁定）
        "mode": "6人局",
        "phase": {"day": 1, "stage": "开局准备"},
        "order": order,
        "safe_item": safe_item,
        "table_items": [{"name": TABLE_WEAPON, "uses_left": ITEM_USES[TABLE_WEAPON]}],  # 桌上凶器
        "unused_professions": [p for p in PROFESSIONS if p not in professions],
        "unused_items": unused_items,
        "players": players_state,
    }


def validate(state: dict) -> list[str]:
    """分配约束校验，返回问题列表（空 = 全部满足）。"""
    problems = []
    players = state["players"]
    ids = [p["identity"] for p in players]
    for want, n in [("平民", 4), ("杀手", 2)]:
        if ids.count(want) != n:
            problems.append(f"身份数量错误：{want} × {ids.count(want)}")
    profs = [p["profession"] for p in players]
    if len(set(profs)) != len(profs):
        problems.append("职业重复")
    items = [p["item"] for p in players] + [e for p in players for e in p.get("extra_items", [])]
    if len(set(items)) != len(items):
        problems.append("道具重复")
    killers = [p for p in players if p["identity"] == "杀手"]
    killer_weapons = [p["item"] for p in killers]
    for p in killers:
        if p["item"] not in VILLAIN_WEAPON_POOL:
            problems.append(f"{p['name']}（杀手）凶器非法：{p['item']}（应为 刀/棒球棍/不明药剂 之一）")
    if len(set(killer_weapons)) != len(killer_weapons):
        problems.append("两名杀手凶器重复")
    key_holders = [p for p in players if "万能钥匙" in p.get("extra_items", []) or p["item"] == "万能钥匙"]
    if len(key_holders) != 1 or key_holders[0]["identity"] != "杀手":
        problems.append("「万能钥匙」道具池错误：应恰好由一名杀手开局持有")
    gear_holders = [p for p in killers
                    if any(e in VILLAIN_ITEMS for e in p.get("extra_items", []))]
    if len(gear_holders) != 1 or len(gear_holders[0]["extra_items"]) != 1:
        problems.append("反派道具池错误：应恰好由未持万能钥匙的那名杀手持有 1 件反派道具")
    elif gear_holders[0] in key_holders:
        problems.append("同一名杀手同时持有万能钥匙与反派道具")
    for p in players:
        if p["identity"] == "平民" and p["item"] == TABLE_WEAPON:
            problems.append(f"{p['name']}（平民）持有桌上凶器「{TABLE_WEAPON}」（不进开局道具池）")
    # 平民道具池：恰好一名平民持螺丝起子；其余平民持有 通用道具 / 未分出的反派道具 /（保险箱抽中反派道具时的）剩余凶器
    screw = [p for p in players if p["item"] == "螺丝起子"]
    if len(screw) != 1 or screw[0]["identity"] != "平民":
        problems.append("螺丝起子道具池错误：应恰好一名平民开局持有")
    for p in players:
        if p["identity"] == "平民" and p["item"] == "螺丝起子":
            continue
        if p["identity"] == "平民" and p["item"] not in ITEMS_GENERAL + VILLAIN_ITEMS + VILLAIN_WEAPON_POOL:
            problems.append(f"{p['name']}（平民）持有道具池外道具：{p['item']}")
    if state.get("table_items") != [{"name": TABLE_WEAPON, "uses_left": ITEM_USES[TABLE_WEAPON]}]:
        problems.append(f"桌上凶器缺失：会客室桌上应固定放有「{TABLE_WEAPON}」")
    if state["safe_item"] in items:
        problems.append("保险箱道具与玩家道具冲突")
    if state["safe_item"] not in WEAPONS and state["safe_item"] not in VILLAIN_ITEMS:
        problems.append("保险箱道具非法（应为剩余凶器或未分配的反派道具）")
    # 四凶器必现：反派 2 件 + 桌上 1 件 + 保险箱或第四平民 1 件
    all_weapons = [p["item"] for p in players if p["item"] in WEAPONS]
    if state["safe_item"] in WEAPONS:
        all_weapons.append(state["safe_item"])
    all_weapons.append(TABLE_WEAPON)
    if sorted(all_weapons) != sorted(WEAPONS):
        problems.append("四凶器未全部入场（两名杀手/桌上凶器/保险箱或第四平民）")
    if len(state["order"]) != len(players):
        problems.append("行动顺序人数不符")
    return problems


# lite 局特权席私密卡附段（deal --seats 的 [full] 标记）：别人的册子是残页，只有你的完整——信息优势纪律
# 文案：prompt/deal_privileged_note.md（段落单行排列，write_card 按行并入，保持卡片逐字节不变）


def write_card(player: dict, players: list, lite: bool = False, privileged: bool = False) -> str:
    """私密卡。lite 模式（rules-6-lite.md 无职业/道具词条）：技能与道具内联一句话定义、
    不引用规则书章节；身份简报中的规则书引用同步剥离。
    privileged = lite 局特权席：卡片按 full 格式写（引用完整规则书章节），并附"你的册子是完整的"附段。"""
    p = player
    brief = _identity_brief(p["identity"], lite)
    lines = [
        f"# {p['name']} 的私密卡（仅本人可见）",
        "",
        f"角色：{p['gender']} / {p['nationality']}人 / 体格{physique_desc(p['physique'])}",
        "",
        f"## 身份：{p['identity']}",
        brief,
    ]
    if privileged:
        lines += ["", *load_prompt("deal_privileged_note.md").splitlines()]
    if p["identity"] == "杀手":
        partner = next(q for q in players if q["identity"] == "杀手" and q["name"] != p["name"])
        lines.append(
            f"你的同伙：{partner['name']}（同为杀手，职业 {partner['profession']}，道具 {partner['item']}）。"
        )
    if lite and p["identity"] == "杀手":
        lines += ["", "## 行凶须知", KILLER_GUIDE_LITE]
    lines += [
        "",
        f"## 职业：{p['profession']}",
        f"技能：{PROFESSION_SKILLS_GENERAL[p['profession']]}",
    ]
    if lite:
        lines[-1] += f"——{SKILL_DESC_GENERAL[p['profession']]}"
    if p["identity"] == "杀手":
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
    if p["item"] == "窃听器":
        lines += ["", "## 窃听器放置", "你持有窃听器：在自由活动或晚间休息进入目标非卧室房间后，可作为房间行动秘密放置（含会客室——它是唯一能听到会客室交谈的手段）；只记录放置后发生的交谈。"]
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
    ap.add_argument("--seats", default=None,
                    help="手动指定席位：id或名字[:身份]，逗号分隔。可全可部分——不足 6 席时剩余随机补足；"
                         "身份可选 平民/杀手（先落位，剩余身份随机补给其他席位）")
    ap.add_argument("--exclude", default=None,
                    help="随机抽席时屏蔽席位：id 或名字，逗号分隔（中美两侧各需剩余 ≥3 席；仅影响随机模式）")
    ap.add_argument("--config", default="players.toml")
    ap.add_argument("--personas", default="personas.toml")
    ap.add_argument("--rules", default=None, help="规则书路径（缺省按 --lite 选择 rules-6-lite.md / rules-6.md）")
    ap.add_argument("--lite", action="store_true",
                    help="失忆版精简规则集：AI 只知生存须知（职业/道具全表与分配结构不下发），裁决规则不变；"
                         "--seats 名单项附 [full] 标记可让该席独持完整规则书（特权席）")
    ap.add_argument("--out", default=None, help="输出目录（缺省 game/<seed>/）")
    args = ap.parse_args()

    if args.players != 6:
        raise SystemExit("当前仅支持 6 人局；8 人局开局将随第二季实现")
    rules_path = args.rules or ("docs/rules-6-lite.md" if args.lite else "docs/rules-6.md")
    if not Path(rules_path).exists():
        raise SystemExit(f"规则书不存在：{rules_path}")
    full_rules_path = ROOT / "docs" / "rules-6.md"
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(10**6)

    players, _ = load_config(args.config)
    personas = load_personas(Path(args.personas))
    seat_keys = [s.strip() for s in args.seats.split(",") if s.strip()] if args.seats else None
    if not args.lite and any("[full]" in s for s in seat_keys or []):
        print("提示：非 lite 局所有席位本来就持有完整规则书，[full] 标记已忽略")
    if args.lite and any("[full]" in s for s in seat_keys or []) and not full_rules_path.exists():
        raise SystemExit(f"完整规则书不存在：{full_rules_path}")
    exclude: set[str] = set()
    exclude_names: list[str] = []
    for k in [s.strip() for s in args.exclude.split(",") if s.strip()] if args.exclude else []:
        p = find_player(players, k)
        if p is None:
            raise SystemExit(f"屏蔽席位 {k!r} 不在配置中")
        exclude.add(p.id)
        exclude_names.append(p.name)

    state = deal(seed, players, personas, seat_keys, lite=args.lite, exclude=exclude)
    full_seats = set(state["full_rule_seats"])
    name_of = {_seat_key(p): p.name for p in players}

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
    initial_json = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    (out / "deal.json").write_text(initial_json, encoding="utf-8")
    (out / "state.json").write_text(initial_json, encoding="utf-8")
    shutil.copyfile(rules_path, out / "rules.md")
    shutil.copyfile(args.config, out / "players.toml")
    shutil.copyfile(args.personas, out / "personas.toml")
    (out / "public" / "roster.md").write_text(write_roster(state["players"]), encoding="utf-8")
    (out / "public" / "announcement.md").write_text(announcement(state["safe_item"]), encoding="utf-8")
    for p in state["players"]:
        seat = p["id"] or p["name"]
        privileged = seat in full_seats
        (out / "private" / seat / "card.md").write_text(
            write_card(p, state["players"], lite=args.lite and not privileged, privileged=privileged),
            encoding="utf-8")
        if privileged:
            # 特权席的完整规则书快照放在私密目录（host 侧素材；AI 只经 prompt 拿到内容）
            shutil.copyfile(full_rules_path, out / "private" / seat / "rules-full.md")

    # 主持人视角汇总（含身份，勿外发）
    print(f"seed = {seed}  →  {out}/" + ("（lite 规则集）" if args.lite else ""))
    if full_seats:
        print("特权席（独持完整规则书）：" + "、".join(name_of.get(k, k) for k in state["full_rule_seats"]))
    if exclude:
        print("屏蔽席位：" + "、".join(exclude_names))
    named_keys = {_seat_key(p) for spec in (seat_keys or [])
                  for p in [find_player(players, spec.replace("[full]", "").partition(":")[0].strip())] if p}
    if named_keys:
        if len(named_keys) < len(IDENTITIES):
            print(f"部分名单：指定 {len(named_keys)} 席，随机补足 {len(IDENTITIES) - len(named_keys)} 席")
        if state["pinned_identities"]:
            print("身份指定：" + "、".join(f"{name_of.get(k, k)}→{v}"
                                           for k, v in state["pinned_identities"].items()))
    print("行动顺序：" + " → ".join(state["order"]))
    print(f"保险箱：{state['safe_item']}（开局公告已公开）｜桌上：{TABLE_WEAPON}（开局公告已公开）")
    print()
    print(f"{'席位':<10}{'身份':<6}{'职业':<10}{'道具':<10}")
    order_key = {name: i for i, name in enumerate(state["order"])}
    for p in sorted(state["players"], key=lambda p: order_key[p["name"]]):
        print(f"{p['name']:<10}{p['identity']:<6}{p['profession']:<10}{p['item']:<10}")


if __name__ == "__main__":
    main()
