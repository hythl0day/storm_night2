"""谋杀判定（rules-6 §7.6/§7.7）、延时装置、舞台魔术（M3 引擎）。

裁定要点：
- 仅杀手可发动谋杀；每阶段一次机会（v3.22：自由活动与晚间休息各自独立一次），
  未遂同样消耗机会并计入无杀倒计时。
- 在场非反派角色数：正常 ≤1，黑暗房间 ≤2。
- 空手附加条件（满足其一）：①帮凶在场 ②本人体格≥目标 ③本人男且目标女。
  黑暗房间中的空手谋杀无任何附加条件（v3.22：卧室夜间黑暗由 is_dark 统一裁定）。
- 凶器谋杀无条件（用一次即消耗，无论成败）；环境凶器（雕像/电线）无条件且不限次数。
- 自卫自动触发：武器克制仅覆盖 刀↔棍 与 枪防刀棍；急救箱/不明药剂（药用）/敏捷闪避防一切
  （含不明药剂、环境凶器、空手）；延时装置与舞台魔术不可被任何方式防御。
- 自卫用的武器不消耗；急救箱/不明药剂（药用）与【敏捷闪避】触发即消耗（优先消耗急救箱）。
- 现场无其他非反派角色时杀手缴获尸体全部未使用道具，且案件证物可被毁证（v3.18 须身处现场；v3.19 仅限案发当天）。
"""
from __future__ import annotations

from .state import (
    ACTION_STAGES,
    ENV_WEAPON,
    FREE_ROOMS,
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
UNDEFENDABLE = ("延时装置", "舞台魔术")


def _present(state: dict, room: str) -> list[dict]:
    return [p for p in state["players"] if p["alive"] and state["positions"].get(p["name"]) == room]


def _defense(state: dict, target: dict, method: str, room: str, day: int, savior: str | None) -> tuple[bool, str | None]:
    """自卫矩阵（自动触发）。返回 (是否防御成功, 防御途径)。延时装置/舞台魔术不可防御。"""
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
    return False, None


def _apply_kill(state: dict, victim: str, method: str, room: str, culprit: str) -> dict:
    """击杀落账：死亡、案件记录、无杀倒计时；满足条件时杀手缴获尸体道具。"""
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
        "body_moved": False,
        "falsified": None,
        "looted": looted,
        "evidence_vulnerable": not others_nv,
        "sealed": False,
    }
    state["cases"].append(case)
    state["last_murder_day"] = day
    return case


def _winner_tail(state: dict, ev: dict) -> dict:
    if check_winner(state):
        ev["winner"] = state["winner"]
        ev["win_reason"] = state["win_reason"]
    return ev


def murder(state: dict, killer: str, target: str, weapon: str | None = None, savior: str | None = None) -> dict:
    """杀手谋杀裁决。weapon 为 None 表示空手，否则为凶器名或环境凶器（雕像/电线）。

    savior：同房持有「急救箱」或「不明药剂」（药用）者可选择救治目标（§4 急救箱/不明药剂"也可用于同在现场的其他人"，
    属主动选择而非自动触发，故由编排器询问后传入）。
    失败时杀手只得知"失败"，无法分辨原因；目标与在场者得知"有人遇袭"。
    """
    s = state
    ph = s["phase"]
    day, stage = ph["day"], ph["stage"]
    if s["winner"]:
        return reject("murder", "游戏已结束")
    kp = get_player(s, killer)
    if kp is None or not kp["alive"]:
        return reject("murder", "杀手不在场或已死亡")
    if kp["identity"] != "杀手":
        return reject("murder", "只有杀手能发动谋杀")
    if stage not in ACTION_STAGES:
        return reject("murder", "当前阶段不可行凶")
    if s["stage_murder_used"]:
        return reject("murder", "本阶段谋杀机会已用完")
    tp = get_player(s, target)
    if tp is None or not tp["alive"]:
        return reject("murder", "目标不存在或已死亡")
    if is_villain(tp):
        return reject("murder", "不能以反派角色为目标")
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
            return reject("murder", "未持有可用凶器")
    present = _present(s, room)
    non_villain = [p for p in present if not is_villain(p)]
    dark = is_dark(s, room)
    if len(non_villain) > (2 if dark else 1):
        return reject("murder", "在场非反派角色过多")
    # 黑暗房间空手无任何附加条件（v3.22：卧室夜间黑暗由 is_dark 统一裁定，原"夜间卧室特权"删除）
    if method == "空手" and not dark:
        ok = (
            any(p["identity"] == "帮凶" for p in present)
            or kp["physique"] >= tp["physique"]
            or (kp["gender"] == "男" and tp["gender"] == "女")
        )
        if not ok:
            return reject("murder", "空手谋杀条件不满足")
    if weapon is not None and weapon not in ("雕像", "电线"):
        consume_item_entry(kp, entry)
    spent = f"{weapon}已消耗；" if weapon is not None and weapon not in ("雕像", "电线") else ""
    s["stage_murder_used"] = True
    s["last_murder_day"] = day
    defended, via = _defense(s, tp, method, room, day, savior)
    witnesses = [p["name"] for p in present if p["name"] not in (killer, target)]
    if defended:
        s["attempts"].append({
            "target": target, "day": day, "stage": stage, "room": room, "period": current_period(s),
            "defense": via,  # 防御途径（host 侧台账用，不下发给任何玩家）
        })
        private = {killer: f"谋杀失败（{spent}你无法分辨是武器、药品还是技能所致）", target: "你遭遇了袭击，但侥幸幸存"}
        for w in witnesses:
            private[w] = "有人遇袭，但你不知道袭击者是谁"
        ev = {"ok": True, "type": "murder_attempt", "public": None, "private": private, "defended": True}
        return _winner_tail(s, ev)
    case = _apply_kill(s, target, method, room, killer)
    suffix = f"（{weapon}已消耗）" if weapon is not None and weapon not in ("雕像", "电线") else ""
    private = {killer: f"谋杀成功{suffix}", target: "你被杀害了"}
    if case["looted"]:
        private[killer] = f"谋杀成功{suffix}；缴获道具：{'、'.join(case['looted'])}"
    for w in witnesses:
        private[w] = "有人被谋杀，但你不知道凶手是谁"
    ev = {"ok": True, "type": "murder", "public": None, "private": private, "case": case}
    return _winner_tail(s, ev)


def set_device(state: dict, user: str, room: str) -> dict:
    """机械师反派【延时装置】：房间行动中在本人所在非卧室房间设置，整局限一次。"""
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
            "private": {user: f"延时装置已设置于{room}；本阶段不触发，后续进入结算时开始检查"}}


def trigger_devices(state: dict) -> list[dict]:
    """房间分配结算后检查装置触发：房间内只有一名非反派角色单独进入时击杀。

    不占杀手当阶段谋杀机会；设置者无需在场（即使已死亡也生效）；不可防御。
    """
    s = state
    events = []
    for room, dev in list(s["devices"].items()):
        if not dev["active"]:
            continue
        occ = _present(s, room)
        if len(occ) == 1 and occ[0]["identity"] == "平民":
            dev["active"] = False
            victim = occ[0]["name"]
            case = _apply_kill(s, victim, "延时装置", room, dev["setter"])
            ev = {
                "ok": True,
                "type": "device_trigger",
                "public": None,
                "private": {victim: "你触发了暗藏的机关，被杀害了", dev["setter"]: f"你的延时装置在{room}触发，击杀了{victim}"},
                "case": case,
            }
            events.append(_winner_tail(s, ev))
    return events


def stage_magic(state: dict, user: str, target: str) -> dict:
    """魔术师反派【舞台魔术】：任意人数非反派在场时可空手谋杀，不可防御。

    目击者结论固定为"这是谋杀，凶手就在当时在场的人之中"（不指向任何人，魔术师不被排除）。
    裁定解释：舞台魔术本身是谋杀行为，同时消耗当阶段谋杀机会（技能整局本就限一次）。
    """
    s = state
    ph = s["phase"]
    if s["winner"]:
        return reject("stage_magic", "游戏已结束")
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("stage_magic", "玩家不存在或已死亡")
    if not is_villain(p) or p["profession"] != "魔术师":
        return reject("stage_magic", "只有反派魔术师能使用舞台魔术")
    if skill_left(s, user, "舞台魔术") <= 0:
        return reject("stage_magic", "舞台魔术次数已用完")
    if ph["stage"] not in ACTION_STAGES:
        return reject("stage_magic", "当前阶段不可行凶")
    if s["stage_murder_used"]:
        return reject("stage_magic", "本阶段谋杀机会已用完")
    tp = get_player(s, target)
    if tp is None or not tp["alive"] or is_villain(tp):
        return reject("stage_magic", "目标必须是在场的非反派角色")
    room = s["positions"].get(user)
    if room is None or s["positions"].get(target) != room:
        return reject("stage_magic", "魔术师与目标不在同一房间")
    consume_skill(s, user, "舞台魔术")
    s["stage_murder_used"] = True
    s["last_murder_day"] = ph["day"]
    case = _apply_kill(s, target, "舞台魔术", room, user)
    private = {user: "舞台魔术成功", target: "你被杀害了"}
    for w in _present(s, room):
        if w["name"] not in (user, target):
            private[w["name"]] = "这是谋杀，凶手就在当时在场的人之中"
    ev = {"ok": True, "type": "stage_magic", "public": None, "private": private, "case": case}
    return _winner_tail(s, ev)
