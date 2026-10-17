"""道具/技能台账与房间行动裁决（rules-6 §4/§5/§9，M3 引擎）。

涵盖：开锁、保险箱、赠送、手电筒、停电/发电机、监控查看与篡改、
现场调查/伪造/移尸/犯罪侧写、看破伪装、封闭空间、密道、窃听器。

通用约定：
- 技能与道具次数用尽即不可用；同名效果的道具是反派道具时仅反派可用。
- 同时持有对应技能与道具时，优先消耗技能次数（技能不可转让，结果确定）。
- 伪造错误结果取值是确定性的：武器伤痕取选项表下一项，死亡时间取当天另一时段，尸体转移取反。
"""
from __future__ import annotations

from ..gamedata import ITEM_USES, ITEMS, PROFESSIONS
from .state import (
    ACTION_STAGES,
    ALL_SKILLS,
    FREE_ROOMS,
    HALL,
    bedroom_of,
    consume_item_entry,
    consume_skill,
    consume_unlock_means,
    current_period,
    get_player,
    grant_item,
    held_item,
    is_bedroom,
    is_dark,
    is_villain,
    mark_blackout_period,
    reject,
    skill_left,
    unlock_means,
)

WOUND_OPTIONS = ["利器", "钝器", "枪", "毒或药物", "空手", "机关", "雕像", "电线"]

FALSY_SKILL = {"伪造痕迹": "伪造痕迹", "销毁证物": "销毁证物", "伪造证物": "伪造证物"}


def _wrong_result(case: dict, choice: str):
    if choice == "武器伤痕":
        i = WOUND_OPTIONS.index(case["wound"]) if case["wound"] in WOUND_OPTIONS else -1
        return WOUND_OPTIONS[(i + 1) % len(WOUND_OPTIONS)]
    if choice == "死亡时间":
        period = case["period"]
        if period.endswith("自由活动"):
            return period[: -len("自由活动")] + "晚间休息"
        return period[: -len("晚间休息")] + "自由活动"
    return not case["body_moved"]


def _falsified_wrong(kind: str | None, choice: str) -> bool:
    return (
        (kind == "伪造痕迹" and choice != "武器伤痕")
        or (kind == "销毁证物" and choice != "死亡时间")
        or (kind == "伪造证物" and choice != "尸体转移")
    )


def _roommates(state: dict, room: str, exclude: str) -> list[str]:
    return [
        p["name"]
        for p in state["players"]
        if p["alive"] and p["name"] != exclude and state["positions"].get(p["name"]) == room
    ]


def unlock(state: dict, user: str, room: str) -> dict:
    """打开一扇上锁的房门（手段：职业技能【暴力破解】/「万能钥匙」/「螺丝起子」）。

    v3.5 门锁口径：功能房（书房/监控室/地下室）的门与保险箱打开后永久开启；
    卧室被他人开锁后下一时间阶段恢复上锁（登记 bedroom_relock）；会客室夜锁每天重新落下。
    停电期间所有门锁失效，无需也无法开锁。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("unlock", "玩家不存在或已死亡")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("unlock", "当前阶段不能开锁")
    if s["power_out"]:
        return reject("unlock", "停电期间门锁全部失效，无需开锁")
    if not s["locks"].get(room, False):
        return reject("unlock", "该房间未上锁")
    means = unlock_means(s, user)
    if means is None:
        return reject("unlock", "没有可用开锁手段")
    consume_unlock_means(s, user, means)
    s["locks"][room] = False
    if is_bedroom(room) and bedroom_of(user) != room:
        s["bedroom_relock"].append(room)
    private = {user: f"你用{means}打开了{room}"}
    for w in _roommates(s, room, user):
        private[w] = f"{user} 打开了{room}的门"
    return {"ok": True, "type": "unlock", "public": None, "private": private}


def open_safe(state: dict, user: str) -> dict:
    """打开书房保险箱并取得其中道具。内容开局已全员公告（v3.2）；非黑暗时同房者看见开箱行为
    （自然知道取走的是哪件），黑暗中则无人知晓（rules-6 §7.5）。"""
    s = state
    day = s["phase"]["day"]
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("open_safe", "玩家不存在或已死亡")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("open_safe", "当前阶段不能开保险箱")
    if s["positions"].get(user) != "书房":
        return reject("open_safe", "你不在书房")
    if s["safe_open"]:
        return reject("open_safe", "保险箱已被打开")
    means = unlock_means(s, user)
    if means is None:
        return reject("open_safe", "没有可用开锁手段")
    consume_unlock_means(s, user, means)
    s["safe_open"] = True
    item = s["safe_item"]
    grant_item(p, item, ITEM_USES[item], day)
    private = {user: f"你打开了保险箱，取走了「{item}」（已收入你的道具）"}
    if not is_dark(s, "书房"):  # 黑暗中房内行为不可见（§7.5）；内容开局已公告，同房者看得见取走的是哪件
        for w in _roommates(s, "书房", user):
            private[w] = f"{user} 打开了保险箱，取走了「{item}」"
    return {"ok": True, "type": "open_safe", "public": None, "private": private}


def transfer(state: dict, giver: str, receiver: str, item: str, accept: bool = True) -> dict:
    """道具当面赠送（v3.27）：聚会后的固定交换环节取消——改在自由活动/晚间休息的房间行动中
    向同房者赠送（对方单独回应接受/拒绝）；不限每日次数（每阶段房间行动限一个动作，天然有界），
    转手不改变剩余次数。

    可见性：明亮房间同房者看得见交易（道具名仍不公开）；黑暗房间中无人知晓（§7.5 房内行为不可见口径）。
    """
    s = state
    day = s["phase"]["day"]
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("transfer", "只能在自由活动/晚间休息（每天早上的聚会结束后）当面赠送")
    gp = get_player(s, giver)
    rp = get_player(s, receiver)
    if gp is None or not gp["alive"] or rp is None or not rp["alive"]:
        return reject("transfer", "双方必须是存活玩家")
    if giver == receiver:
        return reject("transfer", "不能赠送给自己")
    if s["positions"].get(giver) != s["positions"].get(receiver):
        return reject("transfer", "对方不在你所在的房间，无法当面交付（每天聚会结束后去同一房间找 TA）")
    entry = held_item(gp, item, day)
    if entry is None:
        return reject("transfer", "你未持有可用的该道具")
    if not accept:
        return {
            "ok": True,
            "type": "transfer",
            "public": None,
            "private": {giver: f"{receiver} 拒绝了你的赠送", receiver: f"你拒绝了 {giver} 赠送的「{item}」"},
            "accepted": False,
        }
    gp["items"].remove(entry)
    grant_item(rp, entry["name"], entry["uses_left"], day)
    private = {giver: f"你把{item}交给了{receiver}", receiver: f"你收到了{item}（剩余 {entry['uses_left']} 次）"}
    room = s["positions"].get(giver)
    if not is_dark(s, room):  # 明亮房间同房者可见（道具名仍不公开）；黑暗中无人知晓
        for q in s["players"]:
            if q["alive"] and q["name"] not in (giver, receiver) and s["positions"].get(q["name"]) == room:
                private[q["name"]] = f"{giver} 把一件道具交给了 {receiver}"
    return {
        "ok": True,
        "type": "transfer",
        "public": None,
        "private": private,
        "accepted": True,
    }


def use_flashlight(state: dict, user: str) -> dict:
    """「手电筒」：身处黑暗房间（地下室/停电房间/夜间卧室）时使用，本阶段该房间不视为黑暗。

    v3.23：使用时机为进入结算后的独立"手电筒判定"环节（编排器在交谈与行动之前逐一询问）——
    点亮后房间内所有人互相可见、可正常交谈、谋杀按亮房条件判定（非反派 ≤1、空手恢复附加条件）。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("use_flashlight", "玩家不存在或已死亡")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("use_flashlight", "当前阶段不能使用手电筒")
    room = s["positions"].get(user)
    if room is None:
        return reject("use_flashlight", "你不在任何房间")
    if not is_dark(s, room):
        return reject("use_flashlight", "当前房间无需照明")
    entry = held_item(p, "手电筒", s["phase"]["day"])
    if entry is None:
        return reject("use_flashlight", "你未持有手电筒")
    consume_item_entry(p, entry)
    s["flashlight_lit"].append(room)
    private = {user: f"你打开手电筒，{room}本次不视为黑暗"}
    for w in _roommates(s, room, user):
        private[w] = "有人打开了手电筒，房间被照亮"
    return {"ok": True, "type": "use_flashlight", "public": None, "private": private}


def declare_blackout(state: dict, user: str) -> dict:
    """拉电闸制造过载（v3.2）：反派消耗一次任一开锁手段（职业技能【暴力破解】/「万能钥匙」/「螺丝起子」）声明，当天自由活动停电。

    声明窗口：公共发言/投票阶段，或自由活动阶段内选房结算之前（进入房间后才向众人公布）。
    停电期间：会客室外所有房间黑暗（含夜间卧室）、所有门锁失效（保险箱除外）、监控不记录；
    若发电机（地下室）未修复，停电延续到次日。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("declare_blackout", "玩家不存在或已死亡")
    stage = s["phase"]["stage"]
    in_window = stage in ("公共发言", "投票") or (
        stage == "自由活动" and s["rooms_entered"] != current_period(s)
    )
    if not in_window:
        return reject("declare_blackout", "只能在自由活动阶段开始前声明停电")
    if s.get("selection_begun") == current_period(s):
        return reject("declare_blackout", "选房已开始，停电声明窗口已过")
    if s["power_out"]:
        return reject("declare_blackout", "已经处于停电中")
    if not is_villain(p):
        return reject("declare_blackout", "只有反派能发动停电")
    means = unlock_means(s, user)
    if means is None:
        return reject("declare_blackout", "拉电闸需要消耗一次【暴力破解】（职业技能/「万能钥匙」/「螺丝起子」任一）")
    consume_unlock_means(s, user, means)
    s["power_out"] = True
    mark_blackout_period(s, f"第{s['phase']['day']}天自由活动")
    s["power_restored"] = None  # 新停电清除修复时间戳（v3.20：晚间恢复公告与清晨缺口精确到时段用）
    s["blackout_news"] = f"第{s['phase']['day']}天自由活动"  # 进入房间后向众人公布（编排器在选房收尾后公告）
    if s["phase"]["day"] not in s["blackout_days"]:
        s["blackout_days"].append(s["phase"]["day"])
    return {"ok": True, "type": "declare_blackout", "public": None,
            "private": {user: f"你用{means}拉开电闸制造过载——停电开始（进入房间后向众人公布；发电机被修复前停电一直持续，可跨天延续）"}}


def fix_generator(state: dict, user: str) -> dict:
    """在地下室修复发电机，结束停电。"""
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("fix_generator", "玩家不存在或已死亡")
    if not s["power_out"]:
        return reject("fix_generator", "当前没有停电")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("fix_generator", "当前阶段不能修发电机")
    if s["positions"].get(user) != "地下室":
        return reject("fix_generator", "你不在地下室")
    mark_blackout_period(s)
    s["power_out"] = False
    s["power_restored"] = current_period(s)  # v3.20：记录修复时点——晚间恢复公告与清晨监控缺口精确到时段
    return {
        "ok": True,
        "type": "fix_generator",
        "public": {"announcements": ["发电机已修复，供电恢复"]},
        "private": {},
    }


def view_monitoring(state: dict, user: str) -> dict:
    """身处监控室查看监控记录：恒返回全部已有记录（公开口径，即可能被篡改过的记录）。

    v3 起不再支持按时段/房间过滤——AI 玩家发明过滤格式会拿到误导性空结果，
    全量直读零格式摩擦。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("view_monitoring", "玩家不存在或已死亡")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("view_monitoring", "当前阶段不能查监控")
    if s["positions"].get(user) != "监控室":
        return reject("view_monitoring", "你不在监控室")
    return {"ok": True, "type": "view_monitoring", "public": None, "private": {user: s["monitoring"]}}


def tamper_monitoring(state: dict, user: str, period: str, room: str, op: str, name: str, new_name: str | None = None) -> dict:
    """【信号干扰】/「病毒U盘」：在监控室改动指定"时间段×场所"的一条进入记录。

    op: "add" 添加一条 / "remove" 移除一条 / "replace" 把一条 name 换成 new_name。
    一次只能改动一条；篡改后的记录对所有人（含公开宣读）生效。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("tamper_monitoring", "玩家不存在或已死亡")
    if not is_villain(p):
        return reject("tamper_monitoring", "只有反派能篡改监控")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("tamper_monitoring", "当前阶段不能篡改监控")
    if s["positions"].get(user) != "监控室":
        return reject("tamper_monitoring", "你不在监控室")
    if skill_left(s, user, "信号干扰") > 0:
        means = ("skill", "信号干扰")
    elif held_item(p, "病毒U盘", s["phase"]["day"]) is not None:
        means = ("item", "病毒U盘")
    else:
        return reject("tamper_monitoring", "没有【信号干扰】或「病毒U盘」")
    recs = s["monitoring"].setdefault(period, {}).setdefault(room, [])
    if op == "add":
        recs.append(name)
    elif op == "remove":
        if name not in recs:
            return reject("tamper_monitoring", "记录中没有该进入者")
        recs.remove(name)
    elif op == "replace":
        if name not in recs or not new_name:
            return reject("tamper_monitoring", "替换需要指定原有进入者与新名字")
        recs[recs.index(name)] = new_name
    else:
        return reject("tamper_monitoring", "未知篡改操作")
    if means[0] == "skill":
        consume_skill(s, user, means[1])
    else:
        consume_item_entry(p, held_item(p, means[1], s["phase"]["day"]))
    return {"ok": True, "type": "tamper_monitoring", "public": None, "private": {user: "监控记录已篡改"}}


def investigate(state: dict, user: str, case_id: int, choice: str) -> dict:
    """【现场调查】/「鲁米诺试剂」：案件调查阶段获知某起案件的一项真实结论（私下告知）。

    choice ∈ {武器伤痕, 死亡时间, 尸体转移}；被伪造的案件按伪造类型给出错误结果。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("investigate", "玩家不存在或已死亡")
    if s["phase"]["stage"] != "案件调查":
        return reject("investigate", "只能在案件调查阶段使用")
    if choice not in ("武器伤痕", "死亡时间", "尸体转移"):
        return reject("investigate", "调查项必须是 武器伤痕/死亡时间/尸体转移")
    case = next((c for c in s["cases"] if c["id"] == case_id), None)
    if case is None:
        return reject("investigate", "案件不存在")
    day = s["phase"]["day"]
    if skill_left(s, user, "现场调查") > 0:
        consume_skill(s, user, "现场调查")
    elif held_item(p, "鲁米诺试剂", day) is not None:
        consume_item_entry(p, held_item(p, "鲁米诺试剂", day))
    else:
        return reject("investigate", "没有【现场调查】或「鲁米诺试剂」")
    truth = {"武器伤痕": case["wound"], "死亡时间": case["period"], "尸体转移": case["body_moved"]}[choice]
    result = _wrong_result(case, choice) if _falsified_wrong(case["falsified"], choice) else truth
    return {
        "ok": True,
        "type": "investigate",
        "public": None,
        "private": {user: {"case": case["victim"], "room": case["room"], "choice": choice, "result": result}},
    }


def falsify(state: dict, user: str, case_id: int, kind: str) -> dict:
    """毁证类反派技能：【伪造痕迹】/【销毁证物】/【伪造证物】（「可疑的粉末」等同伪造证物）。

    仅当谋杀现场没有其他非反派角色（evidence_vulnerable）时可发动；每起案件只生效一次伪造；
    v3.18：发动者须身处案发现场（场外反派不能远程毁证，编排侧亦不再通知）；
    v3.19：毁证仅限案发当天（次日清晨调查后现场即无法改动）。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("falsify", "玩家不存在或已死亡")
    if not is_villain(p):
        return reject("falsify", "只有反派能毁证")
    if kind not in FALSY_SKILL:
        return reject("falsify", "毁证类型必须是 伪造痕迹/销毁证物/伪造证物")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("falsify", "毁证只能在自由活动/晚间休息（调查开始前）进行")
    case = next((c for c in s["cases"] if c["id"] == case_id), None)
    if case is None:
        return reject("falsify", "案件不存在")
    if case["day"] != s["phase"]["day"]:
        return reject("falsify", "毁证仅限案发当天——次日清晨调查后现场已无法改动")
    if not case["evidence_vulnerable"]:
        return reject("falsify", "现场有其他非反派角色，不能毁证")
    if s["positions"].get(user) != case["room"]:
        return reject("falsify", "你不在案发现场，无法毁证")
    if case["falsified"] is not None:
        return reject("falsify", "该案件已被伪造过")
    if skill_left(s, user, FALSY_SKILL[kind]) > 0:
        consume_skill(s, user, FALSY_SKILL[kind])
    elif kind == "伪造证物" and held_item(p, "可疑的粉末", s["phase"]["day"]) is not None:
        consume_item_entry(p, held_item(p, "可疑的粉末", s["phase"]["day"]))
    else:
        return reject("falsify", "没有对应的毁证手段")
    case["falsified"] = kind
    return {"ok": True, "type": "falsify", "public": None,
            "private": {user: f"已对 {case['victim']} 的案件（{case['room']}）发动【{kind}】"}}


def move_body(state: dict, user: str, case_id: int, to_room: str) -> dict:
    """【转移尸体】/「裹尸袋」：谋杀成功后把尸体移到另一房间而不被发现。
    v3.18：发动者须身处案发现场（场外反派不能远程移尸）；v3.19：仅限案发当天。"""
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("move_body", "玩家不存在或已死亡")
    if not is_villain(p):
        return reject("move_body", "只有反派能转移尸体")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("move_body", "当前阶段不能转移尸体")
    case = next((c for c in s["cases"] if c["id"] == case_id), None)
    if case is None:
        return reject("move_body", "案件不存在")
    if case["day"] != s["phase"]["day"]:
        return reject("move_body", "转移尸体仅限案发当天——次日清晨调查后现场已无法改动")
    if not case["evidence_vulnerable"]:
        return reject("move_body", "现场有其他非反派角色，不能转移尸体")
    if s["positions"].get(user) != case["room"]:
        return reject("move_body", "你不在案发现场，无法转移尸体")
    if case["body_moved"]:
        return reject("move_body", "尸体已被移动过")
    legal_rooms = FREE_ROOMS + [bedroom_of(q["name"]) for q in s["players"]]
    if to_room not in legal_rooms:
        return reject("move_body", f"非法房间：{to_room}")
    if to_room == case["room"]:
        return reject("move_body", "尸体已在该房间，需转移到另一个房间")
    if skill_left(s, user, "转移尸体") > 0:
        consume_skill(s, user, "转移尸体")
    elif held_item(p, "裹尸袋", s["phase"]["day"]) is not None:
        consume_item_entry(p, held_item(p, "裹尸袋", s["phase"]["day"]))
    else:
        return reject("move_body", "没有【转移尸体】或「裹尸袋」")
    case["room"] = to_room
    case["body_moved"] = True
    return {"ok": True, "type": "move_body", "public": None,
            "private": {user: f"{case['victim']} 的尸体已转移到{to_room}"}}


def report(state: dict, user: str) -> dict:
    """【报案】（v3.30，rules-6 §7.6）：房间行动——宣布发现本房间的未公告凶案，成为报案人。

    仅限身处未公开案件案发房间者（进入结算时已私下发现，见 state.discover_bodies）；
    同房多人可各自报案、清晨并列刊出；已报过者不重复记录。公开时机 = 次日清晨公告
    （死者 + 房间 + 报案人）；不报案则无人知道你知情（除非亲口说出）。
    """
    s = state
    if s["winner"]:
        return reject("report", "游戏已结束")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("report", "只能在自由活动或晚间休息的房间行动中报案")
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("report", "玩家不存在或已死亡")
    case = next((c for c in s["cases"]
                 if not c.get("announced") and c["room"] == s["positions"].get(user)), None)
    if case is None:
        return reject("report", "你所在房间没有尚未公告的尸体")
    if user not in case["reported_by"]:
        case["reported_by"].append(user)
    return {"ok": True, "type": "report", "public": None,
            "private": {user: f"你已报案：{case['victim']} 死于{case['room']}。"
                             f"你将被列为报案人，随次日清晨公告公开。"}}


def profile(state: dict, user: str, case_id: int, aspect: str) -> dict:
    """【犯罪侧写】：获知杀手本人的一项特征（性别/国籍/体格）；装置与魔术案件指向设置者/表演者。"""
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("profile", "玩家不存在或已死亡")
    if s["phase"]["stage"] != "案件调查":
        return reject("profile", "只能在案件调查阶段使用")
    if aspect not in ("性别", "国籍", "体格"):
        return reject("profile", "侧写项必须是 性别/国籍/体格")
    if skill_left(s, user, "犯罪侧写") <= 0:
        return reject("profile", "没有可用的【犯罪侧写】")
    case = next((c for c in s["cases"] if c["id"] == case_id), None)
    if case is None:
        return reject("profile", "案件不存在")
    culprit = get_player(s, case["culprit"])
    consume_skill(s, user, "犯罪侧写")
    if aspect == "性别":
        result = culprit["gender"]
    elif aspect == "国籍":
        result = culprit["nationality"]
    else:
        result = "健壮或魁梧" if culprit["physique"] >= 3 else "普通或瘦弱"
    return {
        "ok": True,
        "type": "profile",
        "public": None,
        "private": {user: {"case": case["victim"], "room": case["room"], "aspect": aspect, "result": result}},
    }


def see_through(state: dict, user: str, target: str, claims: dict) -> dict:
    """【看破伪装】/「测谎仪」：验证目标此前关于 职业/道具/技能/行踪 的声明是否含谎言。

    claims 由使用者从目标发言中提取（须引用对方实际声称过的内容）：
    {"profession", "item", "skill", "visited_rooms": [...], "denied_rooms": [...]}。
    每项裁决为三态：属实 / 谎言 / 无法验证（无效名目、对方未作此类声明、或房间无监控覆盖），
    "无法验证"不计入谎言判定。行踪以真实监控记录（monitoring_true）验证（v3.5 起监控覆盖所有房间门口，含会客室）。
    v3.12（game/20 复盘）：存在停电空白（blackout_days）时，行踪声称的"无记录"不构成谎言证据，
    判"无法验证"；全部为非法名目的 claims 整体驳回且不消耗次数（进驳回重报流程）；
    visited_rooms/denied_rooms 容错接受单个字符串。
    """
    s = state
    day = s["phase"]["day"]
    p = get_player(s, user)
    tp = get_player(s, target)
    if p is None or not p["alive"] or tp is None:
        return reject("see_through", "玩家不存在或已死亡")
    if s["phase"]["stage"] != "公共发言":
        return reject("see_through", "只能在公共发言阶段使用")
    if skill_left(s, user, "看破伪装") > 0:
        means = ("skill", "看破伪装")
    elif held_item(p, "测谎仪", day) is not None:
        means = ("item", "测谎仪")
    else:
        return reject("see_through", "没有【看破伪装】或「测谎仪」")
    if not isinstance(claims, dict) or not any(v for v in claims.values()):
        return reject("see_through", "没有可验证的声明（请在 claims 中引用对方实际声称过的内容）")

    def entered(room: str) -> bool:
        return any(target in rooms.get(room, []) for rooms in s["monitoring_true"].values())

    def monitorable(room: str) -> bool:
        return room in FREE_ROOMS or is_bedroom(room)  # v3.5：监控覆盖所有房间门口（含会客室）

    # v3.12（game/20 复盘）：存在停电空白的对局，"无记录"不能作为行踪谎言的证据——
    # 与清晨宣读"缺失不代表无人进入"同口径；有记录为证的声称不受空白影响
    gap = bool(s.get("blackout_days"))

    verdicts: dict[str, str] = {}
    wellformed = 0  # 名目合法的声明数；全部为非法名目（如整句描述）→ 整体驳回、不消耗次数（v3.12）
    if claims.get("profession"):
        c = str(claims["profession"])
        if c in PROFESSIONS:
            wellformed += 1
            verdicts[f"职业声明「{c}」"] = "属实" if c == tp["profession"] else "谎言"
        else:
            verdicts[f"职业声明「{c}」"] = "无法验证（不是有效职业名）"
    if claims.get("item"):
        c = str(claims["item"])
        if c in ITEMS:
            wellformed += 1
            verdicts[f"道具声明「{c}」"] = "属实" if held_item(tp, c, day) is not None else "谎言"
        else:
            verdicts[f"道具声明「{c}」"] = "无法验证（不是有效道具名）"
    if claims.get("skill"):
        c = str(claims["skill"])
        if c in ALL_SKILLS:
            wellformed += 1
            verdicts[f"技能声明「{c}」"] = "属实" if skill_left(s, target, c) > 0 else "谎言"
        else:
            verdicts[f"技能声明「{c}」"] = "无法验证（不是有效技能名）"
    for key, ph in (("visited_rooms", "去过"), ("denied_rooms", "没去过")):
        rooms_claimed = claims.get(key) or []
        if isinstance(rooms_claimed, str):
            rooms_claimed = [rooms_claimed]  # 容错：单个房间允许直接填字符串（v3.12）
        for r in rooms_claimed:
            r = str(r)
            if not monitorable(r):
                verdicts[f"{ph}{r}"] = "无法验证（该房间无监控覆盖）"
                continue
            wellformed += 1
            got = entered(r)
            if key == "visited_rooms":
                if got:
                    verdicts[f"{ph}{r}"] = "属实"
                elif gap:
                    verdicts[f"{ph}{r}"] = "无法验证（监控存在停电空白，不能排除其在空白期进入）"
                else:
                    verdicts[f"{ph}{r}"] = "谎言"
            else:
                if got:
                    verdicts[f"{ph}{r}"] = "谎言"
                elif gap:
                    verdicts[f"{ph}{r}"] = "无法验证（监控存在停电空白，不能确认其从未进入）"
                else:
                    verdicts[f"{ph}{r}"] = "属实"
    if not wellformed:
        return reject("see_through",
                      '没有可验证的声明：职业/道具/技能/房间须填规则书原名（如 "黑客"、"刀"、"书房"），'
                      "不要填整句描述——本次不消耗次数，请重新声明")
    if means[0] == "skill":
        consume_skill(s, user, means[1])
    else:
        consume_item_entry(p, held_item(p, means[1], day))
    n_lie = sum(1 for v in verdicts.values() if v == "谎言")
    n_true = sum(1 for v in verdicts.values() if v == "属实")
    lies = n_lie > 0
    if lies:
        conclusion = f"所验声明中含 {n_lie} 条谎言"
    elif n_true:
        conclusion = "所验声明全部属实"
    else:
        conclusion = "无有效声明可验证（无法验证项不计入谎言判定）"
    return {
        "ok": True,
        "type": "see_through",
        "public": None,
        "private": {user: {"target": target, "verdicts": verdicts, "lies": lies, "conclusion": conclusion}},
    }


def seal_room(state: dict, user: str, allowed: list[str] | None = None) -> dict:
    """【封闭空间】：自由活动时间进入房间后发动，临时锁死至下一个自由活动时间。

    除你与许可的人外任何人无法进出（开锁手段均无效；停电也不会打开它）。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("seal_room", "玩家不存在或已死亡")
    if p["profession"] != "建筑师":
        return reject("seal_room", "只有建筑师能使用封闭空间")
    if skill_left(s, user, "封闭空间") <= 0:
        return reject("seal_room", "封闭空间次数已用完")
    if s["phase"]["stage"] != "自由活动":
        return reject("seal_room", "只能在自由活动时间发动")
    room = s["positions"].get(user)
    if room is None:
        return reject("seal_room", "你不在任何房间")
    if room == HALL:
        return reject("seal_room", "会客室是开放大厅，没有可锁的大门")
    consume_skill(s, user, "封闭空间")
    s["sealed_room"] = {"room": room, "owner": user, "allowed": list(allowed or [])}
    return {"ok": True, "type": "seal_room", "public": None, "private": {user: f"{room}已临时锁死至下一个自由活动时间"}}


def set_passage(state: dict, user: str) -> dict:
    """反派建筑师【建造密道】：开局准备阶段确认密道网存在（v3 起无需指定出口）。"""
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("set_passage", "玩家不存在或已死亡")
    if s["phase"]["stage"] != "开局准备":
        return reject("set_passage", "只能在开局准备阶段确认密道")
    if not is_villain(p) or p["profession"] != "建筑师":
        return reject("set_passage", "只有反派建筑师能建造密道")
    if skill_left(s, user, "建造密道") <= 0:
        return reject("set_passage", "建造密道次数已用完")
    consume_skill(s, user, "建造密道")
    s["passage"] = {"used": False}
    return {"ok": True, "type": "set_passage", "public": None,
            "private": {user: "你确认了古堡的密道网：晚间休息时可经密道前往会客室/书房/监控室/地下室（监控不留记录；出口房间人数须满足谋杀人数条件，否则当晚无法使用；成功使用一次后密道损毁）"}}


def use_passage(state: dict, user: str, room: str) -> dict:
    """使用密道（v3.22）：晚间休息时从自己卧室经密道前往会客室/书房/监控室/地下室之一。

    - 进入条件与谋杀人数条件相同：出口房间在场非反派 ≤1（v3.31 起黑暗房间不设人数限制——容量仍是硬约束）；
      不满足则当晚无法使用——密道保留、不暴露（返回 ok=True, failed=True）；
    - 成功：监控不留记录（当晚记录上你一直在卧室），密道损毁（整局至多成功一次）；
      经密道进入会客室者不上次日清晨的夜宿名单（隐身，见 core._morning_report）；
    - 进入后可正常行动（含谋杀——v3.32 起反派（杀手或帮凶）皆可借密道动手：帮凶不受"不能主动谋杀"
      限制，击杀归属于发动者本人，同舞台魔术/延时装置口径；由编排器的谋杀窗口提供）。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("use_passage", "玩家不存在或已死亡")
    if s["phase"]["stage"] != "晚间休息":
        return reject("use_passage", "只能在晚间休息时间使用密道")
    passage = s["passage"]
    if not passage or passage["used"]:
        return reject("use_passage", "密道不可用")
    if not is_villain(p) or p["profession"] != "建筑师":
        return reject("use_passage", "只有反派建筑师能使用密道")
    if s["positions"].get(user) != bedroom_of(user):
        return reject("use_passage", "你必须在自己卧室才能使用密道")
    if room not in FREE_ROOMS:
        return reject("use_passage", "密道只能通往会客室/书房/监控室/地下室")
    non_villains = [
        q for q in s["players"]
        if q["alive"] and not is_villain(q) and s["positions"].get(q["name"]) == room
    ]
    # v3.31：进入条件与谋杀人数条件同步——黑暗出口不设人数限制（容量上限天然约束）
    if not is_dark(s, room) and len(non_villains) > 1:
        return {"ok": True, "type": "use_passage", "public": None,
                "private": {user: f"密道出口（{room}）今晚人多眼杂，无法使用——密道保留，未被任何人察觉"},
                "failed": True}
    passage["used"] = True
    s["positions"][user] = room
    s["passage_stealth"].append(user)
    return {"ok": True, "type": "use_passage", "public": None,
            "private": {user: f"你经密道抵达{room}，监控与众人眼中你当晚一直在自己卧室；密道随后损毁"}}


def place_bug(state: dict, user: str, room: str) -> dict:
    """「窃听器」：房间行动中在本人所在非卧室房间秘密放置，只记录放置后的交谈。"""
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("place_bug", "玩家不存在或已死亡")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("place_bug", "只能在自由活动或晚间休息的房间行动中放置窃听器")
    if s["bug_room"] is not None:
        return reject("place_bug", "窃听器已放置")
    if held_item(p, "窃听器", s["phase"]["day"]) is None:
        return reject("place_bug", "你未持有窃听器")
    if room not in FREE_ROOMS:
        return reject("place_bug", "窃听器必须放置在非卧室房间")
    if s["positions"].get(user) != room:
        return reject("place_bug", "必须身处目标房间才能放置窃听器")
    s["bug_room"] = room
    s["bug_placed"] = {"day": s["phase"]["day"], "stage": s["phase"]["stage"]}
    return {"ok": True, "type": "place_bug", "public": None,
            "private": {user: f"窃听器已放置于{room}；只记录放置后发生的交谈"}}


def bug_listen(state: dict, user: str) -> dict:
    """收听窃听器：晚间休息身处自己卧室时使用；放置前的交谈不追溯。"""
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("bug_listen", "玩家不存在或已死亡")
    if s["phase"]["stage"] != "晚间休息":
        return reject("bug_listen", "只能在晚间休息时间收听")
    if s["bug_room"] is None:
        return reject("bug_listen", "窃听器尚未放置")
    if s["positions"].get(user) != bedroom_of(user):
        return reject("bug_listen", "你必须身处自己卧室才能收听")
    entry = held_item(p, "窃听器", s["phase"]["day"])
    if entry is None:
        return reject("bug_listen", "窃听器收听次数已用完")
    consume_item_entry(p, entry)
    return {
        "ok": True,
        "type": "bug_listen",
        "public": None,
        "private": {user: f"你收听了{s['bug_room']}当天自由活动时段、放置后发生的交谈（内容由编排器下发）"},
        "room": s["bug_room"],
    }


def take_table_item(state: dict, user: str, item: str) -> dict:
    """取桌上物品（v3.5 rules-6 §5/§7.7）：晚间休息、身处会客室且室内无其他存活者时，取走一件桌上物品。

    白天（含自由活动滞留会客室）不可取——开放大厅众目睽睽；取用行为无人看见，
    但桌上物品的变动记入 table_events，次日清晨公告。桌上物品保留各自剩余次数。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("take_table_item", "玩家不存在或已死亡")
    if s["phase"]["stage"] != "晚间休息":
        return reject("take_table_item", "只有夜间才能取桌上物品（白天众目睽睽）")
    if s["positions"].get(user) != HALL:
        return reject("take_table_item", "你不在会客室")
    others = [q["name"] for q in s["players"]
              if q["alive"] and q["name"] != user and s["positions"].get(q["name"]) == HALL]
    if others:
        return reject("take_table_item", f"会客室里还有其他人（{'、'.join(others)}），无法悄悄取用")
    entry = next((e for e in s["table_items"] if e["name"] == item), None)
    if entry is None:
        return reject("take_table_item", "桌上没有这件物品")
    s["table_items"].remove(entry)
    grant_item(p, entry["name"], entry["uses_left"], s["phase"]["day"])
    s["table_events"].append(f"会客室桌上的「{entry['name']}」不见了")
    return {"ok": True, "type": "take_table_item", "public": None,
            "private": {user: f"你悄悄取走了会客室桌上的「{entry['name']}」（剩余 {entry['uses_left']} 次；明早众人会发现它不见了）"}}
