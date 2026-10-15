"""投票与出局裁决（rules-6 §7.3），含记忆植入、武装指认与侦探竞选/警徽遗嘱（M3 引擎）。

- 第 2 天起每天投票；得票超过存活人数一半者出局；平票或无人过半则无人出局。
- v3.3：侦探（第 1 天竞选产生）一票计 1.5 票；侦探出局时警徽按遗嘱传承或销毁，不补选。
- v3.5：侦探当选时获得桌上凶器「左轮手枪」（侦探佩枪）；有侦探时仅侦探可武装指认。
- 出局者公布身份与职业；出局者是杀手时帮凶私下继任杀手。
- 反派全部出局 → 平民获胜；平民全灭 → 反派获胜（v3.14：删去"只剩 1 平民直接判负"，残局自然打完）。
- 【记忆植入】：反派心理医生强迫一名玩家改投指定对象；公告只说"票数被更改过"。
- 武装指认（第 2 天首次投票起，v3.21 淘汰制）：投票结算后、出局执行前发动——① 未产生出局者的投票后；
  ② 投票将导致最后一名平民出局时，该平民在出局执行前（背水一战）。
  正确→被指认的杀手出局（帮凶私下继任，游戏继续；帮凶已不在则反派全灭平民胜）；
  指认为帮凶/平民→指认者出局。指认不消耗武器；临时取用桌上凶器指认后放回（无论对错）。
  有侦探时仅侦探（须持有武器）；无侦探时持武器平民可指认，桌上凶器仍在时平民也可临时取用指认。
- v3.5 遗嘱制：接受的道具当天即可用；未指定/被拒收的道具不销毁，放回会客室桌上。
"""
from __future__ import annotations

from ..gamedata import ARMED_WEAPONS
from .state import (
    check_winner,
    consume_skill,
    get_player,
    grant_item,
    held_item_names,
    is_villain,
    reject,
    skill_left,
)

DETECTIVE_VOTE_WEIGHT = 1.5  # 侦探票权（rules-6 §7.4）


def _seal_player_items(state: dict, p: dict) -> None:
    for e in list(p["items"]):
        if e["uses_left"] > 0:
            state["sealed_items"].append({"name": e["name"], "uses_left": e["uses_left"], "from": p["name"]})
            p["items"].remove(e)


def _execute_out(s: dict, out: str, public: dict, private: dict) -> None:
    """出局结算（投票处决/武装指认处决共用）：死亡、未使用道具封存、身份职业公开；
    出局者是杀手时帮凶私下继任（rules-6 §7.3）。"""
    op = get_player(s, out)
    op["alive"] = False
    if out not in s["profession_public"]:
        s["profession_public"].append(out)
    _seal_player_items(s, op)
    public["identity"] = op["identity"]
    public["profession"] = op["profession"]
    public["announcements"].append(f"{out} 出局。身份：{op['identity']}；职业：{op['profession']}")
    if op["identity"] == "杀手":
        heir = next((p for p in s["players"] if p["alive"] and p["identity"] == "帮凶"), None)
        if heir is not None:
            heir["identity"] = "杀手"
            private[heir["name"]] = "杀手已出局，你立即继任为杀手（此消息不公开）"


def execute_pending_out(state: dict) -> dict:
    """v3.21：执行被 defer 的出局结算（最后平民的结算前武装指认窗口关闭——放弃或被驳回后）。"""
    s = state
    out = s.pop("pending_out", None)
    if out is None:
        return reject("execute_pending_out", "没有待执行的出局结算")
    public: dict = {"announcements": []}
    private: dict = {}
    _execute_out(s, out, public, private)
    check_winner(s)
    ev = {"ok": True, "type": "execute_out", "public": public, "private": private}
    if s["winner"]:
        ev["winner"] = s["winner"]
        ev["win_reason"] = s["win_reason"]
    return ev


def resolve_vote(state: dict, votes: dict[str, str | None], implant: dict | None = None,
                 defer_out: bool = False) -> dict:
    """投票结算。votes: {投票人: 目标或 None(弃权)}，缺席者按弃权计。

    implant: {"by": 反派心理医生, "voter": 被强迫者, "target": 指定目标}（【记忆植入】）。
    defer_out=True：只计票不执行出局（out 写入 pending_out）——v3.21 最后一名平民的
    结算前武装指认窗口用；窗口关闭后由 execute_pending_out / armed_accusation 完成结算。
    """
    s = state
    if s["winner"]:
        return reject("vote", "游戏已结束")
    day = s["phase"]["day"]
    if s["phase"]["stage"] != "投票":
        return reject("vote", "当前不是投票阶段")
    if day < 2:
        return reject("vote", "第 1 天没有投票")
    alive = [p["name"] for p in s["players"] if p["alive"]]
    for v, t in votes.items():
        if v not in alive:
            return reject("vote", f"{v} 不是存活玩家")
        if t is not None and t not in alive:
            return reject("vote", f"投票目标 {t} 不是存活玩家")
    ballots = {n: votes.get(n) for n in alive}
    implanted = False
    if implant is not None:
        by, voter, target = implant.get("by"), implant.get("voter"), implant.get("target")
        bp = get_player(s, by) if by else None
        if bp is None or not bp["alive"] or not is_villain(bp) or bp["profession"] != "心理医生":
            return reject("vote", "只有反派心理医生能使用记忆植入")
        if skill_left(s, by, "记忆植入") <= 0:
            return reject("vote", "记忆植入次数已用完")
        if voter not in alive or target not in alive:
            return reject("vote", "记忆植入的对象必须是存活玩家")
        consume_skill(s, by, "记忆植入")
        ballots[voter] = target
        implanted = True
    det = s.get("detective")
    tally: dict[str, float] = {}
    for v, t in ballots.items():
        if t is not None:
            tally[t] = tally.get(t, 0) + (DETECTIVE_VOTE_WEIGHT if v == det else 1)
    out = None
    if tally:
        top = max(tally.values())
        cands = [t for t, c in tally.items() if c == top]
        if len(cands) == 1 and top > len(alive) / 2:
            out = cands[0]
    public: dict = {"tally": tally, "out": out, "announcements": []}
    if implanted:
        public["announcements"].append("票数被更改过")
    private: dict = {}
    if out is not None:
        if defer_out:
            s["pending_out"] = out  # v3.21：出局结算待执行——最后平民可先武装指认
            public["deferred"] = True
        else:
            _execute_out(s, out, public, private)
    s["last_vote"] = {"day": day, "out": out}
    check_winner(s)  # v3.14：删去"只剩 1 平民反派直接胜"——残局由下一轮投票自然了结，死锁由主神兜底
    ev = {"ok": True, "type": "vote", "public": public, "private": private}
    if s["winner"]:
        ev["winner"] = s["winner"]
        ev["win_reason"] = s["win_reason"]
    return ev


def armed_accusation(state: dict, accuser: str, suspect: str) -> dict:
    """武装指认（rules-6 §7.3，v3.21 淘汰制）：第 2 天首次投票起，投票结算后、出局执行前发动。

    收益：指对 → 被指认的**杀手出局**（遗言/身份公布/遗嘱同投票出局；帮凶私下继任，游戏继续——
    帮凶已不在则反派全灭、平民获胜）；指错（帮凶/平民）→ **指认者出局**（公开可推知所指非杀手）。
    发动时机：① 一次投票未产生出局者时（last_vote.out 为空）；
    ② 投票将导致**最后一名平民**出局时——该平民在出局执行前（pending_out 为本人且平民仅剩 1 人）。
    资格（v3.5）：仅限平民；有侦探时仅侦探本人（且须持有武器——枪/刀/棍）；
    无侦探时，持有武器的平民可指认；桌上仍有凶器时任何平民可临时取用指认
    （临时取用不进入道具栏，指认后凶器仍在桌上——无论对错）。指认不消耗武器。
    """
    s = state
    if s["winner"]:
        return reject("armed_accusation", "游戏已结束")
    day = s["phase"]["day"]
    if s["phase"]["stage"] != "投票":
        return reject("armed_accusation", "只能在投票阶段指认")
    lv = s["last_vote"]
    if not lv or lv["day"] != day:
        return reject("armed_accusation", "当前不在投票结算后的指认窗口")
    if s.get("accusation_fired_day") == day:
        return reject("armed_accusation", "本轮投票已有一次指认（每次投票至多一次）")
    pending = s.get("pending_out")
    if pending is None and lv["out"] is not None:
        return reject("armed_accusation", "本轮投票已产生出局者（仅最后一名平民可在结算前指认）")
    if pending is not None and accuser != pending:
        return reject("armed_accusation", "只有即将出局的最后一名平民能在结算前指认")
    ap = get_player(s, accuser)
    if ap is None or not ap["alive"] or ap["identity"] != "平民":
        return reject("armed_accusation", "只有存活平民能武装指认")
    if pending is not None and sum(1 for p in s["players"] if p["identity"] == "平民") != 1:
        return reject("armed_accusation", "结算前指认仅限最后一名平民")
    det = s.get("detective")
    temp_grab = False
    if det is not None:
        if accuser != det:
            return reject("armed_accusation", "有侦探时，仅侦探本人可武装指认")
        if not (held_item_names(ap, day) & set(ARMED_WEAPONS)):
            return reject("armed_accusation", "侦探须持有武器（左轮手枪/刀/棒球棍）才能武装指认")
    else:
        if held_item_names(ap, day) & set(ARMED_WEAPONS):
            pass
        elif any(e["name"] in ARMED_WEAPONS for e in s["table_items"]):
            temp_grab = True  # 临时取用桌上凶器
        else:
            return reject("armed_accusation", "指认需要持有武器（左轮手枪/刀/棒球棍），或桌上凶器仍在会客室桌上")
    sp = get_player(s, suspect)
    if sp is None or not sp["alive"]:
        return reject("armed_accusation", "指认对象必须是存活玩家")
    grab_note = f"{accuser} 拿起会客室桌上的凶器，" if temp_grab else ""
    table_note = "（临时取用的凶器已放回桌上）" if temp_grab else ""
    s["accusation_fired_day"] = day  # 每次投票至多一次指认（引擎级守卫，防续跑重复）
    if sp["identity"] == "杀手":
        s.pop("pending_out", None)  # 触发②：处决对象替换为杀手——最后平民获救
        public = {"out": suspect, "announcements": [
            f"{grab_note}{accuser} 武装指认 {suspect} 为杀手——指认正确{table_note}"]}
        private: dict = {}
        _execute_out(s, suspect, public, private)
        check_winner(s)  # 帮凶继任则游戏继续；帮凶已不在则反派全灭、平民胜
        ev = {"ok": True, "type": "armed_accusation", "public": public, "private": private}
        if s["winner"]:
            ev["winner"] = s["winner"]
            ev["win_reason"] = s["win_reason"]
        return ev
    s.pop("pending_out", None)  # 触发②且指错：按原处决——指认者（最后平民）出局
    public = {"out": accuser, "announcements": [
        f"{grab_note}{accuser} 武装指认 {suspect} 为杀手——指认错误"
        + ("（桌上凶器仍在会客室桌上）" if temp_grab else "")]}
    _execute_out(s, accuser, public, {})
    check_winner(s)
    ev = {"ok": True, "type": "armed_accusation", "public": public, "private": {}}
    if s["winner"]:
        ev["winner"] = s["winner"]
        ev["win_reason"] = s["win_reason"]
    return ev


def bequest(state: dict, owner: str, mapping: dict, replies: dict) -> dict:
    """遗嘱落账（rules-6 §7.3，v3.5）：出局者/死者的封存道具由本人逐件指定归属，接收人可拒绝。

    mapping: {道具名: 接收人名}（本人指定）；replies: {接收人名: {道具名: bool}}（拒绝或无回应 → 见下）。
    接受的道具**当天即可用**；未指定归属或被拒收的道具**不销毁**，放回会客室桌上
    （桌上物品夜间可被独处会客室者取走，见 ledger.take_table_item）。
    公告公开道具名、来源与去向（信任信号即推理素材）。
    """
    s = state
    day = s["phase"]["day"]
    owned = [e for e in s["sealed_items"] if e.get("from") == owner]
    announcements: list[str] = []
    for e in list(owned):
        name = e["name"]
        to = mapping.get(name)
        tp = get_player(s, to) if to else None
        s["sealed_items"].remove(e)
        if tp is not None and tp["alive"] and bool(replies.get(to, {}).get(name)):
            grant_item(tp, name, e["uses_left"], day)
            announcements.append(f"遗嘱：「{name}」（{owner} 遗留）→ {to}（当天即可用）")
        else:
            s["table_items"].append({"name": name, "uses_left": e["uses_left"]})
            if tp is not None and tp["alive"]:
                announcements.append(f"遗嘱：「{name}」（{owner} 遗留）被 {to} 拒收，放回会客室桌上")
            else:
                announcements.append(f"遗嘱：「{name}」（{owner} 遗留）未指定归属，放回会客室桌上")
    return {"ok": True, "type": "bequest", "public": {"announcements": announcements}, "private": {}}


def elect_detective(state: dict, ran: list[str], stay: list[str], votes: dict[str, str | None]) -> dict:
    """侦探竞选裁决（v3.3 rules-6 §7.4）：第 1 天"公共发言"时段改为竞选。

    ran: 声明参选者（按行动顺序）；stay: 密封退选后仍留任者（⊆ ran）；
    votes: {投票人: 留任候选人或 None(弃权)}——自始未参选的存活者才有投票权（退选者无）。
    无人报名 / 全员退选 / 全员参选（无合格投票人）/ 平票 → 本局不设侦探，且不补选。
    v3.5：侦探当选时获得桌上凶器「左轮手枪」（侦探佩枪，公开公告）。
    """
    s = state
    if s["winner"]:
        return reject("elect_detective", "游戏已结束")
    if s["phase"]["day"] != 1 or s["phase"]["stage"] != "公共发言":
        return reject("elect_detective", "侦探竞选只在第 1 天竞选时段进行")
    if s.get("detective_election_done"):
        return reject("elect_detective", "侦探竞选已结算")
    alive = [p["name"] for p in s["players"] if p["alive"]]
    if any(n not in alive for n in ran) or any(n not in ran for n in stay):
        return reject("elect_detective", "候选人名单非法")
    s["detective_election_done"] = True

    def done(announcements: list[str]) -> dict:
        return {"ok": True, "type": "elect_detective", "public": {"announcements": announcements}, "private": {}}

    def grant_gun(name: str) -> str:
        """侦探佩枪：桌上凶器归侦探持有（v3.5）。"""
        gun = next((e for e in s["table_items"] if e["name"] in ARMED_WEAPONS), None)
        if gun is None:
            return ""
        s["table_items"].remove(gun)
        dp = get_player(s, name)
        grant_item(dp, gun["name"], gun["uses_left"], s["phase"]["day"])
        return f"；侦探佩枪：桌上凶器「{gun['name']}」归 {name} 持有"

    if not ran:
        return done(["侦探竞选：无人报名参选，本局不设侦探"])
    if not stay:
        return done(["侦探竞选：候选人全部退选，本局不设侦探"])
    if len(stay) == 1:
        s["detective"] = stay[0]
        return done([f"侦探竞选：{stay[0]} 为唯一候选人，直接当选侦探"
                     "（公共发言末置发言，投票计 1.5 票，垄断武装指认；出局时警徽按遗嘱传承）"
                     + grant_gun(stay[0])])
    voters = [n for n in alive if n not in ran]
    if not voters:
        return done(["侦探竞选：全员参选，无人持有投票权，本局不设侦探"])
    tally: dict[str, int] = {}
    for v, t in votes.items():
        if v in voters and t in stay:
            tally[t] = tally.get(t, 0) + 1
    if not tally:
        return done(["侦探竞选：无人得票，本局不设侦探"])
    top = max(tally.values())
    cands = [t for t, c in tally.items() if c == top]
    if len(cands) != 1:
        return done(["侦探竞选：平票，本局不设侦探"])
    s["detective"] = cands[0]
    detail = "、".join(f"{t} {c}票" for t, c in sorted(tally.items(), key=lambda kv: -kv[1]))
    return done([f"侦探竞选：{cands[0]} 当选侦探（{detail}）"
                 "（公共发言末置发言，投票计 1.5 票，垄断武装指认；出局时警徽按遗嘱传承）"
                 + grant_gun(cands[0])])


def bequest_badge(state: dict, owner: str, to: str | None, accept: bool) -> dict:
    """警徽遗嘱（v3.3 rules-6 §7.4）：侦探出局时把侦探身份传给指定存活者；拒收/未指定 → 销毁，不补选。"""
    s = state
    if s.get("detective") != owner:
        return reject("bequest_badge", f"{owner} 不是现任侦探")
    tp = get_player(s, to) if to else None
    if tp is not None and tp["alive"] and accept:
        s["detective"] = to
        ann = f"遗嘱：侦探徽章（{owner} 遗留）→ {to}"
    else:
        s["detective"] = None
        if tp is not None and tp["alive"]:
            ann = f"遗嘱：侦探徽章（{owner} 遗留）被 {to} 拒收，销毁"
        else:
            ann = f"遗嘱：侦探徽章（{owner} 遗留）未指定归属，销毁"
    return {"ok": True, "type": "bequest_badge", "public": {"announcements": [ann]}, "private": {}}
