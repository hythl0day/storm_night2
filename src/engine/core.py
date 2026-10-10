"""引擎主体：阶段流转、房间分配结算与行动入口（M3）。

阶段流：开局准备 → 公共发言 →（第 2 天起）投票 → 自由活动 → 晚间休息 → 次日清晨案件调查 → …
- 第 1 天没有投票、没有侦探，全天禁止谋杀。
- 每天翻页时行动顺序首位移到最后，新顺序首位成为次日侦探（第 2 天起）。
- 第 4 天起除卧室外所有门锁自动打开。
- 无杀倒计时：首起谋杀行为（含未遂）起算，连续两个完整天无谋杀行为则第三天清晨平民胜。
- 第 5 天晚间休息结束仍无结果 → 主神获胜（全员失败）。
"""
from __future__ import annotations

from . import ledger, murder as murder_mod, vote as vote_mod
from .state import (
    ACTION_STAGES,
    BEDROOM_CAPACITY,
    MEETING_STAGES,
    PUBLIC_ROOMS,
    ROOM_CAPACITY,
    bedroom_of,
    bedroom_owner,
    check_winner,
    consume_unlock_means,
    current_period,
    ensure,
    get_player,
    is_bedroom,
    record_entry,
    reject,
    unlock_means,
)


class Engine:
    """规则引擎：持有并修改 state，所有裁决方法返回结构化事件 dict。

    事件结构：{"ok": bool, "type": str, "public": dict|None, "private": dict, ...}
    - public：可全员广播的信息；驳回（ok=False）时恒为 None（驳回不公开）。
    - private：{玩家名: 仅本人可见的信息}，供编排器按席位分发。
    """

    def __init__(self, state: dict):
        self.state = ensure(state)

    @property
    def winner(self) -> str | None:
        return self.state["winner"]

    def murder(self, killer: str, target: str, weapon: str | None = None, savior: str | None = None) -> dict:
        return murder_mod.murder(self.state, killer, target, weapon, savior)

    def set_device(self, user: str, room: str) -> dict:
        return murder_mod.set_device(self.state, user, room)

    def stage_magic(self, user: str, target: str) -> dict:
        return murder_mod.stage_magic(self.state, user, target)

    def resolve_vote(self, votes: dict[str, str | None], implant: dict | None = None) -> dict:
        return vote_mod.resolve_vote(self.state, votes, implant)

    def armed_accusation(self, accuser: str, suspect: str) -> dict:
        return vote_mod.armed_accusation(self.state, accuser, suspect)

    def assign_sealed(self, mapping: dict[str, str | None]) -> dict:
        return vote_mod.assign_sealed(self.state, mapping)

    def unlock(self, user: str, room: str) -> dict:
        return ledger.unlock(self.state, user, room)

    def open_safe(self, user: str) -> dict:
        return ledger.open_safe(self.state, user)

    def transfer(self, giver: str, receiver: str, item: str, accept: bool = True) -> dict:
        return ledger.transfer(self.state, giver, receiver, item, accept)

    def use_flashlight(self, user: str) -> dict:
        return ledger.use_flashlight(self.state, user)

    def declare_blackout(self, user: str) -> dict:
        return ledger.declare_blackout(self.state, user)

    def fix_generator(self, user: str) -> dict:
        return ledger.fix_generator(self.state, user)

    def view_monitoring(self, user: str, period: str | None = None, room: str | None = None) -> dict:
        return ledger.view_monitoring(self.state, user, period, room)

    def detective_monitoring(self, user: str, period: str, rooms: list[str]) -> dict:
        return ledger.detective_monitoring(self.state, user, period, rooms)

    def tamper_monitoring(self, user: str, period: str, room: str, op: str, name: str, new_name: str | None = None) -> dict:
        return ledger.tamper_monitoring(self.state, user, period, room, op, name, new_name)

    def investigate(self, user: str, case_id: int, choice: str) -> dict:
        return ledger.investigate(self.state, user, case_id, choice)

    def falsify(self, user: str, case_id: int, kind: str) -> dict:
        return ledger.falsify(self.state, user, case_id, kind)

    def move_body(self, user: str, case_id: int, to_room: str) -> dict:
        return ledger.move_body(self.state, user, case_id, to_room)

    def profile(self, user: str, case_id: int, aspect: str) -> dict:
        return ledger.profile(self.state, user, case_id, aspect)

    def see_through(self, user: str, target: str, claims: dict) -> dict:
        return ledger.see_through(self.state, user, target, claims)

    def seal_room(self, user: str, allowed: list[str] | None = None) -> dict:
        return ledger.seal_room(self.state, user, allowed)

    def set_passage(self, user: str, room: str) -> dict:
        return ledger.set_passage(self.state, user, room)

    def use_passage(self, user: str) -> dict:
        return ledger.use_passage(self.state, user)

    def place_bug(self, user: str, room: str) -> dict:
        return ledger.place_bug(self.state, user, room)

    def bug_listen(self, user: str) -> dict:
        return ledger.bug_listen(self.state, user)

    def advance(self) -> dict:
        """推进到下一个时间阶段，返回该次推进产生的公开公告与私密通知。"""
        s = self.state
        if s["winner"]:
            return reject("advance", "游戏已结束")
        day, stage = s["phase"]["day"], s["phase"]["stage"]
        public: dict = {"announcements": []}
        private: dict = {}
        for r in s["bedroom_relock"]:
            s["locks"][r] = True
        s["bedroom_relock"] = []
        if stage in ACTION_STAGES:
            for c in s["cases"]:
                if c["day"] == day and c["stage"] == stage:
                    public["announcements"].append(f"{c['victim']} 死亡，案发房间：{c['room']}")
            for a in s["attempts"]:
                if a["day"] == day and a["stage"] == stage and a["target"]:
                    public["announcements"].append(f"{a['target']} 遇袭但幸存（袭击者与方式未公布）")
        if stage == "开局准备":
            new_day, new_stage = day, "公共发言"
        elif stage == "公共发言":
            new_day, new_stage = day, ("投票" if day >= 2 else "自由活动")
        elif stage == "投票":
            new_day, new_stage = day, "自由活动"
        elif stage == "自由活动":
            new_day, new_stage = day, "晚间休息"
        elif stage == "晚间休息":
            if day >= 5:
                s["winner"] = "主神"
                s["win_reason"] = "第 5 天晚间休息结束仍无阵营获胜，全员失败"
                return {
                    "ok": True,
                    "type": "game_over",
                    "from": {"day": day, "stage": stage},
                    "to": None,
                    "public": public,
                    "private": private,
                    "winner": "主神",
                    "win_reason": s["win_reason"],
                }
            new_day, new_stage = day + 1, "案件调查"
            s["order"] = s["order"][1:] + s["order"][:1]
            s["detective"] = s["order"][0]
            s["gifts_used"] = []
            public["announcements"].append(f"第 {new_day} 天开始。今日行动顺序：{' → '.join(s['order'])}；侦探：{s['detective']}")
            if new_day == 4:
                for r in PUBLIC_ROOMS:
                    s["locks"][r] = False
                public["announcements"].append("第 4 天起，除卧室外所有门锁自动打开")
            if s["last_murder_day"] is not None and s["last_murder_day"] <= new_day - 3:
                s["winner"] = "平民"
                s["win_reason"] = "无杀倒计时完成（连续两个完整天无任何谋杀行为）"
                s["phase"] = {"day": new_day, "stage": new_stage}
                return {
                    "ok": True,
                    "type": "game_over",
                    "from": {"day": day, "stage": stage},
                    "to": dict(s["phase"]),
                    "public": public,
                    "private": private,
                    "winner": "平民",
                    "win_reason": s["win_reason"],
                }
            self._morning_report(new_day, public, private)
        else:
            new_day, new_stage = day, "公共发言"
        s["phase"] = {"day": new_day, "stage": new_stage}
        self._enter_stage(new_stage, public)
        ev = {"ok": True, "type": "advance", "from": {"day": day, "stage": stage}, "to": dict(s["phase"]), "public": public, "private": private}
        if check_winner(s):
            ev["winner"] = s["winner"]
            ev["win_reason"] = s["win_reason"]
        return ev

    def _enter_stage(self, stage: str, public: dict) -> None:
        s = self.state
        if stage in MEETING_STAGES:
            s["positions"] = {p["name"]: "会客室" for p in s["players"] if p["alive"]}
        if stage == "自由活动":
            s["stage_murder_used"] = False
            s["flashlight_lit"] = []
            s["passage_stealth"] = []
            s["sealed_room"] = None
            if s["power_out"]:
                public["announcements"].append("停电中：除卧室外所有房间一片黑暗")
        if stage == "晚间休息":
            s["stage_murder_used"] = any(
                c["day"] == s["phase"]["day"] and c["stage"] == "自由活动" for c in s["cases"]
            )
            s["flashlight_lit"] = []
            s["passage_stealth"] = []

    def _morning_report(self, day: int, public: dict, private: dict) -> None:
        """清晨公告：昨日死亡与未遂、强制开锁、职业公开、尸体道具封存、监控公开宣读。"""
        s = self.state
        yday = day - 1
        cases = [c for c in s["cases"] if c["day"] == yday]
        attempts = [a for a in s["attempts"] if a["day"] == yday and a["target"]]
        for c in cases:
            public["announcements"].append(f"{c['victim']} 死亡，尸体发现于{c['room']}")
            if s["locks"].get(c["room"]):
                s["locks"][c["room"]] = False
                public["announcements"].append(f"案发房间{c['room']}当时上锁，已强制打开")
                if is_bedroom(c["room"]):
                    s["bedroom_relock"].append(c["room"])
            vp = get_player(s, c["victim"])
            if c["victim"] not in s["profession_public"]:
                s["profession_public"].append(c["victim"])
                public["announcements"].append(f"{c['victim']} 的职业公开：{vp['profession']}")
        for a in attempts:
            public["announcements"].append(f"{a['target']} 遇袭但幸存（袭击者与方式未公布）")
        monitoring_public: dict[str, dict[str, list[str]]] = {}
        for c in cases:
            if not c["sealed"]:
                vp = get_player(s, c["victim"])
                for e in list(vp["items"]):
                    if e["uses_left"] > 0:
                        s["sealed_items"].append({"name": e["name"], "uses_left": e["uses_left"], "from": c["victim"]})
                        vp["items"].remove(e)
                c["sealed"] = True
            rooms = {c["room"], bedroom_of(c["victim"])}
            records = s["monitoring"].get(c["period"], {})
            monitoring_public[c["period"]] = {r: records.get(r, []) for r in sorted(rooms)}
        if monitoring_public:
            public["monitoring"] = monitoring_public
            public["announcements"].append("监控公开宣读：案发时间段 ×（案发房间 + 死者卧室）的进入者名单见 monitoring 字段")

    def enter_rooms(self, assignments: dict[str, str], invited: dict[str, str] | None = None) -> dict:
        """自由活动选房间结算（原子：整体非法则驳回且不改状态，由编排器让违规者改选）。

        assignments: {玩家: 房间}，须覆盖全体存活玩家。上锁房间可被有开锁手段者自动开锁进入
        （顺序靠前者优先支付手段）；invited: {受邀者: 邀请者} 允许受邀者进入邀请者的卧室。
        侦探选择的房间立即对所有人公开。结算后自动检查延时装置触发。
        """
        s = self.state
        if s["winner"]:
            return reject("enter_rooms", "游戏已结束")
        if s["phase"]["stage"] != "自由活动":
            return reject("enter_rooms", "当前不是自由活动阶段")
        alive = [p["name"] for p in s["players"] if p["alive"]]
        if set(assignments) != set(alive):
            return reject("enter_rooms", "必须覆盖全体存活玩家")
        valid_rooms = set(PUBLIC_ROOMS) | {bedroom_of(p["name"]) for p in s["players"]}
        for name, room in assignments.items():
            if room not in valid_rooms:
                return reject("enter_rooms", f"无效房间：{room}")
        invited = invited or {}
        counts: dict[str, int] = {}
        for room in assignments.values():
            counts[room] = counts.get(room, 0) + 1
        for room, n in counts.items():
            cap = BEDROOM_CAPACITY if is_bedroom(room) else ROOM_CAPACITY[room]
            if n > cap:
                return reject("enter_rooms", f"{room} 人数超过上限（{cap}）")
        sealed = s["sealed_room"]
        for name, room in assignments.items():
            if sealed and room == sealed["room"] and name != sealed["owner"] and name not in sealed["allowed"]:
                return reject("enter_rooms", f"{room} 被【封闭空间】锁死")
        locked_needed: dict[str, list[str]] = {}
        for name in s["order"]:
            if name not in assignments:
                continue
            room = assignments[name]
            if not s["locks"].get(room, False):
                continue
            if is_bedroom(room) and bedroom_owner(room) == name:
                continue
            inviter = invited.get(name)
            if inviter and room == bedroom_of(inviter) and assignments.get(inviter) == room:
                continue
            locked_needed.setdefault(room, []).append(name)
        unlock_plan: dict[str, str] = {}
        for room, names in locked_needed.items():
            payer = next((n for n in names if unlock_means(s, n) is not None), None)
            if payer is None:
                return reject("enter_rooms", f"{room} 上锁，进入者都没有开锁手段")
            unlock_plan[room] = payer
        private: dict = {}
        for room, payer in unlock_plan.items():
            means = unlock_means(s, payer)
            consume_unlock_means(s, payer, means)
            s["locks"][room] = False
            if is_bedroom(room) and bedroom_owner(room) != payer:
                s["bedroom_relock"].append(room)
            private[payer] = f"你用{means}打开了{room}"
        s["positions"] = dict(assignments)
        s["rooms_entered"] = current_period(s)
        for name, room in assignments.items():
            record_entry(s, name, room)
        for room, payer in unlock_plan.items():
            for w in [n for n, r in assignments.items() if r == room and n != payer]:
                private[w] = f"{payer} 打开了{room}的门"
        public: dict = {"announcements": []}
        det = s["detective"]
        if det and det in assignments:
            public["announcements"].append(f"侦探 {det} 选择了 {assignments[det]}")
            public["detective_room"] = assignments[det]
        device_events = murder_mod.trigger_devices(s)
        for dev_ev in device_events:
            private.update(dev_ev["private"])
        ev = {"ok": True, "type": "enter_rooms", "public": public, "private": private}
        if device_events:
            ev["device_triggers"] = [e["case"]["victim"] for e in device_events]
        if check_winner(s):
            ev["winner"] = s["winner"]
            ev["win_reason"] = s["win_reason"]
        return ev

    def night_assign(self, assignments: dict[str, str] | None = None, invited: dict[str, str] | None = None) -> dict:
        """晚间休息归宿结算（按行动顺序逐个安置，冲突者默认返回自己卧室）。

        assignments 缺省回自己卧室。规则要点：
        - 每个夜间房间（书房/监控室/地下室）只能进入 1 人，靠后者失败；
        - 卧室满 2 人后他人无法进入；被邀请者无法进入杀手已进入的卧室；
        - 杀手夜袭他人卧室需开锁手段；若卧室已有 2 人则无法进入（记为谋杀失败一次，
          消耗夜间谋杀机会并重置无杀倒计时）。
        结算后自动检查延时装置触发。
        """
        s = self.state
        if s["winner"]:
            return reject("night_assign", "游戏已结束")
        if s["phase"]["stage"] != "晚间休息":
            return reject("night_assign", "当前不是晚间休息阶段")
        day = s["phase"]["day"]
        assignments = assignments or {}
        invited = invited or {}
        positions: dict[str, str] = {}
        private: dict = {}
        for name in s["order"]:
            p = get_player(s, name)
            if not p or not p["alive"]:
                continue
            want = assignments.get(name, bedroom_of(name))
            placed = None
            note = None
            if want == bedroom_of(name):
                placed = want
            elif is_bedroom(want):
                owner = bedroom_owner(want)
                occupants = [n for n, r in positions.items() if r == want]
                killer_inside = any(get_player(s, n)["identity"] == "杀手" for n in occupants)
                is_invited = invited.get(name) == owner and positions.get(owner) == want
                is_night_raid = p["identity"] == "杀手" and not s["stage_murder_used"] and not is_invited
                if killer_inside and p["identity"] != "杀手":
                    note = "无法进入（不知原因）"
                elif len(occupants) >= BEDROOM_CAPACITY:
                    if is_night_raid:
                        s["stage_murder_used"] = True
                        s["last_murder_day"] = day
                        s["attempts"].append({"target": None, "day": day, "stage": "晚间休息", "room": want, "period": current_period(s)})
                        note = "无法进入（记为谋杀失败一次）"
                    else:
                        note = "无法进入（不知原因）"
                elif not is_invited and s["locks"].get(want, False):
                    means = unlock_means(s, name)
                    if means is None or (p["identity"] == "杀手" and s["stage_murder_used"]):
                        note = "无法进入（不知原因）"
                    else:
                        consume_unlock_means(s, name, means)
                        s["locks"][want] = False
                        s["bedroom_relock"].append(want)
                        placed = want
                        private[name] = f"你用{means}进入了{want}"
                else:
                    placed = want
            elif want in PUBLIC_ROOMS:
                occupants = [n for n, r in positions.items() if r == want]
                sealed = s["sealed_room"]
                if sealed and want == sealed["room"] and name != sealed["owner"] and name not in sealed["allowed"]:
                    note = "无法进入（不知原因）"
                elif occupants:
                    note = "无法进入（夜间房间已满）"
                elif s["locks"].get(want, False):
                    means = unlock_means(s, name)
                    if means is None:
                        note = "无法进入（房间上锁）"
                    else:
                        consume_unlock_means(s, name, means)
                        s["locks"][want] = False
                        placed = want
                        private[name] = f"你用{means}进入了{want}"
                else:
                    placed = want
            if placed is None:
                placed = bedroom_of(name)
                if note:
                    private.setdefault(name, note)
            positions[name] = placed
            record_entry(s, name, placed)
        s["positions"] = positions
        s["night_assigned"] = current_period(s)
        device_events = murder_mod.trigger_devices(s)
        for dev_ev in device_events:
            private.update(dev_ev["private"])
        ev = {"ok": True, "type": "night_assign", "public": {"announcements": []}, "private": private, "positions": dict(positions)}
        if device_events:
            ev["device_triggers"] = [e["case"]["victim"] for e in device_events]
        if check_winner(s):
            ev["winner"] = s["winner"]
            ev["win_reason"] = s["win_reason"]
        return ev
