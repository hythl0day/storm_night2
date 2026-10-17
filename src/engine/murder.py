"""谋杀判定（rules-6 §7.6/§7.7）、延时装置、舞台魔术、强袭、缓释毒剂、密道谋杀（M3 引擎）。

裁定要点（v4.0）：
- 两名杀手都拥有完整谋杀权（双杀手制，删除帮凶检查与密道借道分支）；每个谋杀窗口全队至多一次尝试
  （stage_murder_used：仅放弃/无合法目标顺延），未遂同样消耗尝试并计入无杀倒计时。
- 每日成功封顶（day_kill_capped）：成功击杀（含缓释毒剂下毒、延时装置触发）置位后，
  当天一切谋杀/技能杀驳回；进入下一个自由活动阶段时重置。未遂只消耗本窗口尝试、不封顶当天。
- 中毒目标保护：已中毒（缓释毒剂延迟死亡队列中）的目标被排除出一切谋杀/技能杀对象。
- 在场非反派角色数：亮房 ≤1；v3.31 起黑暗房间不设人数限制（房间容量仍是硬约束）。
- 空手附加条件（满足其一）：①另一名杀手在场 ②本人体格≥目标 ③本人男且目标女。
  黑暗房间中的空手谋杀无任何附加条件（卧室夜间黑暗由 is_dark 统一裁定）。
- 凶器谋杀无条件（用一次即消耗，无论成败）；环境凶器（雕像/电线）无条件且不限次数；
  v3.31：会客室桌上凶器视同环境凶器供反派临时取用（无需持有，用后消耗，桌上变动次日清晨公告）。
- 自卫自动触发：武器克制仅覆盖 刀↔棍 与 枪防刀棍；急救箱/不明药剂（药用）/敏捷闪避/药理学防一切
  （含不明药剂、环境凶器、空手、舞台魔术）；延时装置、缓释毒剂、强袭不可被任何方式防御。
- 自卫用的武器不消耗；急救箱/不明药剂（药用）/【敏捷闪避】/【药理学】触发即消耗（优先消耗急救箱）。
- 现场无其他非反派角色时杀手缴获尸体全部未使用道具，且案件可在毁证窗口被篡改（v3.18 须身处现场；v3.19 仅限案发当天）。
- v3.30：旁观者口径统一——黑暗目击者/未遂在场者/舞台魔术在场者当场只知"些许奇怪的动静"，
  不知死亡发生；死亡随"再进现场发现"或"次日清晨公告"公开（发现/报案见 state.discover_bodies 与 ledger.report）。
"""
from __future__ import annotations

import random

from .state import (
    ACTION_STAGES,
    ENV_WEAPON,
    FREE_ROOMS,
    HALL,
    WOUND_LABEL,
    check_winner,
    consume_item_entry,
    consume_skill,
    current_period,
    get_player,
    grant_item,
    held_item,
    held_item_names,
    is_dark,
    is_villain,
    reject,
    skill_left,
)

WEAPON_DEFENSE = {"刀": {"左轮手枪", "棒球棍"}, "棒球棍": {"左轮手枪", "刀"}}
UNDEFENDABLE = ("延时装置", "缓释毒剂", "强袭")  # v4.0：舞台魔术改为可防御（救援系可抵消）

CLUE_TRAITS = ("性别", "国籍", "体格")


def lock_clue_trait(state: dict, case: dict) -> str:
    """案发时随机锁定凶手线索项（性别/国籍/体格；同一案件所有人查到的都是同一项）。

    引擎不带 rng：用 存档seed × 案件号 做确定性随机——同一存档结果固定，无法通过重试刷随机。
    """
    trait = case.get("clue_trait")
    if trait is None:
        rng = random.Random(f"{state.get('seed')}:{case['id']}:clue")
        trait = rng.choice(CLUE_TRAITS)
        case["clue_trait"] = trait
    return trait


def _present(state: dict, room: str) -> list[dict]:
    return [p for p in state["players"] if p["alive"] and state["positions"].get(p["name"]) == room]


def poisoned_names(state: dict) -> set[str]:
    """已中毒（延迟死亡队列中）的玩家——不可再成为任何谋杀/技能杀的目标。"""
    return {e["name"] for e in state["poisoned"]}


def _defense(state: dict, target: dict, method: str, room: str, day: int, savior: str | None) -> tuple[bool, str | None]:
    """自卫矩阵（自动触发）。返回 (是否防御成功, 防御途径)。延时装置/缓释毒剂/强袭不可防御。"""
    if method in UNDEFENDABLE:
        return False, None
    holds = held_item_names(target, day)
    blockers = WEAPON_DEFENSE.get(method, set())
    if holds & blockers:
        return True, "武器自卫"
    kit = held_item(target, "急救箱", day)
    if kit is not None:
        consume_item_entry(target, kit)
        return True, "急救箱"
    med = held_item(target, "不明药剂", day)
    if med is not None:
        consume_item_entry(target, med)
        return True, "不明药剂（药用）"
    if skill_left(state, target["name"], "敏捷闪避") > 0:
        consume_skill(state, target["name"], "敏捷闪避")
        return True, "敏捷闪避"
    if skill_left(state, target["name"], "药理学") > 0:
        # v4.0：【药理学】可自救（等同急救箱）；反派医师持有时才登记，故此处天然限反派医师
        consume_skill(state, target["name"], "药理学")
        return True, "药理学"
    if savior and savior != target["name"]:
        sp = get_player(state, savior)
        if sp and sp["alive"] and state["positions"].get(savior) == room:
            skit = held_item(sp, "急救箱", day)
            if skit is not None:
                consume_item_entry(sp, skit)
                return True, "急救箱(他人)"
            smed = held_item(sp, "不明药剂", day)
            if smed is not None:
                consume_item_entry(sp, smed)
                return True, "不明药剂（药用·他人）"
            if skill_left(state, savior, "药理学") > 0:
                consume_skill(state, savior, "药理学")
                return True, "药理学(他人)"
    return False, None


def _apply_kill(state: dict, victim: str, method: str, room: str, culprit: str) -> dict:
    """击杀落账：死亡、案件记录（含杀手线索锁定）、无杀倒计时；满足条件时杀手缴获尸体道具。"""
    day = state["phase"]["day"]
    vp = get_player(state, victim)
    vp["alive"] = False
    others_nv = any(
        p["alive"] and not is_villain(p) and state["positions"].get(p["name"]) == room
        for p in state["players"]
    )
    looted: list[str] = []
    kp = get_player(state, culprit)
    if not others_nv and kp and kp["alive"] and state["positions"].get(culprit) == room:
        for e in list(vp["items"]):
            if e["uses_left"] > 0:
                grant_item(kp, e["name"], e["uses_left"], day)
                looted.append(e["name"])
                vp["items"].remove(e)
    case = {
        "id": len(state["cases"]) + 1,
        "victim": victim,
        "room": room,
        "crime_room": room,
        "day": day,
        "stage": state["phase"]["stage"],
        "period": current_period(state),
        "method": method,
        "wound": WOUND_LABEL[method],
        "culprit": culprit,
        "body_moved": False,          # v4.0：尸体不可移动（恒为 False，仅作篡改噪声项）
        "clue_trait": None,           # v4.0：案发时随机锁定的杀手线索项（惰性锁定，见 lock_clue_trait）
        "falsified": None,            # v4.0 篡改：None 或 {调查项: 假值}（一次定型，此后本案调查永久为假）
        "investigated": False,        # v4.0：每起案件只被调查/篡改一次，首个处理即定型
        "looted": looted,
        "evidence_vulnerable": not others_nv,
        "sealed": False,
        "announced": False,       # v3.30：是否已随清晨公告公开（未公开前可被发现/报案）
        "discovered_by": [],      # v3.30：私下发现者（host 侧台账，不公开放送）
        "reported_by": [],        # v3.30：报案人（清晨公告并列刊出）
    }
    lock_clue_trait(state, case)  # 案发时锁定杀手线索项
    state["cases"].append(case)
    state["last_murder_day"] = day
    return case


def _winner_tail(state: dict, ev: dict) -> dict:
    if check_winner(state):
        ev["winner"] = state["winner"]
        ev["win_reason"] = state["win_reason"]
    return ev


def _check_window(s: dict, etype: str, killer: str, target: str | None = None):
    """谋杀窗口公共校验。返回 (事件驳回 | None, 杀手player, 目标player | None)。"""
    if s["winner"]:
        return reject(etype, "游戏已结束"), None, None
    kp = get_player(s, killer)
    if kp is None or not kp["alive"]:
        return reject(etype, "杀手不在场或已死亡"), None, None
    if not is_villain(kp):
        return reject(etype, "只有杀手能发动谋杀"), None, None
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject(etype, "当前阶段不可行凶"), None, None
    if s["stage_murder_used"]:
        return reject(etype, "本窗口的谋杀尝试已用完（每窗口全队至多一次）"), None, None
    if s["day_kill_capped"]:
        return reject(etype, "今天已有一起成功谋杀（每天全队至多一起）"), None, None
    tp = None
    if target is not None:
        tp = get_player(s, target)
        if tp is None or not tp["alive"]:
            return reject(etype, "目标不存在或已死亡"), None, None
        if is_villain(tp):
            return reject(etype, "不能以反派角色为目标"), None, None
        if target in poisoned_names(s):
            return reject(etype, "目标已中毒（缓释毒剂生效中），不可再成为谋杀对象"), None, None
    return None, kp, tp


def _presence_ok(s: dict, room: str) -> bool:
    """人数限制：亮房在场非反派 ≤1；黑暗房间不设人数限制（容量仍是硬约束）。"""
    if is_dark(s, room):
        return True
    return len([p for p in _present(s, room) if not is_villain(p)]) <= 1


def _unarmed_ok(s: dict, kp: dict, tp: dict, room: str) -> bool:
    """空手附加条件（亮房适用，黑暗豁免）：另一名杀手在场 / 体格≥目标 / 男对女。"""
    if is_dark(s, room):
        return True
    present = _present(s, room)
    return (
        any(is_villain(p) and p["name"] != kp["name"] for p in present)
        or kp["physique"] >= tp["physique"]
        or (kp["gender"] == "男" and tp["gender"] == "女")
    )


def _fire(s: dict, killer: str, target: str, method: str, room: str, savior: str | None = None,
          spent_note: str = "") -> dict:
    """攻击落账共用段：消耗窗口尝试、重置倒计时、自卫判定、未遂/成功事件。"""
    day, stage = s["phase"]["day"], s["phase"]["stage"]
    s["stage_murder_used"] = True
    s["last_murder_day"] = day
    tp = get_player(s, target)
    defended, via = _defense(s, tp, method, room, day, savior)
    witnesses = [p["name"] for p in _present(s, room) if p["name"] not in (killer, target)]
    if defended:
        s["attempts"].append({
            "target": target, "day": day, "stage": stage, "room": room, "period": current_period(s),
            "defense": via,  # 防御途径（host 侧台账用，不下发给任何玩家）
        })
        private = {killer: f"谋杀失败（{spent_note}你无法分辨是武器、药品还是技能所致）",
                   target: "你遭遇了袭击，但侥幸幸存"}
        for w in witnesses:
            private[w] = "你听到些许奇怪的动静，但不知道是谁造成的。"
        ev = {"ok": True, "type": "murder_attempt", "public": None, "private": private, "defended": True}
        return _winner_tail(s, ev)
    case = _apply_kill(s, target, method, room, killer)
    s["day_kill_capped"] = True  # v4.0：成功击杀每天全队至多一起
    suffix = f"（{spent_note[:-1]}）" if spent_note else ""
    private = {killer: f"谋杀成功{suffix}", target: "你被杀害了"}
    if case["looted"]:
        private[killer] = f"谋杀成功{suffix}；缴获道具：{'、'.join(case['looted'])}"
    for w in witnesses:
        private[w] = "你听到些许奇怪的动静，但不知道是谁造成的。"
    ev = {"ok": True, "type": "murder", "public": None, "private": private, "case": case}
    return _winner_tail(s, ev)


def murder(state: dict, killer: str, target: str, weapon: str | None = None, savior: str | None = None) -> dict:
    """杀手谋杀裁决。weapon 为 None 表示空手，否则为凶器名或环境凶器（雕像/电线）。

    savior：同房持有「急救箱」/「不明药剂」（药用）/【药理学】者可选择救治目标
    （§3/§4"也可用于同在现场的其他人"，属主动选择而非自动触发，故由编排器询问后传入）。
    失败时杀手只得知"失败"，无法分辨原因；目标得知自己遇袭幸存，在场的其他人只听到些许奇怪的动静、
    不知发生了什么（v3.30 口径，与黑暗旁观者/舞台魔术在场者统一）。
    v3.31：身处会客室的反派可把桌上凶器视同环境凶器临时取用（无需持有，用后消耗，桌上变动次日清晨公告）。
    """
    s = state
    day = s["phase"]["day"]
    err, kp, tp = _check_window(s, "murder", killer, target)
    if err:
        return err
    room = s["positions"].get(killer)
    if room is None or s["positions"].get(target) != room:
        return reject("murder", "杀手与目标不在同一房间")
    method = weapon or "空手"
    if weapon in ("雕像", "电线"):
        if ENV_WEAPON.get(room) != weapon:
            return reject("murder", "该房间没有此环境凶器")
    elif weapon is not None:
        entry = held_item(kp, weapon, day)
        if entry is None:
            # v3.31：会客室桌上凶器视同环境凶器临时取用（反派 + 身处会客室；用后消耗，见下）
            if not (room == HALL and any(e["name"] == weapon for e in s["table_items"])):
                return reject("murder", "未持有可用凶器")
    if not _presence_ok(s, room):
        return reject("murder", "在场非反派角色过多")
    if method == "空手" and not _unarmed_ok(s, kp, tp, room):
        return reject("murder", "空手谋杀条件不满足")
    spent_note = ""
    if weapon is not None and weapon not in ("雕像", "电线"):
        if entry is not None:
            consume_item_entry(kp, entry)
        else:  # v3.31 桌上凶器：消耗桌上份额，变动记入次日清晨公告
            tentry = next(e for e in s["table_items"] if e["name"] == weapon)
            s["table_items"].remove(tentry)
            s["table_events"].append(f"会客室桌上的「{weapon}」已消耗")
        spent_note = f"{weapon}已消耗；"
    return _fire(s, killer, target, method, room, savior, spent_note)


def pharmacology_kill(state: dict, user: str, target: str, savior: str | None = None) -> dict:
    """医师反派【药理学·杀】：等同凶器「不明药剂」——凶器判定（满足人数条件即可，无体格/性别/同伙要求）；
    可被急救箱/敏捷闪避/药理学/药用不明药剂防御；武器伤痕"毒或药物"。技能×1，无论成败消耗。"""
    s = state
    err, kp, tp = _check_window(s, "pharmacology_kill", user, target)
    if err:
        return err
    if kp["profession"] != "医师":
        return reject("pharmacology_kill", "只有反派医师能使用药理学")
    if skill_left(s, user, "药理学") <= 0:
        return reject("pharmacology_kill", "药理学次数已用完")
    room = s["positions"].get(user)
    if room is None or s["positions"].get(target) != room:
        return reject("pharmacology_kill", "你与目标不在同一房间")
    if not _presence_ok(s, room):
        return reject("pharmacology_kill", "在场非反派角色过多")
    consume_skill(s, user, "药理学")
    return _fire(s, user, target, "药理学", room, savior, "药理学已消耗；")


def strong_attack(state: dict, user: str, target: str) -> dict:
    """运动员反派【强袭】：谋杀窗口空手强杀——无任何附加条件（体格/性别/同伙豁免）、不可防御；
    但仍受人数限制（亮房非反派 ≤1 / 黑暗不限）。武器伤痕"空手"。"""
    s = state
    err, kp, tp = _check_window(s, "strong_attack", user, target)
    if err:
        return err
    if kp["profession"] != "运动员":
        return reject("strong_attack", "只有反派运动员能使用强袭")
    if skill_left(s, user, "强袭") <= 0:
        return reject("strong_attack", "强袭次数已用完")
    room = s["positions"].get(user)
    if room is None or s["positions"].get(target) != room:
        return reject("strong_attack", "你与目标不在同一房间")
    if not _presence_ok(s, room):
        return reject("strong_attack", "在场非反派角色过多")
    consume_skill(s, user, "强袭")
    return _fire(s, user, target, "强袭", room)


def slow_poison(state: dict, user: str, target: str) -> dict:
    """化学家反派【缓释毒剂】：谋杀窗口对同房一名非反派下毒——走空手谋杀判定（人数限制 +
    空手附加条件，黑暗豁免），不可防御。目标不知情、照常行动，当夜睡梦中死亡
    （翻页时结算：案发房间=其卧室，时段=晚间休息，伤痕=毒或药物）。下毒即计入谋杀行为并视为当天成功封顶。"""
    s = state
    day = s["phase"]["day"]
    err, kp, tp = _check_window(s, "slow_poison", user, target)
    if err:
        return err
    if kp["profession"] != "化学家":
        return reject("slow_poison", "只有反派化学家能使用缓释毒剂")
    if skill_left(s, user, "缓释毒剂") <= 0:
        return reject("slow_poison", "缓释毒剂次数已用完")
    room = s["positions"].get(user)
    if room is None or s["positions"].get(target) != room:
        return reject("slow_poison", "你与目标不在同一房间")
    if not _presence_ok(s, room):
        return reject("slow_poison", "在场非反派角色过多")
    if not _unarmed_ok(s, kp, tp, room):
        return reject("slow_poison", "空手谋杀条件不满足")
    consume_skill(s, user, "缓释毒剂")
    s["stage_murder_used"] = True
    s["last_murder_day"] = day        # 以下毒那一刻计入谋杀行为（重置无杀倒计时）
    s["day_kill_capped"] = True       # 下毒即视为当天成功——本日不再有其他谋杀
    s["poisoned"].append({"name": target, "day": day, "by": user})
    # 目标与旁观者不收任何通知（毒发前无人知情）
    return {"ok": True, "type": "slow_poison", "public": None,
            "private": {user: f"你已给 {target} 下毒——TA 毫无察觉、照常行动，将于当夜睡梦中死亡"}}


def passage_kill(state: dict, user: str, target: str, weapon: str | None = None, savior: str | None = None) -> dict:
    """建筑师反派【密道谋杀】：谋杀窗口内经密道现身目标所在房间动手（出口限会客室/书房/监控室/地下室）。

    走正常谋杀判定（人数限制/凶器/空手附加条件/自卫矩阵均适用）；监控与众人眼中你当阶段
    一直在自己卧室（不留进入记录、不上夜宿名单）；杀后人在现场（可缴获、可在毁证窗口篡改）。
    整局限成功一次——未遂消耗本窗口尝试但密道保留；成功即消耗技能、密道损毁。
    """
    s = state
    err, kp, tp = _check_window(s, "passage_kill", user, target)
    if err:
        return err
    if kp["profession"] != "建筑师":
        return reject("passage_kill", "只有反派建筑师能使用密道谋杀")
    if skill_left(s, user, "建造密道") <= 0:
        return reject("passage_kill", "密道已损毁（整局限成功一次）")
    room = s["positions"].get(target)
    if room not in FREE_ROOMS:
        return reject("passage_kill", "密道只能通往会客室/书房/监控室/地下室（不含卧室）")
    method = weapon or "空手"
    day = s["phase"]["day"]
    if weapon is not None:
        entry = held_item(kp, weapon, day)
        if entry is None:
            if not (room == HALL and any(e["name"] == weapon for e in s["table_items"])):
                return reject("passage_kill", "未持有可用凶器")
    if not _presence_ok(s, room):
        return reject("passage_kill", "在场非反派角色过多")
    if method == "空手" and not _unarmed_ok(s, kp, tp, room):
        return reject("passage_kill", "空手谋杀条件不满足")
    # 校验通过后经密道现身该房间：监控不留记录（本阶段记录上你一直在自己卧室）
    s["positions"][user] = room
    if user not in s["passage_stealth"]:
        s["passage_stealth"].append(user)
    spent_note = ""
    if weapon is not None:
        if entry is not None:
            consume_item_entry(kp, entry)
        else:
            tentry = next(e for e in s["table_items"] if e["name"] == weapon)
            s["table_items"].remove(tentry)
            s["table_events"].append(f"会客室桌上的「{weapon}」已消耗")
        spent_note = f"{weapon}已消耗；"
    ev = _fire(s, user, target, method, room, savior, spent_note)
    if ev["type"] == "murder":  # 成功一次：技能消耗、密道损毁；未遂保留
        consume_skill(s, user, "建造密道")
        s["passage"] = {"used": True}
    return ev


def set_device(state: dict, user: str, room: str) -> dict:
    """机械师反派【延时装置】：房间行动中在本人所在非卧室房间预设，整局限一次。
    v4.0：不再自动触发——从下一阶段起由设置者在谋杀窗口手动启动（见 device_fire）。"""
    s = state
    if s["winner"]:
        return reject("set_device", "游戏已结束")
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("set_device", "玩家不存在或已死亡")
    if not is_villain(p) or p["profession"] != "机械师":
        return reject("set_device", "只有反派机械师能设置延时装置")
    if skill_left(s, user, "延时装置") <= 0:
        return reject("set_device", "延时装置次数已用完")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("set_device", "只能在自由活动或晚间休息的房间行动中设置")
    if room not in FREE_ROOMS:
        return reject("set_device", "延时装置只能设置在非卧室房间")
    if s["positions"].get(user) != room:
        return reject("set_device", "只能在所在房间设置")
    consume_skill(s, user, "延时装置")
    s["devices"][room] = {"setter": user, "active": True}
    return {"ok": True, "type": "set_device", "public": None,
            "private": {user: f"延时装置已预设于{room}；从下一阶段起，谋杀窗口内若该房间恰好只有一名非反派独处，"
                              f"你会收到机关推送，可远程决定启动（须你存活；占用本窗口尝试）"}}


def pending_devices(state: dict) -> list[dict]:
    """机关推送查询（供编排器谋杀窗口菜单）：装置房恰好只有一名非反派独处、设置者存活、
    本窗口尝试未用且当天未封顶 → 该装置当前可启动。"""
    s = state
    out = []
    if s["stage_murder_used"] or s["day_kill_capped"]:
        return out
    for room, dev in s["devices"].items():
        if not dev["active"]:
            continue
        setter = get_player(s, dev["setter"])
        if setter is None or not setter["alive"]:
            continue  # 设置者已死亡，装置失效
        occ = _present(s, room)
        if len(occ) == 1 and not is_villain(occ[0]):
            out.append({"room": room, "setter": dev["setter"], "victim": occ[0]["name"]})
    return out


def device_fire(state: dict, user: str) -> dict:
    """机械师反派【延时装置·启动】：谋杀窗口内远程启动——装置所在房间恰好只有一名非反派独处时将其击杀。
    占用本窗口尝试；设置者须存活；不可防御；武器伤痕"机关"；触发即视为当天成功封顶。"""
    s = state
    if s["winner"]:
        return reject("device_fire", "游戏已结束")
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("device_fire", "玩家不存在或已死亡")
    if not is_villain(p) or p["profession"] != "机械师":
        return reject("device_fire", "只有反派机械师能启动延时装置")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("device_fire", "当前阶段不可行凶")
    if s["stage_murder_used"]:
        return reject("device_fire", "本窗口的谋杀尝试已用完（每窗口全队至多一次）")
    if s["day_kill_capped"]:
        return reject("device_fire", "今天已有一起成功谋杀（每天全队至多一起）")
    ready = next((d for d in pending_devices(s) if d["setter"] == user), None)
    if ready is None:
        return reject("device_fire", "没有可启动的装置（需装置房恰好只有一名非反派独处）")
    room, victim = ready["room"], ready["victim"]
    s["devices"][room]["active"] = False
    s["stage_murder_used"] = True
    case = _apply_kill(s, victim, "延时装置", room, user)
    s["day_kill_capped"] = True
    private = {victim: "你触发了暗藏的机关，被杀害了", user: f"你远程启动了{room}的延时装置，击杀了{victim}"}
    if case["looted"]:
        private[user] += f"；缴获道具：{'、'.join(case['looted'])}"
    ev = {"ok": True, "type": "device_fire", "public": None, "private": private, "case": case}
    return _winner_tail(s, ev)


def stage_magic(state: dict, user: str, target: str, savior: str | None = None) -> dict:
    """魔术师反派【舞台魔术】：任意人数非反派在场时可空手谋杀。

    v4.0 削弱：可被「急救箱」/【敏捷闪避】/【药理学】/药用的「不明药剂」抵消（抵消则未遂、消耗本窗口尝试）。
    v3.30：在场者当场只察觉些许奇怪动静、不知是谋杀（与黑暗旁观者同口径，并非真的表演）；
    死讯随"再进现场发现"或"次日清晨公告"公开。占用本窗口尝试，整局限一次。
    """
    s = state
    err, kp, tp = _check_window(s, "stage_magic", user, target)
    if err:
        return err
    if kp["profession"] != "魔术师":
        return reject("stage_magic", "只有反派魔术师能使用舞台魔术")
    if skill_left(s, user, "舞台魔术") <= 0:
        return reject("stage_magic", "舞台魔术次数已用完")
    room = s["positions"].get(user)
    if room is None or s["positions"].get(target) != room:
        return reject("stage_magic", "魔术师与目标不在同一房间")
    consume_skill(s, user, "舞台魔术")
    return _fire(s, user, target, "舞台魔术", room, savior)
