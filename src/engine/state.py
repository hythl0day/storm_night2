"""状态装载、扩展字段与共享助手（M3 引擎）。

Engine 在 deal.py 产出的 state 之上扩展以下字段（ensure() 幂等初始化，随 state 一并落盘）：

- locks: {房间: bool}                   门锁（True=上锁）；卧室键为 "<名字>的卧室"；初始监控室与各卧室上锁（v3.2：地下室去锁、监控室上锁）
- safe_open: bool                       书房保险箱是否已被打开
- table_items: [{name,uses_left}]       会客室桌上物品（v3.5：开局为桌上凶器「左轮手枪」；遗嘱未指定/拒收的道具放回桌上）
- table_events: [str]                   待清晨公告的桌上物品变动（夜间被取走等）
- positions: {名字: 房间}               当前阶段所在房间；白天会议阶段为 "会客室"
- monitoring: {时段: {房间: [名字]}}    公开监控（只记进入；可被反派以【远程入侵】/「木马程序U盘」整段抹除；v3.5 起覆盖会客室）
- monitoring_true: 同结构               真实进入记录（仅宿主可见；【看破伪装】以它验证行踪）
- skill_uses: {名字: {技能: 剩余}}      技能台账（反派技能仅反派登记；使用时仍校验身份与次数）
- cases: [案件]                         死亡案件：伤痕/死亡时间/是否移尸/杀手线索（clue_trait）/篡改（falsified={调查项: 假值}）/封存/
                                        announced（清晨已公告）/investigated（已定型：调查或篡改一次即定型）/discovered_by（发现者，host 侧）/reported_by（报案人，公告刊出）
- attempts: [未遂]                      谋杀未遂记录；供公告与无杀倒计时
- poisoned: [{name,day,by}]             缓释毒剂延迟死亡队列（v4.0）：下毒当夜翻页时结算（死于自己卧室，伤痕毒或药物）
- last_murder_day: int|None             最近一次谋杀行为（含未遂与下毒）的日期；None=尚无行为
- day_kill_capped: bool                 本日已有一起成功谋杀（含下毒/装置触发）——置位后一切谋杀/技能杀驳回（v4.0 每日封顶）
- winner / win_reason                   胜负结果（"平民"/"反派"/"主神"）
- detective: 名字|None                  侦探（第 1 天竞选产生；出局时警徽按遗嘱传承或销毁，不补选，见 rules-6 §7.4）
- detective_election_done: bool         第 1 天侦探竞选是否已结算（防重复竞选）
- stage_murder_used: bool               本谋杀窗口全队尝试是否已用（v4.0：每窗口一次尝试，仅放弃/无目标顺延）
- power_out: bool                       停电中（修复发电机后恢复）
- blackout_days: [int]                  拉闸声明日（停电次数统计）
- blackout_periods: [时段]              实际发生监控缺失的时段（含跨日延续、修复当日与远程抹除时段）
- power_restored: 时段|None             最近一次发电机修复时点
- flashlight_lit: [房间]                本阶段被「手电筒」照亮的房间
- bedroom_relock: [房间]                被他人开锁的卧室，进入下一时间阶段时恢复上锁
- sealed_room: dict|None                【封闭空间】临时锁死（至下一个自由活动时间失效）
- passage: dict|None                    反派建筑师密道 {used}（v4.0：密道谋杀成功一次后损毁）
- passage_stealth: [名字]               本阶段经密道行动者（监控不留记录、不上夜宿名单）
- bug_room: 房间|None                   「窃听器」放置位置
- bug_placed: {day,stage}|None          「窃听器」放置时点（此前交谈不追溯；旧档 None 兼容原口径）
- devices: {房间: {setter, active}}     机械师【延时装置】（v4.0：预设后由设置者在谋杀窗口手动启动）
- sealed_items: [{name,uses_left,from}] 尸体/出局者封存的未使用道具，待投票分配
- profession_public: [名字]             职业已公开的人
- last_vote: {day, out}|None            最近一次投票结果（武装指认的前提判断）
- rooms_entered: 时段|None              本时段自由活动是否已完成选房结算（断点续跑与停电窗口用）
- night_assigned: 时段|None             本时段晚间休息是否已完成归宿结算

玩家道具从 deal 的 item/item_uses_left 扩展为列表：
  player["items"] = [{"name", "uses_left", "available_day"}]
  deal 还可用 extra_items 额外配发（如一名杀手固定持有的「万能钥匙」，rules-6 §2），ensure 时并入。
  杀手可缴获多件道具；available_day 之前不可用（v3.5：遗嘱分配当天即可用）；转手不改变剩余次数。

v4.0：身份取值只余 平民/杀手（双杀手制，删除帮凶与继任）；与 v3.x 存档不兼容，旧档仅供查看。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..gamedata import ITEM_USES

PUBLIC_ROOMS = ["书房", "监控室", "地下室"]  # 功能房：监控覆盖，夜间开放（v3.22 起容量与白天一致）
HALL = "会客室"  # 大厅：白天会议场所，自由活动同样开放；永不黑暗、不可封闭、不限人数；桌上物品所在地
FREE_ROOMS = [HALL] + PUBLIC_ROOMS  # 自由活动合法去向（白天卧室清场，见 rules-6 §5）
ENV_WEAPON = {"书房": "雕像", "地下室": "电线"}
ROOM_CAPACITY = {"书房": 3, "监控室": 2, "地下室": 3}  # 会客室不限人数；v3.2 监控室降为 2
BEDROOM_CAPACITY = 2
MEETING_STAGES = ("案件调查", "公共发言", "投票")
ACTION_STAGES = ("自由活动", "晚间休息")

WOUND_LABEL = {
    "空手": "空手",
    "左轮手枪": "枪",
    "刀": "利器",
    "棒球棍": "钝器",
    "不明药剂": "毒或药物",
    "药理学": "毒或药物",
    "缓释毒剂": "毒或药物",
    "强袭": "空手",
    "雕像": "雕像",
    "电线": "电线",
    "延时装置": "机关",
    "舞台魔术": "空手",
}

GENERAL_SKILLS = {
    "医师": {"现场调查": 2},
    "化学家": {"现场调查": 2},
    "运动员": {"敏捷闪避": 1},
    "黑客": {"远程入侵": 1},
    "心理医生": {"看破伪装": 2},
    "建筑师": {"封闭空间": 1},
    "机械师": {"暴力破解": 2},  # v4.0：机械师暴力破解升为 ×2
    "魔术师": {"看破伪装": 2},
}
VILLAIN_SKILLS = {
    "医师": {"药理学": 1},
    "化学家": {"缓释毒剂": 1},
    "运动员": {"强袭": 1},
    "黑客": {"篡改投票": 1},
    "心理医生": {"篡改投票": 1},
    "建筑师": {"建造密道": 1},
    "机械师": {"延时装置": 1},
    "魔术师": {"舞台魔术": 1},
}

ALL_SKILLS = {s for kit in list(GENERAL_SKILLS.values()) + list(VILLAIN_SKILLS.values()) for s in kit}


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
    """幂等补齐引擎扩展字段；同时把 deal 的单道具扩展为 items 列表。

    v4.0 起与 v3.x 存档不兼容（身份取值、技能表、案件结构均变），不再做跨版本迁移。
    """
    s = state
    s.setdefault("safe_open", False)
    s.setdefault("table_items", [])  # v3.5 桌上物品；deal 写入初始桌上凶器
    s.setdefault("table_events", [])
    s.setdefault("positions", {})
    s.setdefault("monitoring", {})
    s.setdefault("monitoring_true", {})
    s.setdefault("skill_uses", {})
    s.setdefault("cases", [])
    s.setdefault("attempts", [])
    s.setdefault("poisoned", [])
    s.setdefault("last_murder_day", None)
    s.setdefault("day_kill_capped", False)
    s.setdefault("winner", None)
    s.setdefault("win_reason", None)
    s.setdefault("detective", None)
    s.setdefault("detective_election_done", False)  # v3.3：第 1 天侦探竞选结算标记
    s.setdefault("stage_murder_used", False)
    s.setdefault("power_out", False)
    s.setdefault("blackout_days", [])  # 拉闸声明日（评分卡统计停电次数用）
    s.setdefault("blackout_periods", [])
    s.setdefault("power_restored", None)       # 发电机修复时点（current_period；None=未修复/无停电）
    s.setdefault("pending_out", None)          # 被 defer 的出局者（v3.21 最后平民结算前指认窗口用）
    s.setdefault("accusation_fired_day", None)  # 本轮投票已发生武装指认的天数（每次投票至多一次）
    s.setdefault("blackout_news", None)      # 待公告的停电时段（选房收尾后向众人公布，rules-6 §7.5）
    s.setdefault("selection_begun", None)    # 选房首个进入已结算的时段（此后停电声明窗口关闭）
    s.setdefault("flashlight_lit", [])
    s.setdefault("bedroom_relock", [])
    s.setdefault("sealed_room", None)
    s.setdefault("passage", None)
    s.setdefault("passage_stealth", [])
    s.setdefault("bug_room", None)
    s.setdefault("bug_placed", None)
    s.setdefault("devices", {})
    s.setdefault("sealed_items", [])
    s.setdefault("profession_public", [])
    s.setdefault("last_vote", None)
    s.setdefault("rooms_entered", None)
    s.setdefault("night_assigned", None)
    locks = s.setdefault("locks", {})
    for r in PUBLIC_ROOMS:
        locks.setdefault(r, r == "监控室")  # v3.2：监控室上锁（首开需开锁手段）；地下室取消门锁
    for p in s["players"]:
        locks.setdefault(bedroom_of(p["name"]), True)
        if "items" not in p:
            p["items"] = [{"name": p["item"], "uses_left": p["item_uses_left"], "available_day": 1}]
            for extra in p.get("extra_items", []):
                p["items"].append({"name": extra, "uses_left": ITEM_USES[extra], "available_day": 1})
        if p["name"] not in s["skill_uses"]:
            # 身份终生不变：反派技能只登记给反派（使用时仍校验身份与次数）
            skills = dict(GENERAL_SKILLS.get(p["profession"], {}))
            if is_villain(p):
                skills.update(VILLAIN_SKILLS.get(p["profession"], {}))
            s["skill_uses"][p["name"]] = skills
        # 平民不登记反派技能（使用时仍校验身份与次数）
        skills = s["skill_uses"].get(p["name"], {})
        if not is_villain(p):
            for vsk in VILLAIN_SKILLS.get(p["profession"], {}):
                skills.pop(vsk, None)
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
    return p["identity"] == "杀手"  # v4.0 双杀手制：反派 = 两名杀手（帮凶已删除）


def alive_players(state: dict) -> list[dict]:
    return [p for p in state["players"] if p["alive"]]


def current_period(state: dict) -> str:
    ph = state["phase"]
    return f"第{ph['day']}天{ph['stage']}"


def mark_blackout_period(state: dict, period: str | None = None) -> None:
    """记录一个实际发生监控缺失的时段（与拉闸声明日的统计口径分开）。"""
    period = period or current_period(state)
    periods = state.setdefault("blackout_periods", [])
    if period not in periods:
        periods.append(period)


def countdown_text(day: int, last_murder_day: int | None) -> str:
    """无杀倒计时公告口径（rules-6 §8）：直接给出进度与触发条件，避免玩家自行换算。
    v4.0：倒计时从第 1 天起算（基准日 0）——第 1、2 天均无谋杀行为，第 3 天清晨平民直接获胜。
    quiet ≥ 2 的清晨即倒计时完成、平民当场获胜（与 core.advance 的判定一致），
    文本宣告完成而非"明早"（修复：完成当天清晨曾仍预告明天）。"""
    last = last_murder_day if last_murder_day is not None else 0
    quiet = max(0, day - 1 - last)
    if quiet == 0:
        return ("无杀倒计时：从第 1 天起算，若连续 2 个完整天没有任何谋杀行为，"
                "下一个清晨平民直接获胜（当前进度 0/2；任何谋杀出手含未遂都会重置）")
    if quiet >= 2:
        return (f"无杀倒计时：已连续 {quiet} 个完整天无谋杀行为（{quiet}/2）——"
                "倒计时完成，平民直接获胜")
    return (f"无杀倒计时：已连续 {quiet} 个完整天无谋杀行为（{quiet}/2）——"
            "今天若仍无谋杀行为，明早平民直接获胜（任何谋杀出手含未遂都会重置）")


def is_dark(state: dict, room: str) -> bool:
    """黑暗判定（v3.22）：地下室永久黑暗；会客室永不黑暗；卧室在晚间休息阶段黑暗（v3.22 起不再
    依赖停电——夜间卧室本就无照明）；停电期间除会客室外全部黑暗；手电筒照亮的房间本次除外。"""
    if room in state["flashlight_lit"]:
        return False
    if room == HALL:
        return False
    if room == "地下室":
        return True
    if state["power_out"]:
        return True
    if is_bedroom(room) and state["phase"]["stage"] == "晚间休息":
        return True
    return False


def door_locked(state: dict, room: str) -> bool:
    """门锁判定（v3.2）：停电期间所有门锁失效（保险箱除外——箱子不是门），供电恢复后回到原锁状态。"""
    if state["power_out"]:
        return False
    return bool(state["locks"].get(room, False))


def record_entry(state: dict, name: str, room: str) -> None:
    """监控只记录"某时间段谁进入了某场所"，不记离开；停电期间监控缺失（不记录）；经密道者不留记录。

    v3.5：监控覆盖所有房间门口（含会客室，rules-6 §9.1）。
    """
    if name in state["passage_stealth"] or state["power_out"]:
        return
    period = current_period(state)
    state["monitoring"].setdefault(period, {}).setdefault(room, []).append(name)
    state["monitoring_true"].setdefault(period, {}).setdefault(room, []).append(name)


def discover_bodies(state: dict) -> dict[str, str]:
    """v3.30 凶案发现：未公告案件的案发房间内有存活玩家 → 该人当场私下发现（记 discovered_by，不记报案）。

    在进入结算（自由活动收尾/晚间归宿）之后调用；返回 {名字: 私密消息} 由调用方
    并入事件 private，经编排器 tell() 送达本人简报。发现只是私人知识——是否成为报案人由本人在
    房间行动中另行选择（ledger.report）；黑暗房间同样触发（黑暗中绊到尸体）。
    """
    msgs: dict[str, str] = {}
    for c in state["cases"]:
        if c.get("announced"):
            continue
        room = c["room"]
        for p in state["players"]:
            if not p["alive"] or p["name"] in c["discovered_by"]:
                continue
            if state["positions"].get(p["name"]) != room:
                continue
            c["discovered_by"].append(p["name"])
            msgs[p["name"]] = (f"你在{room}发现了{c['victim']}的尸体。你可以在本阶段房间行动"
                               f"中选择「报案」成为报案人（将随次日清晨公告公开，同房的人各自做决定）；"
                               f"你也可以保持沉默、照常行动。")
    return msgs


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
    """可用开锁手段（三种）：优先职业技能【暴力破解】，其次道具「万能钥匙」「螺丝起子」。"""
    p = get_player(state, name)
    if p is None or not p["alive"]:
        return None
    if skill_left(state, name, "暴力破解") > 0:
        return "暴力破解"
    day = state["phase"]["day"]
    for item in ("万能钥匙", "螺丝起子"):
        if held_item(p, item, day) is not None:
            return item
    return None


def consume_unlock_means(state: dict, name: str, means: str) -> None:
    if means == "暴力破解":
        consume_skill(state, name, "暴力破解")
    else:
        p = get_player(state, name)
        consume_item_entry(p, held_item(p, means, state["phase"]["day"]))


def check_winner(state: dict) -> str | None:
    """通用胜负检查：反派全灭→平民胜；平民全灭→反派胜。
    v3.14：删去"只剩 1 平民反派直接胜"（v3.13 曾加入，回滚）——规则回归"杀光/票光所有平民"
    一句话；2v1 等票仓锁死残局由下一轮投票自然了结，1v1 决斗（含平民侦探反杀）保留，
    真正死锁由第 6 天终审投票兜底（v3.15）。其余胜负（倒计时/指认/主神）在各自裁决点检查。"""
    if state["winner"]:
        return state["winner"]
    civ = [p for p in state["players"] if p["alive"] and p["identity"] == "平民"]
    vil = [p for p in state["players"] if p["alive"] and is_villain(p)]
    if not vil:
        state["winner"] = "平民"
        state["win_reason"] = "两名杀手均已出局"
    elif not civ:
        state["winner"] = "反派"
        state["win_reason"] = "所有平民死亡或被投票出局"
    return state["winner"]
