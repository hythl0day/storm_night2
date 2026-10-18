"""道具/技能台账与房间行动裁决（rules-6 §4/§5/§9，M3 引擎）。

涵盖：开锁、保险箱、赠送、手电筒、停电/发电机、监控查看与远程入侵、
现场调查（四项全给、结果公开、每案一次）与篡改、看破伪装、封闭空间、窃听器。

通用约定：
- 技能与道具次数用尽即不可用；同名效果的道具是反派道具时，篡改/抹除用途仅反派可用。
- 同时持有对应技能与道具时，优先消耗技能次数（技能不可转让，结果确定）。
- v4.0：删除伪造痕迹/销毁证物/伪造证物/信号干扰/犯罪侧写/转移尸体/裹尸袋/病毒U盘；
  案件篡改统一为【tamper_investigation】（随机改四项中两项，一次定型，确定性随机不可刷）。
"""
from __future__ import annotations

import random

from ..gamedata import ITEM_USES, ITEMS, PROFESSIONS
from .murder import lock_clue_trait
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

INVESTIGATE_ITEMS = ("鲁米诺试剂", "可疑的粉末")  # 调查道具（等同【现场调查】；粉末仅反派可用来篡改）
REMOTE_ITEMS = ("木马程序U盘",)  # 远程入侵道具（查看人人可用；抹除仅反派）


def _wrong_result(case: dict, choice: str):
    """篡改假值取值（确定性）：武器伤痕取选项表下一项，死亡时间取当天另一时段，尸体转移取真。"""
    if choice == "武器伤痕":
        i = WOUND_OPTIONS.index(case["wound"]) if case["wound"] in WOUND_OPTIONS else -1
        return WOUND_OPTIONS[(i + 1) % len(WOUND_OPTIONS)]
    if choice == "死亡时间":
        period = case["period"]
        if period.endswith("自由活动"):
            return period[: -len("自由活动")] + "晚间休息"
        return period[: -len("晚间休息")] + "自由活动"
    if choice == "尸体转移":
        return True
    raise ValueError(choice)


def _clue_value(state: dict, case: dict) -> str:
    """杀手线索真值：按案件锁定的特征项报凶手本人的特征（体格按文字口径）。"""
    trait = lock_clue_trait(state, case)
    cp = get_player(state, case["culprit"])
    if trait == "性别":
        return cp["gender"]
    if trait == "国籍":
        return cp["nationality"]
    return "健壮或魁梧" if cp["physique"] >= 3 else "普通或瘦弱"


def _fake_clue(state: dict, case: dict) -> str:
    """杀手线索假值：同一特征项的错误值。"""
    trait = lock_clue_trait(state, case)
    cp = get_player(state, case["culprit"])
    if trait == "性别":
        return "女" if cp["gender"] == "男" else "男"
    if trait == "国籍":
        return "美国" if cp["nationality"] == "中国" else "中国"
    return "普通或瘦弱" if cp["physique"] >= 3 else "健壮或魁梧"


def _case_results(state: dict, case: dict) -> dict:
    """案件四项调查结论（篡改覆盖已生效）。"""
    res = {
        "死亡时间": case["period"],
        "尸体转移": case["body_moved"],
        "武器伤痕": case["wound"],
        "杀手线索": _clue_value(state, case),
    }
    if case["falsified"]:
        res.update(case["falsified"])
    return res


def investigation_means(state: dict, p: dict) -> tuple[str, str] | None:
    """可用调查手段：优先技能【现场调查】，其次「鲁米诺试剂」「可疑的粉末」。"""
    if skill_left(state, p["name"], "现场调查") > 0:
        return ("skill", "现场调查")
    day = state["phase"]["day"]
    for item in INVESTIGATE_ITEMS:
        if held_item(p, item, day) is not None:
            return ("item", item)
    return None


def _consume_means(state: dict, p: dict, means: tuple[str, str]) -> None:
    if means[0] == "skill":
        consume_skill(state, p["name"], means[1])
    else:
        consume_item_entry(p, held_item(p, means[1], state["phase"]["day"]))


def _remote_means(state: dict, p: dict) -> tuple[str, str] | None:
    """可用远程入侵手段：优先技能【远程入侵】，其次「木马程序U盘」。"""
    if skill_left(state, p["name"], "远程入侵") > 0:
        return ("skill", "远程入侵")
    day = state["phase"]["day"]
    for item in REMOTE_ITEMS:
        if held_item(p, item, day) is not None:
            return ("item", item)
    return None


def _roommates(state: dict, room: str, exclude: str) -> list[str]:
    return [
        p["name"]
        for p in state["players"]
        if p["alive"] and p["name"] != exclude and state["positions"].get(p["name"]) == room
    ]


def unlock(state: dict, user: str, room: str) -> dict:
    """打开一扇上锁的房门（手段：职业技能【暴力破解】/「万能钥匙」/「螺丝起子」）。

    门锁口径：功能房（书房/监控室/地下室）的门与保险箱打开后永久开启；
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


def remote_view_monitoring(state: dict, user: str) -> dict:
    """【远程入侵】/「木马程序U盘」（v4.0）：任意行动阶段远程查看当前监控记录（等同进监控室查看）。

    查看与抹除共用一次次数（查看或抹除任选其一，用掉即消耗）。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("remote_view_monitoring", "玩家不存在或已死亡")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("remote_view_monitoring", "当前阶段不能远程入侵")
    means = _remote_means(s, p)
    if means is None:
        return reject("remote_view_monitoring", "没有【远程入侵】或「木马程序U盘」")
    _consume_means(s, p, means)
    return {"ok": True, "type": "remote_view_monitoring", "public": None, "private": {user: s["monitoring"]}}


def remote_tamper_monitoring(state: dict, user: str, period: str) -> dict:
    """【远程入侵】/「木马程序U盘」反派用途（v4.0）：远程抹除某天某时段的全部监控记录。

    period 形如 "第2天自由活动" / "第2天晚间休息"；抹除永不恢复——记录清空且宣读时注明缺失
    （等同停电空白：记入 blackout_periods；【看破伪装】对该时段判"无法验证"）。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("remote_tamper_monitoring", "玩家不存在或已死亡")
    if not is_villain(p):
        return reject("remote_tamper_monitoring", "只有反派能抹除监控记录")
    if s["phase"]["stage"] not in ACTION_STAGES:
        return reject("remote_tamper_monitoring", "当前阶段不能远程入侵")
    means = _remote_means(s, p)
    if means is None:
        return reject("remote_tamper_monitoring", "没有【远程入侵】或「木马程序U盘」")
    if not isinstance(period, str) or not (period.endswith("自由活动") or period.endswith("晚间休息")):
        return reject("remote_tamper_monitoring", "时段须形如「第N天自由活动」或「第N天晚间休息」")
    _consume_means(s, p, means)
    s["monitoring"].pop(period, None)  # 抹除永不恢复（host 侧 monitoring_true 仍留底）
    mark_blackout_period(s, period)
    return {"ok": True, "type": "remote_tamper_monitoring", "public": None,
            "private": {user: f"已远程抹除 {period} 的全部监控记录（宣读时将注明该时段缺失）"}}


def investigate(state: dict, user: str, case_id: int) -> dict:
    """【现场调查】/「鲁米诺试剂」/「可疑的粉末」（v4.0）：清晨案件调查阶段一次获知全部四项结论——
    死亡时间 / 尸体是否被移动过 / 武器伤痕 / 杀手线索；结果以调查者本人当众宣读的口径全员公开（含调查者姓名）。

    每起案件只被调查一次：首个完成的调查即定型（investigated），此后任何人不能再查本案。
    被篡改过的案件按篡改值公告（announce 的内容即结算结果，调用方无需区分真假）。
    """
    s = state
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("investigate", "玩家不存在或已死亡")
    if s["phase"]["stage"] != "案件调查":
        return reject("investigate", "只能在案件调查阶段使用")
    case = next((c for c in s["cases"] if c["id"] == case_id), None)
    if case is None:
        return reject("investigate", "案件不存在")
    if case["investigated"]:
        return reject("investigate", "该案件已有定论（每起案件只被调查一次）")
    means = investigation_means(s, p)
    if means is None:
        return reject("investigate", "没有可用的调查手段（【现场调查】/「鲁米诺试剂」/「可疑的粉末」）")
    _consume_means(s, p, means)
    case["investigated"] = True
    res = _case_results(s, case)
    trait = lock_clue_trait(s, case)
    ann = (f"{user} 当众宣读了自己的调查结论（{case['victim']} 案 · {case['room']}）："
           f"「死亡时间 = {res['死亡时间']}；尸体{'被移动过' if res['尸体转移'] else '未被移动'}；"
           f"武器伤痕 = {res['武器伤痕']}；杀手线索：{trait} = {res['杀手线索']}」")
    return {
        "ok": True,
        "type": "investigate",
        "public": {"announcements": [ann]},
        "private": {user: {"case": case["victim"], "room": case["room"], "results": res}},
    }


def tamper_investigation(state: dict, user: str, case_id: int) -> dict:
    """篡改案件调查（v4.0）：反派消耗一次调查次数，随机篡改四项结论中的两项（私下告知改了哪两项、
    改成什么），此后本案的调查结果永久为假。同一案件篡改一次即定型（不可重复篡改刷随机数）；
    篡改行为不会被任何人发现（毁证窗口的亮房可见性除外，见下）。

    注意：篡改不封闭调查——本案此后仍可被正常调查一次（公告的即为假值）。篡改抢先的意义正在于此：
    反派在清晨队列中先于平民调查者行动时篡改，随后平民的自动调查会把假结论当成真的公告出去。

    两处窗口：
    ① 毁证窗口——案发当天（自由活动/晚间休息），身处案发现场且现场无其他非反派（evidence_vulnerable）；
       亮房中此行为会被同房者看见（沿用毁证可见性口径）；
    ② 清晨调查队列——案件调查阶段，本案已公告、尚未被任何人调查且未被篡改。
    """
    s = state
    day, stage = s["phase"]["day"], s["phase"]["stage"]
    p = get_player(s, user)
    if p is None or not p["alive"]:
        return reject("tamper_investigation", "玩家不存在或已死亡")
    if not is_villain(p):
        return reject("tamper_investigation", "只有反派能篡改案件调查")
    case = next((c for c in s["cases"] if c["id"] == case_id), None)
    if case is None:
        return reject("tamper_investigation", "案件不存在")
    if case["falsified"] is not None:
        return reject("tamper_investigation", "该案件已被篡改过（一次即定型）")
    if case["investigated"]:
        return reject("tamper_investigation", "该案件已被调查定型，无法再篡改")
    at_scene = (stage in ACTION_STAGES and case["day"] == day
                and case["evidence_vulnerable"] and s["positions"].get(user) == case["room"])
    in_queue = stage == "案件调查" and case["announced"] and case["day"] == day - 1
    if not (at_scene or in_queue):
        return reject("tamper_investigation",
                      "篡改只能在两处窗口进行：案发当天身处案发现场（毁证窗口），或清晨调查队列中轮到你了")
    means = investigation_means(s, p)
    if means is None:
        return reject("tamper_investigation", "没有可用的调查手段（篡改消耗一次调查次数）")
    _consume_means(s, p, means)
    # 随机篡改四项中的两项：确定性随机（存档seed × 案件号），一次定型、不可刷随机
    rng = random.Random(f"{s.get('seed')}:{case_id}:tamper")
    picks = rng.sample(["死亡时间", "尸体转移", "武器伤痕", "杀手线索"], 2)
    faked = {}
    for item in picks:
        faked[item] = _fake_clue(s, case) if item == "杀手线索" else _wrong_result(case, item)
    case["falsified"] = faked
    detail = "；".join(f"{k} → {v if not isinstance(v, bool) else ('是' if v else '否')}" for k, v in faked.items())
    private = {user: f"你已篡改 {case['victim']} 的案件调查：改动两项——{detail}。"
                     f"此后本案的调查结果永久为假（无人会察觉篡改本身）。"}
    if at_scene and not is_dark(s, case["room"]):
        for w in _roommates(s, case["room"], user):  # 毁证可见性口径：亮房中同房者看见此行为
            private[w] = f"{user} 在{case['room']}对案发现场做了些什么（疑似篡改证物）"
    return {"ok": True, "type": "tamper_investigation", "public": None, "private": private}


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


def see_through(state: dict, user: str, target: str, claims: dict) -> dict:
    """【看破伪装】/「测谎仪」：验证目标此前关于 职业/道具/技能/行踪 的声明是否含谎言。

    claims 由使用者从目标发言中提取（须引用对方实际声称过的内容）：
    {"profession", "item", "skill", "visited_rooms": [...], "denied_rooms": [...]}。
    每项裁决为三态：属实 / 谎言 / 无法验证（无效名目、对方未作此类声明、或房间无监控覆盖），
    "无法验证"不计入谎言判定。行踪以真实监控记录（monitoring_true）验证（v3.5 起监控覆盖所有房间门口，含会客室）。
    v3.12（game/20 复盘）：存在监控空白（停电或被远程抹除的时段）时，行踪声称的"无记录"不构成谎言证据，
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

    # v3.12（game/20 复盘）：存在监控空白的对局，"无记录"不能作为行踪谎言的证据——
    # 与清晨宣读"缺失不代表无人进入"同口径；有记录为证的声称不受空白影响。
    # v4.0：空白来源含停电与【远程入侵】/「木马程序U盘」的整段抹除（均记入 blackout_periods）
    gap = bool(s.get("blackout_periods")) or bool(s.get("blackout_days"))

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
                    verdicts[f"{ph}{r}"] = "无法验证（监控存在空白时段，不能排除其在空白期进入）"
                else:
                    verdicts[f"{ph}{r}"] = "谎言"
            else:
                if got:
                    verdicts[f"{ph}{r}"] = "谎言"
                elif gap:
                    verdicts[f"{ph}{r}"] = "无法验证（监控存在空白时段，不能确认其从未进入）"
                else:
                    verdicts[f"{ph}{r}"] = "属实"
    if not wellformed:
        return reject("see_through",
                      '没有可验证的声明：职业/道具/技能/房间须填规则书原名（如 "黑客"、"刀"、"书房"），'
                      "不要填整句描述——本次不消耗次数，请重新声明")
    _consume_means(s, p, means)
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
