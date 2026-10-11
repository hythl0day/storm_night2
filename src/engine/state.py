"""状态装载、扩展字段与共享助手（M3 引擎）。

Engine 在 deal.py 产出的 state 之上扩展以下字段（ensure() 幂等初始化，随 state 一并落盘）：

- locks: {房间: bool}                   门锁（True=上锁）；卧室键为 "<名字>的卧室"；初始地下室与各卧室上锁
- safe_open: bool                       书房保险箱是否已被打开
- positions: {名字: 房间}               当前阶段所在房间；白天会议阶段为 "会客室"
- monitoring: {时段: {房间: [名字]}}    公开监控（只记进入；可被【信号干扰】/「病毒U盘」篡改）
- monitoring_true: 同结构               真实进入记录（仅宿主可见；【看破伪装】以它验证行踪）
- skill_uses: {名字: {技能: 剩余}}      技能台账（通用+反派技能都登记；反派技能使用时校验身份）
- cases: [案件]                         死亡案件：伤痕/死亡时间/是否移尸/伪造状态/证物是否可毁/是否已封存
- attempts: [未遂]                      谋杀未遂（含"夜袭未能进入"，target 为 None）；供公告与无杀倒计时
- last_murder_day: int|None             最近一次谋杀行为（含未遂）的日期；None=倒计时未启动
- winner / win_reason                   胜负结果（"平民"/"反派"/"主神"）
- detective: 名字|None                  当日侦探（仅已有谋杀案件的日子指定，见 rules-6 §7.4）
- stage_murder_used: bool               杀手本阶段谋杀机会是否已用
- power_out: bool                       停电中（修复发电机后恢复）
- flashlight_lit: [房间]                本阶段被「手电筒」照亮的房间
- bedroom_relock: [房间]                被他人开锁的卧室，进入下一时间阶段时恢复上锁
- sealed_room: dict|None                【封闭空间】临时锁死（至下一个自由活动时间失效）
- passage: dict|None                    反派建筑师密道 {used}（夜间使用，出口三功能房任选）
- passage_stealth: [名字]               本阶段经密道行动者（监控不留记录）
- bug_room: 房间|None                   「窃听器」放置位置
- devices: {房间: {setter, active}}     机械师【延时装置】
- sealed_items: [{name,uses_left,from}] 尸体/出局者封存的未使用道具，待投票分配
- gifts_used: [名字]                    当天交换环节已发起过赠送的人
- profession_public: [名字]             职业已公开的人
- last_vote: {day, out}|None            最近一次投票结果（武装指认的前提判断）
- rooms_entered: 时段|None              本时段自由活动是否已完成选房结算（断点续跑与停电窗口用）
- night_assigned: 时段|None             本时段晚间休息是否已完成归宿结算

玩家道具从 deal 的 item/item_uses_left 扩展为列表：
  player["items"] = [{"name", "uses_left", "available_day"}]
  杀手可缴获多件道具；available_day 之前不可用（投票分配次日可用）；转手不改变剩余次数。
"""
from __future__ import annotations

import json
from pathlib import Path

PUBLIC_ROOMS = ["书房", "监控室", "地下室"]  # 功能房：监控覆盖，夜间开放（每房限 1 人）
HALL = "会客室"  # 大厅：白天会议场所，自由活动同样开放；永不黑暗、不设监控、不可封闭、不限人数；夜间不开放
FREE_ROOMS = [HALL] + PUBLIC_ROOMS  # 自由活动合法去向（白天卧室清场，见 rules-6 §5）
ENV_WEAPON = {"书房": "雕像", "地下室": "电线"}
ROOM_CAPACITY = {"书房": 3, "监控室": 3, "地下室": 3}  # 会客室不限人数
BEDROOM_CAPACITY = 2
MEETING_STAGES = ("案件调查", "公共发言", "投票")
ACTION_STAGES = ("自由活动", "晚间休息")

WOUND_LABEL = {
    "空手": "空手",
    "消音手枪": "枪",
    "刀": "利器",
    "棒球棍": "钝器",
    "毒药": "毒或药物",
    "雕像": "雕像",
    "电线": "电线",
    "延时装置": "机关",
    "舞台魔术": "空手",
}

GENERAL_SKILLS = {
    "医师": {"现场调查": 2},
    "化学家": {"现场调查": 2},
    "运动员": {"敏捷闪避": 1},
    "黑客": {"破门而入": 1},
    "犯罪侧写师": {"犯罪侧写": 1},
    "心理医生": {"看破伪装": 2},
    "建筑师": {"封闭空间": 1},
    "机械师": {"破门而入": 1},
    "魔术师": {"看破伪装": 2},
}
VILLAIN_SKILLS = {
    "医师": {"伪造痕迹": 1},
    "化学家": {"销毁证物": 1},
    "运动员": {"转移尸体": 1, "暴力破解": 1},
    "黑客": {"信号干扰": 1, "暴力破解": 1},
    "犯罪侧写师": {"伪造证物": 1},
    "心理医生": {"记忆植入": 1},
    "建筑师": {"建造密道": 1},
    "机械师": {"延时装置": 1},
    "魔术师": {"舞台魔术": 1},
}


def bedroom_of(name: str) -> str:
    return f"{name}的卧室"


def is_bedroom(room: str) -> bool:
    return room.endswith("的卧室")


def bedroom_owner(room: str) -> str | None:
    return room[:-3] if is_bedroom(room) else None


def load_state(path: str | Path) -> dict:
    """从 JSON 文件装载 state（唯一的文件 I/O 辅助）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_state(state: dict, path: str | Path) -> None:
    """把 state 落盘为 JSON。"""
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure(state: dict) -> dict:
    """幂等补齐引擎扩展字段；同时把 deal 的单道具扩展为 items 列表。"""
    s = state
    s.setdefault("safe_open", False)
    s.setdefault("positions", {})
    s.setdefault("monitoring", {})
    s.setdefault("monitoring_true", {})
    s.setdefault("skill_uses", {})
    s.setdefault("cases", [])
    s.setdefault("attempts", [])
    s.setdefault("last_murder_day", None)
    s.setdefault("winner", None)
    s.setdefault("win_reason", None)
    s.setdefault("detective", None)
    s.setdefault("stage_murder_used", False)
    s.setdefault("power_out", False)
    s.setdefault("flashlight_lit", [])
    s.setdefault("bedroom_relock", [])
    s.setdefault("sealed_room", None)
    s.setdefault("passage", None)
    s.setdefault("passage_stealth", [])
    s.setdefault("bug_room", None)
    s.setdefault("devices", {})
    s.setdefault("sealed_items", [])
    s.setdefault("gifts_used", [])
    s.setdefault("profession_public", [])
    s.setdefault("last_vote", None)
    s.setdefault("rooms_entered", None)
    s.setdefault("night_assigned", None)
    locks = s.setdefault("locks", {})
    for r in PUBLIC_ROOMS:
        locks.setdefault(r, r == "地下室")
    for p in s["players"]:
        locks.setdefault(bedroom_of(p["name"]), True)
        if "items" not in p:
            p["items"] = [{"name": p["item"], "uses_left": p["item_uses_left"], "available_day": 1}]
        if p["name"] not in s["skill_uses"]:
            skills = dict(GENERAL_SKILLS.get(p["profession"], {}))
            skills.update(VILLAIN_SKILLS.get(p["profession"], {}))
            s["skill_uses"][p["name"]] = skills
    return s


def reject(etype: str, error: str) -> dict:
    """非法动作驳回：ok=False，不改变状态，驳回事实不公开。"""
    return {"ok": False, "type": etype, "error": error, "public": None, "private": {}}


def get_player(state: dict, name: str) -> dict | None:
    for p in state["players"]:
        if p["name"] == name:
            return p
    return None


def is_villain(p: dict) -> bool:
    return p["identity"] in ("杀手", "帮凶")


def alive_players(state: dict) -> list[dict]:
    return [p for p in state["players"] if p["alive"]]


def current_period(state: dict) -> str:
    ph = state["phase"]
    return f"第{ph['day']}天{ph['stage']}"


def is_dark(state: dict, room: str) -> bool:
    """黑暗判定：地下室永久黑暗；停电期间除卧室外全部黑暗；手电筒照亮的房间本次除外。"""
    if room in state["flashlight_lit"]:
        return False
    if is_bedroom(room) or room == "会客室":
        return False
    if room == "地下室":
        return True
    return bool(state["power_out"])


def record_entry(state: dict, name: str, room: str) -> None:
    """监控只记录"某时间段谁进入了某场所"，不记离开；经密道者不留记录；会客室不设监控。"""
    if name in state["passage_stealth"] or room == HALL:
        return
    period = current_period(state)
    state["monitoring"].setdefault(period, {}).setdefault(room, []).append(name)
    state["monitoring_true"].setdefault(period, {}).setdefault(room, []).append(name)


def skill_left(state: dict, name: str, skill: str) -> int:
    return state["skill_uses"].get(name, {}).get(skill, 0)


def consume_skill(state: dict, name: str, skill: str) -> None:
    state["skill_uses"][name][skill] -= 1


def held_item(p: dict, name: str, day: int) -> dict | None:
    for e in p["items"]:
        if e["name"] == name and e["uses_left"] > 0 and e["available_day"] <= day:
            return e
    return None


def held_item_names(p: dict, day: int) -> set[str]:
    return {e["name"] for e in p["items"] if e["uses_left"] > 0 and e["available_day"] <= day}


def consume_item_entry(p: dict, entry: dict) -> None:
    """消耗一次道具次数；次数归零即销毁（从持有列表移除）。"""
    entry["uses_left"] -= 1
    if entry["uses_left"] <= 0:
        p["items"].remove(entry)


def grant_item(p: dict, name: str, uses_left: int, available_day: int) -> None:
    p["items"].append({"name": name, "uses_left": uses_left, "available_day": available_day})


def unlock_means(state: dict, name: str) -> str | None:
    """可用开锁手段：优先职业技能【破门而入】，其次道具「万能钥匙」。"""
    p = get_player(state, name)
    if p is None or not p["alive"]:
        return None
    if skill_left(state, name, "破门而入") > 0:
        return "破门而入"
    if held_item(p, "万能钥匙", state["phase"]["day"]) is not None:
        return "万能钥匙"
    return None


def consume_unlock_means(state: dict, name: str, means: str) -> None:
    if means == "破门而入":
        consume_skill(state, name, "破门而入")
    else:
        consume_item_entry(get_player(state, name), held_item(get_player(state, name), "万能钥匙", state["phase"]["day"]))


def check_winner(state: dict) -> str | None:
    """通用胜负检查：反派全灭→平民胜；平民全灭→反派胜。其余胜负在各自裁决点检查。"""
    if state["winner"]:
        return state["winner"]
    civ = [p for p in state["players"] if p["alive"] and p["identity"] == "平民"]
    vil = [p for p in state["players"] if p["alive"] and is_villain(p)]
    if not vil:
        state["winner"] = "平民"
        state["win_reason"] = "杀手与帮凶均已出局"
    elif not civ:
        state["winner"] = "反派"
        state["win_reason"] = "所有平民死亡或被投票出局"
    return state["winner"]
