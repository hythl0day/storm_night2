"""引擎主体：阶段流转、房间分配结算与行动入口（M3）。

阶段流：开局准备 → 公共发言 →（第 2 天起）投票 → 自由活动 → 晚间休息 → 次日清晨案件调查 → …
- 第 1 天没有投票，全天禁止谋杀；第 1 天公共发言时段改为侦探竞选（rules-6 §7.4，v3.3）。
- 每天翻页时行动顺序首位移到最后。
- 第 4 天起除卧室外所有门锁自动打开。
- 无杀倒计时：自第 2 天起算，连续两个完整天无谋杀行为（含未遂）则下一清晨平民胜。
- 第 5 天晚间休息结束仍无结果 → 主神获胜（全员失败）。
- 自由活动合法去向 = 会客室/书房/监控室/地下室（白天卧室清场）；会客室不限人数、
  永不黑暗、夜间可夜宿（v3.4）；v3.5 起监控覆盖会客室门口。
- v3.5 选房为顺序结算（rules-6 §7.5）：按行动顺序逐人即时结算进入——进入即开锁
  （自动消耗手段），满员/无手段/被封闭空间挡住者滞留会客室；顺序靠后者可见先前结果。
  延时装置仍在全员结算后统一判定（触发时机不变）。
"""
from __future__ import annotations

from . import ledger, murder as murder_mod, vote as vote_mod
from .state import (
    ACTION_STAGES,
    BEDROOM_CAPACITY,
    FREE_ROOMS,
    HALL,
    MEETING_STAGES,
    PUBLIC_ROOMS,
    ROOM_CAPACITY,
    bedroom_of,
    bedroom_owner,
    check_winner,
    consume_unlock_means,
    current_period,
    door_locked,
    ensure,
    get_player,
    is_bedroom,
    is_dark,
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

    def bequest(self, owner: str, mapping: dict, replies: dict) -> dict:
        return vote_mod.bequest(self.state, owner, mapping, replies)

    def elect_detective(self, ran: list[str], stay: list[str], votes: dict[str, str | None]) -> dict:
        return vote_mod.elect_detective(self.state, ran, stay, votes)

    def bequest_badge(self, owner: str, to: str | None, accept: bool) -> dict:
        return vote_mod.bequest_badge(self.state, owner, to, accept)

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

    def view_monitoring(self, user: str) -> dict:
        return ledger.view_monitoring(self.state, user)

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

    def set_passage(self, user: str) -> dict:
        return ledger.set_passage(self.state, user)

    def use_passage(self, user: str, room: str) -> dict:
        return ledger.use_passage(self.state, user, room)

    def place_bug(self, user: str, room: str) -> dict:
        return ledger.place_bug(self.state, user, room)

    def bug_listen(self, user: str) -> dict:
        return ledger.bug_listen(self.state, user)

    def take_table_item(self, user: str, item: str) -> dict:
        return ledger.take_table_item(self.state, user, item)

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
            alive_names = [p["name"] for p in s["players"] if p["alive"]]
            s["gifts_used"] = []
            shown_order = " → ".join(n for n in s["order"] if n in alive_names)
            ann = f"第 {new_day}/5 天开始。今日行动顺序：{shown_order}"
            if s["detective"] and s["detective"] in alive_names:
                ann += f"；侦探：{s['detective']}"
            public["announcements"].append(ann)
            # 清晨状态行：host 算好的全局状态，避免玩家自行推算规则状态出错（game/42 复盘项）
            public["announcements"].append(f"目前存活 {len(alive_names)} 人：{'、'.join(alive_names)}")
            # 无杀倒计时自第 2 天起算（rules-6 §8）：尚无谋杀行为时视同末次行为为第 1 天
            last_behavior = s["last_murder_day"] if s["last_murder_day"] is not None else 1
            quiet = new_day - 1 - last_behavior
            public["announcements"].append(
                f"无杀倒计时：已连续 {quiet} 个完整天无谋杀行为（连续 2 个完整天则平民直接获胜）")
            if s["sealed_items"]:
                public["announcements"].append(f"遗留道具待立遗嘱：{len(s['sealed_items'])} 件")
            if new_day == 4:
                for r in PUBLIC_ROOMS:
                    s["locks"][r] = False
                s["locks"][HALL] = False  # 含会客室夜锁（v3.4）
                public["announcements"].append("第 4 天起，除卧室外所有门锁自动打开")
            if last_behavior <= new_day - 3:
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
            s["locks"][HALL] = False  # 会客室夜锁随夜晚结束解除（v3.4）
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
            if s["phase"]["day"] < 4:
                s["locks"][HALL] = True  # v3.4：会客室夜间自动落锁（第 4 天起全开）

    def _morning_report(self, day: int, public: dict, private: dict) -> None:
        """清晨公告：昨日死亡与未遂、强制开锁、职业公开、尸体道具封存、桌上物品变动、监控公开宣读。"""
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
        hall_sleepers = [n for n, r in s["positions"].items()
                         if r == HALL and (get_player(s, n) or {}).get("alive")]
        if hall_sleepers:
            public["announcements"].append(f"夜宿会客室公开：{'、'.join(hall_sleepers)}（大厅无隐私）")
        if s["table_events"]:
            public["announcements"] += list(s["table_events"])  # v3.5：桌上物品变动（夜间被取走等）
            s["table_events"] = []
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
            if yday in s.get("blackout_days", []):
                public["announcements"].append("（昨日停电，当日时段监控记录缺失——缺失不代表无人进入）")

    def choose_room_one(self, name: str, room: str) -> dict:
        """顺序结算的单人进入（v3.5）：进入即开锁（自动消耗手段）；无法进入者滞留会客室。

        供编排器交错模式使用：每位玩家选择后立即结算（顺序靠后者可见先前结果）。
        positions 随逐人结算增量构建（编排器在选房轮开始时重置）。
        """
        s = self.state
        if s["winner"]:
            return reject("choose_room", "游戏已结束")
        if s["phase"]["stage"] != "自由活动":
            return reject("choose_room", "当前不是自由活动阶段")
        p = get_player(s, name)
        if p is None or not p["alive"]:
            return reject("choose_room", "玩家不存在或已死亡")
        private: dict = {}
        placed, note = self._settle_entry(name, room)
        s["positions"][name] = placed
        record_entry(s, name, placed)
        if note:
            private[name] = note
        return {"ok": True, "type": "choose_room", "public": None, "private": private,
                "room": placed, "stranded": placed != room}

    def _settle_entry(self, name: str, room: str) -> tuple[str, str | None]:
        """按当前已结算位置判定单人进入结果。返回（实际房间, 私密提示|None）。

        进入上锁房间即自动消耗手段开门（顺序结算下被锁房间此刻必然无人，无需通知同房者——
        靠后进入者从实时简报得知门已开，见规则书 §7.5.3）。
        """
        s = self.state
        if room not in FREE_ROOMS:
            hint = "（白天卧室清场，不开放）" if is_bedroom(room) else ""
            return HALL, f"「{room}」不是自由活动的合法去向{hint}，你滞留在会客室"
        if room == HALL:
            return HALL, None
        occupants = [n for n, r in s["positions"].items() if r == room]
        if len(occupants) >= ROOM_CAPACITY[room]:
            return HALL, f"{room} 已满员（{ROOM_CAPACITY[room]} 人），你滞留在会客室"
        sealed = s["sealed_room"]
        if sealed and room == sealed["room"] and name != sealed["owner"] and name not in sealed["allowed"]:
            return HALL, f"{room} 被【封闭空间】锁死，你无法进入，滞留在会客室"
        if door_locked(s, room):
            means = unlock_means(s, name)
            if means is None:
                return HALL, f"{room} 上锁，你没有开锁手段，滞留在会客室"
            consume_unlock_means(s, name, means)
            s["locks"][room] = False
            return room, f"你用{means}打开了{room}"
        return room, None

    def finish_room_entries(self) -> dict:
        """选房收尾（v3.5）：落结算标记，延时装置在全员进入后统一判定。"""
        s = self.state
        if s["winner"]:
            return reject("finish_room_entries", "游戏已结束")
        if s["phase"]["stage"] != "自由活动":
            return reject("finish_room_entries", "当前不是自由活动阶段")
        s["rooms_entered"] = current_period(s)
        private: dict = {}
        device_events = murder_mod.trigger_devices(s)
        for dev_ev in device_events:
            private.update(dev_ev["private"])
        ev: dict = {"ok": True, "type": "finish_room_entries", "public": None, "private": private}
        if device_events:
            ev["device_triggers"] = [e["case"]["victim"] for e in device_events]
        if check_winner(s):
            ev["winner"] = s["winner"]
            ev["win_reason"] = s["win_reason"]
        return ev

    def enter_rooms(self, assignments: dict[str, str], invited: dict[str, str] | None = None) -> dict:
        """自由活动选房批量结算（测试/便捷入口）：内部按行动顺序逐人顺序结算（v3.5）。

        assignments 须覆盖全体存活玩家；合法去向 = 会客室/书房/监控室/地下室（白天卧室清场）。
        逐人语义见 choose_room_one：进入即开锁（顺序最前的有手段者付费，之后随入）；
        满员/无手段/被封闭空间挡住者滞留会客室（不再整体驳回）。invited 仅作存档兼容，无机制效果。
        结算后统一检查延时装置触发。
        """
        s = self.state
        if s["winner"]:
            return reject("enter_rooms", "游戏已结束")
        if s["phase"]["stage"] != "自由活动":
            return reject("enter_rooms", "当前不是自由活动阶段")
        alive = [p["name"] for p in s["players"] if p["alive"]]
        if set(assignments) != set(alive):
            return reject("enter_rooms", "必须覆盖全体存活玩家")
        s["positions"] = {}
        private: dict = {}
        stranded: dict[str, str] = {}
        for name in s["order"]:
            if name not in assignments:
                continue
            ev = self.choose_room_one(name, assignments[name])
            private.update(ev["private"])
            if ev["stranded"]:
                stranded[name] = ev["room"]
        fin = self.finish_room_entries()
        private.update(fin["private"])
        public: dict = {"announcements": []}
        ev = {"ok": True, "type": "enter_rooms", "public": public, "private": private,
              "positions": dict(s["positions"])}
        if stranded:
            ev["stranded"] = stranded
        if fin.get("device_triggers"):
            ev["device_triggers"] = fin["device_triggers"]
        if "winner" in fin:
            ev["winner"] = fin["winner"]
            ev["win_reason"] = fin["win_reason"]
        return ev

    def night_assign(self, assignments: dict[str, str] | None = None, invited: dict[str, str] | None = None) -> dict:
        """晚间休息归宿结算（按行动顺序逐个安置，冲突者默认返回自己卧室）。

        assignments 缺省回自己卧室。规则要点：
        - 每个夜间房间（书房/监控室/地下室）只能进入 1 人，靠后者失败；
        - 卧室满 2 人后他人无法进入；被邀请者无法进入杀手已进入的卧室；
        - 杀手夜袭他人卧室需开锁手段（停电期间门锁失效除外）；若卧室已有 2 人：停电黑暗中
          杀手可潜入并袭击其一；否则无法进入——当夜谋杀机会消耗，但不计为谋杀行为（不重置无杀倒计时）。
        - 停电期间所有门锁失效（door_locked 统一裁定）。
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
                    if is_night_raid and is_dark(s, want):
                        # 停电黑暗中卧室满员也可潜入（v3.2 破守夜）；谋杀条件在深夜判定（黑暗 ≤2）
                        placed = want
                        private[name] = "你在黑暗中潜入了这间已有两人的卧室"
                    elif is_night_raid:
                        s["stage_murder_used"] = True  # 消耗当夜机会，但不计为谋杀行为（不重置无杀倒计时）
                        note = "无法进入（房内满员，当夜谋杀机会已消耗）"
                    else:
                        note = "无法进入（不知原因）"
                elif not is_invited and door_locked(s, want):
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
            elif want == HALL:
                # v3.4：会客室夜宿——夜间上锁（停电时锁失效可直接进入；第 4 天起全开）；
                # 【暴力破解】开锁后当夜他人可随入，夜锁每日重新落下。人数不限，亮房谋杀条件。
                if door_locked(s, HALL):
                    means = unlock_means(s, name)
                    if means is None:
                        note = "无法进入（会客室夜间上锁）"
                    else:
                        consume_unlock_means(s, name, means)
                        s["locks"][HALL] = False
                        placed = want
                        private[name] = f"你用{means}打开了会客室的夜锁，今晚在大厅过夜"
                else:
                    placed = want
            elif want in PUBLIC_ROOMS:
                occupants = [n for n, r in positions.items() if r == want]
                sealed = s["sealed_room"]
                if sealed and want == sealed["room"] and name != sealed["owner"] and name not in sealed["allowed"]:
                    note = "无法进入（不知原因）"
                elif occupants:
                    note = "无法进入（夜间房间已满）"
                elif door_locked(s, want):
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
