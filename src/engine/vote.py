"""投票与出局裁决（rules-6 §7.3），含记忆植入与武装指认（M3 引擎）。

- 第 2 天起每天投票；得票超过存活人数一半者出局；平票或无人过半则无人出局。
- 出局者公布身份与职业；出局者是杀手时帮凶私下继任杀手。
- 投票后存活平民只剩 1 人 → 反派立即获胜；反派全部出局 → 平民获胜。
- 【记忆植入】：反派心理医生强迫一名玩家改投指定对象；公告只说"票数被更改过"。
- 武装指认（第 4 天起）：持武器平民在一次未产生出局者的投票后指认杀手；
  正确→平民立即获胜；指认为帮凶/平民→指认者出局（揭示身份职业，并复査平民人数）。
"""
from __future__ import annotations

from ..gamedata import WEAPONS
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


def _seal_player_items(state: dict, p: dict) -> None:
    for e in list(p["items"]):
        if e["uses_left"] > 0:
            state["sealed_items"].append({"name": e["name"], "uses_left": e["uses_left"], "from": p["name"]})
            p["items"].remove(e)


def _civilian_count(state: dict) -> int:
    return sum(1 for p in state["players"] if p["alive"] and p["identity"] == "平民")


def resolve_vote(state: dict, votes: dict[str, str | None], implant: dict | None = None) -> dict:
    """投票结算。votes: {投票人: 目标或 None(弃权)}，缺席者按弃权计。

    implant: {"by": 反派心理医生, "voter": 被强迫者, "target": 指定目标}（【记忆植入】）。
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
    tally: dict[str, int] = {}
    for t in ballots.values():
        if t is not None:
            tally[t] = tally.get(t, 0) + 1
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
                private[heir["name"]] = "杀手已被投票出局，你立即继任为杀手（此消息不公开）"
    s["last_vote"] = {"day": day, "out": out}
    check_winner(s)
    if not s["winner"] and _civilian_count(s) <= 1:
        s["winner"] = "反派"
        s["win_reason"] = "投票后存活平民只剩 1 人"
    ev = {"ok": True, "type": "vote", "public": public, "private": private}
    if s["winner"]:
        ev["winner"] = s["winner"]
        ev["win_reason"] = s["win_reason"]
    return ev


def armed_accusation(state: dict, accuser: str, suspect: str) -> dict:
    """武装指认（第 4 天起）：持武器平民在一次未产生出局者的投票后指认杀手。"""
    s = state
    if s["winner"]:
        return reject("armed_accusation", "游戏已结束")
    day = s["phase"]["day"]
    if day < 4:
        return reject("armed_accusation", "第 4 天起才能武装指认")
    if s["phase"]["stage"] != "投票":
        return reject("armed_accusation", "只能在投票后指认")
    lv = s["last_vote"]
    if not lv or lv["day"] != day or lv["out"] is not None:
        return reject("armed_accusation", "只能在未产生出局者的投票后指认")
    ap = get_player(s, accuser)
    if ap is None or not ap["alive"] or ap["identity"] != "平民":
        return reject("armed_accusation", "只有存活平民能武装指认")
    if not (held_item_names(ap, day) & set(WEAPONS)):
        return reject("armed_accusation", "指认者必须持有武器")
    sp = get_player(s, suspect)
    if sp is None or not sp["alive"]:
        return reject("armed_accusation", "指认对象必须是存活玩家")
    if sp["identity"] == "杀手":
        s["winner"] = "平民"
        s["win_reason"] = "武装指认杀手成功"
        return {
            "ok": True,
            "type": "armed_accusation",
            "public": {"announcements": [f"{accuser} 武装指认 {suspect} 为杀手——指认正确，平民获胜"]},
            "private": {},
            "winner": s["winner"],
            "win_reason": s["win_reason"],
        }
    ap["alive"] = False
    if accuser not in s["profession_public"]:
        s["profession_public"].append(accuser)
    _seal_player_items(s, ap)
    public = {"announcements": [
        f"{accuser} 武装指认 {suspect} 为杀手——指认错误，{accuser} 出局。身份：{ap['identity']}；职业：{ap['profession']}"
    ]}
    check_winner(s)
    if not s["winner"] and _civilian_count(s) <= 1:
        s["winner"] = "反派"
        s["win_reason"] = "投票后存活平民只剩 1 人"
    ev = {"ok": True, "type": "armed_accusation", "public": public, "private": {}}
    if s["winner"]:
        ev["winner"] = s["winner"]
        ev["win_reason"] = s["win_reason"]
    return ev


def assign_sealed(state: dict, mapping: dict[str, str | None]) -> dict:
    """封存道具分配：{道具名: 归属玩家或 None(销毁)}。分配到的道具次日起可用。

    侦探平票裁决等讨论过程由编排器处理；本函数只校验并落账。
    """
    s = state
    day = s["phase"]["day"]
    pool = {e["name"]: e for e in s["sealed_items"]}
    for item, target in mapping.items():
        if item not in pool:
            return reject("assign_sealed", f"{item} 不在封存道具中")
        if target is not None:
            tp = get_player(s, target)
            if tp is None or not tp["alive"]:
                return reject("assign_sealed", f"{target} 不是存活玩家")
    granted: dict[str, list[str]] = {}
    destroyed: list[str] = []
    for item, target in mapping.items():
        e = pool[item]
        s["sealed_items"].remove(e)
        if target is None:
            destroyed.append(item)
        else:
            grant_item(get_player(s, target), e["name"], e["uses_left"], day + 1)
            granted.setdefault(target, []).append(item)
    return {
        "ok": True,
        "type": "assign_sealed",
        "public": {"granted": granted, "destroyed": destroyed, "announcements": ["封存道具已分配（次日起可用）"]},
        "private": {},
    }
