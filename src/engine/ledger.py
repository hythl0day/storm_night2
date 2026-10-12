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
    PUBLIC_ROOMS,
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
    """打开一扇上锁的房门（手段：【破门而入】/「万能钥匙」/「螺丝起子」）。

    门开后永久开启；例外：卧室被他人开锁后下一时间阶段恢复上锁（登记 bedroom_relock）。
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
    """交换环节道具赠送：转手不改变剩余次数；每人每天最多发起一次（被拒也计入）。"""
    s = state
    day = s["phase"]["day"]
    if s["phase"]["stage"] not in ("公共发言", "投票"):
        return reject("transfer", "只能在交换环节（公共发言后）赠送")
    gp = get_player(s, giver)
    rp = get_player(s, receiver)
    if gp is None or not gp["alive"] or rp is None or not rp["alive"]:
        return reject("transfer", "双方必须是存活玩家")
    if giver in s["gifts_used"]:
        return reject("transfer", "你今天的赠送机会已用完")
    entry = held_item(gp, item, day)
    if entry is None:
        return reject("transfer", "你未持有可用的该道具")
    s["gifts_used"].append(giver)
    if not accept:
        return {
            "ok": True,
            "type": "transfer",
            "public": {"announcements": [f"{giver} 向 {receiver} 赠送道具被拒绝"]},
            "private": {},
            "accepted": False,
        }
    gp["items"].remove(entry)
    grant_item(rp, entry["name"], entry["uses_left"], day)
    return {
        "ok": True,
        "type": "transfer",
        "public": {"announcements": [f"{giver} 把一件道具交给了 {receiver}"]},
        "private": {giver: f"你送出了{item}", receiver: f"你收到了{item}（剩余 {entry['uses_left']} 次）"},
        "accepted": True,
    }


def use_flashlight(state: dict, user: str) -> dict:
    """「手电筒」：进入地下室或遭遇停电时使用，本次所在房间不视为黑暗。"""
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
    """拉电闸制造过载（v3.2）：反派消耗一次任一开锁手段（【破门而入】/「万能钥匙」/「螺丝起子」）声明，当天自由活动停电。

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
    if s["power_out"]:
        return reject("declare_blackout", "已经处于停电中")
    if not is_villain(p):
        return reject("declare_blackout", "只有反派能发动停电")
    means = unlock_means(s, user)
    if means is None:
        return reject("declare_blackout", "拉电闸需要消耗一次【暴力破解】（【破门而入】/「万能钥匙」/「螺丝起子」任一）")
    consume_unlock_means(s, user, means)
    s["power_out"] = True
    if s["phase"]["day"] not in s["blackout_days"]:
        s["blackout_days"].append(s["phase"]["day"])
    return {"ok": True, "type": "declare_blackout", "public": None,
            "private": {user: f"你用{means}拉开电闸制造过载——今天停电（进入房间后向众人公布）"}}


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
    s["power_out"] = False
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
        "private": {user: {"case_id": case_id, "choice": choice, "result": result}},
    }


def falsify(state: dict, user: str, case_id: int, kind: str) -> dict:
    """毁证类反派技能：【伪造痕迹】/【销毁证物】/【伪造证物】（「可疑的粉末」等同伪造证物）。

    仅当谋杀现场没有其他非反派角色（evidence_vulnerable）时可发动；每起案件只生效一次伪造。
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
    if not case["evidence_vulnerable"]:
        return reject("falsify", "现场有其他非反派角色，不能毁证")
    if case["falsified"] is not None:
        return reject("falsify", "该案件已被伪造过")
    if skill_left(s, user, FALSY_SKILL[kind]) > 0:
        consume_skill(s, user, FALSY_SKILL[kind])
    elif kind == "伪造证物" and held_item(p, "可疑的粉末", s["phase"]["day"]) is not None:
        consume_item_entry(p, held_item(p, "可疑的粉末", s["phase"]["day"]))
    else:
        return reject("falsify", "没有对应的毁证手段")
    case["falsified"] = kind
    return {"ok": True, "type": "falsify", "public": None, "private": {user: f"已对案件 {case_id} 发动【{kind}】"}}


def move_body(state: dict, user: str, case_id: int, to_room: str) -> dict:
    """【转移尸体】/「裹尸袋」：谋杀成功后把尸体移到另一房间而不被发现。"""
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
    if not case["evidence_vulnerable"]:
        return reject("move_body", "现场有其他非反派角色，不能转移尸体")
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
    return {"ok": True, "type": "move_body", "public": None, "private": {user: f"尸体已转移到{to_room}"}}


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
        result = "强壮" if culprit["physique"] >= 3 else "普通"
    return {
        "ok": True,
        "type": "profile",
        "public": None,
        "private": {user: {"case_id": case_id, "aspect": aspect, "result": result}},
    }


def see_through(state: dict, user: str, target: str, claims: dict) -> dict:
    """【看破伪装】/「测谎仪」：验证目标此前关于 职业/道具/技能/行踪 的声明是否含谎言。

    claims 由使用者从目标发言中提取（须引用对方实际声称过的内容）：
    {"profession", "item", "skill", "visited_rooms": [...], "denied_rooms": [...]}。
    每项裁决为三态：属实 / 谎言 / 无法验证（无效名目、对方未作此类声明、或房间无监控覆盖），
    "无法验证"不计入谎言判定。行踪以真实监控记录（monitoring_true）验证；会客室不设监控。
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
        return room in PUBLIC_ROOMS or is_bedroom(room)

    verdicts: dict[str, str] = {}
    if claims.get("profession"):
        c = str(claims["profession"])
        if c in PROFESSIONS:
            verdicts[f"职业声明「{c}」"] = "属实" if c == tp["profession"] else "谎言"
        else:
            verdicts[f"职业声明「{c}」"] = "无法验证（不是有效职业名）"
    if claims.get("item"):
        c = str(claims["item"])
        if c in ITEMS:
            verdicts[f"道具声明「{c}」"] = "属实" if held_item(tp, c, day) is not None else "谎言"
        else:
            verdicts[f"道具声明「{c}」"] = "无法验证（不是有效道具名）"
    if claims.get("skill"):
        c = str(claims["skill"])
        if c in ALL_SKILLS:
            verdicts[f"技能声明「{c}」"] = "属实" if skill_left(s, target, c) > 0 else "谎言"
        else:
            verdicts[f"技能声明「{c}」"] = "无法验证（不是有效技能名）"
    for key, ph in (("visited_rooms", "去过"), ("denied_rooms", "没去过")):
        for r in claims.get(key) or []:
            r = str(r)
            if not monitorable(r):
                verdicts[f"{ph}{r}"] = "无法验证（该房间无监控覆盖）"
            elif key == "visited_rooms":
                verdicts[f"{ph}{r}"] = "属实" if entered(r) else "谎言"
            else:
                verdicts[f"{ph}{r}"] = "属实" if not entered(r) else "谎言"
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
            "private": {user: "你确认了古堡的密道网：晚间休息时可经密道前往书房/监控室/地下室（监控不留记录；出口房间人数须满足谋杀人数条件，否则当晚无法使用；成功使用一次后密道损毁）"}}


def use_passage(state: dict, user: str, room: str) -> dict:
    """使用密道（v3）：晚间休息时从自己卧室经密道前往书房/监控室/地下室之一。

    - 进入条件与谋杀人数条件相同：出口房间在场非反派 ≤1（黑暗房间 ≤2）；
      不满足则当晚无法使用——密道保留、不暴露（返回 ok=True, failed=True）；
    - 成功：监控不留记录（当晚记录上你一直在卧室），密道损毁（整局至多成功一次）；
    - 进入后可正常行动（含谋杀——杀手限定，由编排器的夜间谋杀窗口提供）。
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
    if room not in PUBLIC_ROOMS:
        return reject("use_passage", "密道只能通往书房/监控室/地下室")
    non_villains = [
        q for q in s["players"]
        if q["alive"] and not is_villain(q) and s["positions"].get(q["name"]) == room
    ]
    limit = 2 if is_dark(s, room) else 1
    if len(non_villains) > limit:
        return {"ok": True, "type": "use_passage", "public": None,
                "private": {user: f"密道出口（{room}）今晚人多眼杂，无法使用——密道保留，未被任何人察觉"},
                "failed": True}
    passage["used"] = True
    s["positions"][user] = room
    s["passage_stealth"].append(user)
    return {"ok": True, "type": "use_passage", "public": None,
            "private": {user: f"你经密道抵达{room}，监控与众人眼中你当晚一直在自己卧室；密道随后损毁"}}


def place_bug(state: dict, user: str, room: str) -> dict:
    """「窃听器」：开局前秘密放置于一个非卧室房间（含会客室——唯一能监视会客室的手段）。"""
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("place_bug", "玩家不存在或已死亡")
    if s["phase"]["stage"] != "开局准备":
        return reject("place_bug", "只能在开局准备阶段放置窃听器")
    if s["bug_room"] is not None:
        return reject("place_bug", "窃听器已放置")
    if held_item(p, "窃听器", 1) is None:
        return reject("place_bug", "你未持有窃听器")
    if room not in FREE_ROOMS:
        return reject("place_bug", "窃听器必须放置在非卧室房间")
    s["bug_room"] = room
    return {"ok": True, "type": "place_bug", "public": None, "private": {user: f"窃听器已放置于{room}"}}


def bug_listen(state: dict, user: str) -> dict:
    """收听窃听器（v3）：晚间休息时间身处自己卧室时使用（交谈内容由编排器下发）。"""
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
        "private": {user: f"你收听了{s['bug_room']}当天自由活动时段的全部交谈（内容由编排器下发）"},
        "room": s["bug_room"],
    }
