"""引擎主体：阶段流转、房间分配结算与行动入口（M3）。

阶段流：开局准备 → 公共发言 →（第 2 天起）投票 → 自由活动 → 晚间休息 → 次日清晨案件调查 → …
- 第 1 天没有投票，全天禁止谋杀；第 1 天公共发言时段改为侦探竞选（rules-6 §7.4，v3.3）。
- 每天翻页时行动顺序首位移到最后。
- 第 4 天起除卧室外所有门锁自动打开。
- 无杀倒计时：自第 2 天起算，连续两个完整天无谋杀行为（含未遂）则下一清晨平民胜。
- 第 6 天为终审日（只有白天会议），其投票结束仍无结果 → 主神获胜（全员失败）。
- 自由活动合法去向 = 会客室/书房/监控室/地下室（白天卧室清场）；会客室不限人数、
  永不黑暗、夜间可夜宿（v3.4）；v3.5 起监控覆盖会客室门口。
- v3.5 选房为顺序结算（rules-6 §7.5）：按行动顺序逐人即时结算进入——进入即开锁
  （自动消耗手段），满员/无手段/被封闭空间挡住者滞留会客室；顺序靠后者可见先前结果。
  延时装置仍在全员结算后统一判定（触发时机不变）。
- v3.22：晚间休息与自由活动同构（合法去向含卧室；卧室夜间黑暗；平民慎入功能房；
  每阶段独立一次谋杀机会）；死亡/未遂不再阶段末即时公告，统一由次日清晨发布。
"""
from __future__ import annotations

from . import ledger, murder as murder_mod, vote as vote_mod
from .state import (
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
    countdown_text,
    current_period,
    door_locked,
    ensure,
    get_player,
    is_bedroom,
    is_villain,
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

    def resolve_vote(self, votes: dict[str, str | None], implant: dict | None = None,
                     defer_out: bool = False) -> dict:
        return vote_mod.resolve_vote(self.state, votes, implant, defer_out=defer_out)

    def execute_pending_out(self) -> dict:
        return vote_mod.execute_pending_out(self.state)

    def armed_accusation(self, accuser: str, suspect: str, weapon: str | None = None) -> dict:
        return vote_mod.armed_accusation(self.state, accuser, suspect, weapon)

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
        # v3.22：死亡/未遂不再即时公告（不暴露死亡时段），统一由次日清晨 _morning_report 发布
        if stage == "开局准备":
            new_day, new_stage = day, "公共发言"
        elif stage == "公共发言":
            new_day, new_stage = day, ("投票" if day >= 2 else "自由活动")
        elif stage == "投票":
            if day >= 6:
                # v3.15：主神死线从第 5 天晚间移至第 6 天投票后——第 6 天为终审日
                # （只有白天会议：案件调查/公共发言/投票，无自由活动与晚间休息）；
                # 锁死的残局由终审投票自然了结，无需"票仓判定"等新概念
                s["winner"] = "主神"
                s["win_reason"] = "第 6 天投票结束仍无阵营获胜，全员失败"
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
            new_day, new_stage = day, "自由活动"
        elif stage == "自由活动":
            new_day, new_stage = day, "晚间休息"
        elif stage == "晚间休息":
            new_day, new_stage = day + 1, "案件调查"
            s["order"] = s["order"][1:] + s["order"][:1]
            alive_names = [p["name"] for p in s["players"] if p["alive"]]
            s["gifts_used"] = []
            shown_order = " → ".join(n for n in s["order"] if n in alive_names)
            ann = f"第 {new_day}/6 天开始。今日行动顺序：{shown_order}"
            if s["detective"] and s["detective"] in alive_names:
                ann += f"；侦探：{s['detective']}"
            public["announcements"].append(ann)
            # 清晨状态行：host 算好的全局状态，避免玩家自行推算规则状态出错（game/42 复盘项）
            public["announcements"].append(f"目前存活 {len(alive_names)} 人：{'、'.join(alive_names)}")
            # 无杀倒计时自第 2 天起算（rules-6 §8）：尚无谋杀行为时视同末次行为为第 1 天；
            # 公告口径见 countdown_text，判定仍用 last_behavior
            last_behavior = s["last_murder_day"] if s["last_murder_day"] is not None else 1
            public["announcements"].append(countdown_text(new_day, s["last_murder_day"]))
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
            if s["power_out"] and s.get("blackout_news") != current_period(s):
                # 当天新声明的停电在选房收尾后统一公布（rules-6 §7.5）；这里只公告跨日延续的停电
                public["announcements"].append("停电中：除卧室外所有房间一片黑暗（可前往地下室修复发电机恢复供电——未修复则停电一直持续）")
        if stage == "晚间休息":
            # v3.22：每阶段独立一次谋杀机会——白天成功杀人不再占用当夜机会（rules-6 §7.6"每阶段一次"）
            s["stage_murder_used"] = False
            s["flashlight_lit"] = []
            s["passage_stealth"] = []
            if s["phase"]["day"] < 4:
                s["locks"][HALL] = True  # v3.4：会客室夜间自动落锁（第 4 天起全开）
            if s["power_out"]:
                public["announcements"].append("停电持续中：除会客室外所有房间一片黑暗（含卧室——受邀组合仍可摸黑低语）；修复发电机前不会恢复")
            elif s["phase"]["day"] in s.get("blackout_days", []) and s.get("power_restored"):
                # v3.20：白天修复后，晚间开始时明确告知供电已恢复（game/838823：杀手误以为当夜仍停电）
                public["announcements"].append("供电已恢复（发电机已于今日修复）：今晚门锁与监控正常工作，不再有黑暗")

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
                         if r == HALL and n not in s["passage_stealth"]
                         and (get_player(s, n) or {}).get("alive")]  # v3.22：密道者隐身，不上夜宿名单
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
            # v3.22：宣读覆盖案发当日全天两个时段（不暴露死亡时段——死亡时间只能靠调查获知）
            for period in (f"第{yday}天自由活动", f"第{yday}天晚间休息"):
                records = s["monitoring"].get(period, {})
                monitoring_public[period] = {r: records.get(r, []) for r in sorted(rooms)}
        if monitoring_public:
            public["monitoring"] = monitoring_public
            public["announcements"].append("监控公开宣读：案发当日全天（自由活动 + 晚间休息）（案发房间 + 死者卧室）的进入者名单如下")
            if yday in s.get("blackout_days", []):
                # v3.20：缺口精确到时段——修复于自由活动则仅该时段缺失；修复于晚间（归宿结算后）或
                # 未修复则当夜同样缺失（game/838823：粗口径"当日时段缺失"与随后宣读的夜间记录矛盾）
                missing = [f"第{yday}天自由活动"]
                if s.get("power_restored") != f"第{yday}天自由活动":
                    missing.append(f"第{yday}天晚间休息")
                public["announcements"].append(
                    f"（昨日停电，{'与'.join(missing)}时段监控记录缺失——缺失不代表无人进入）")

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
        s["selection_begun"] = current_period(s)  # 首个进入落定后，停电声明窗口关闭（rules-6 §7.5：阶段开始前声明）
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
        """晚间休息进入结算（v3.22 与白天同一套结构：按行动顺序逐个安置，进入结果互不可见）。

        assignments 缺省回自己卧室；invited={应邀者: 邀请者}（同入/守夜邀请，成立条件 =
        邀请者已安置在应邀者的目标房间）。规则要点（rules-6 §7.7 v3.22）：
        - 合法去向：自己卧室（默认）/ 他人卧室 / 会客室 / 书房 / 监控室 / 地下室；
        - 锁：他人卧室上锁——对所有人一视同仁（含反派）：需【暴力破解】手段或受邀
          （被他人开锁后下一阶段复锁）；会客室夜锁（手段或停电/第 4 天起免费）；
          监控室上锁（首开需手段，开后永久开启）；书房/地下室无锁；停电全部锁失效；
        - 容量与白天一致：书房 3 / 监控室 2 / 地下室 3 / 卧室 2 / 会客室不限；
        - 平民慎入：平民非受邀进入已有他人的功能房（书房/监控室/地下室）→ 失败回卧室
          （"已经有人了，出于安全考虑，你没有进入"）；被邀请者与反派豁免
          （豁免仅限本规则——反派进他人卧室仍须开锁或受邀）；
        - 无法进入者一律默认回自己卧室：功能房/会客室失败告知简要原因，卧室失败仅告知"无法进入"；
        - 卧室夜间黑暗（is_dark 统一裁定）：空手谋杀豁免附加条件、谋杀条件非反派 ≤2、
          受邀组合可摸黑低语（编排层分发）。
        结算后自动检查延时装置触发。
        """
        s = self.state
        if s["winner"]:
            return reject("night_assign", "游戏已结束")
        if s["phase"]["stage"] != "晚间休息":
            return reject("night_assign", "当前不是晚间休息阶段")
        assignments = assignments or {}
        invited = invited or {}
        positions: dict[str, str] = {}
        private: dict = {}
        host: list[str] = []  # host 备注：裁定真实原因（进 script.md 行动回执，对玩家保密）
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
                is_invited = invited.get(name) == owner and positions.get(owner) == want
                if len(occupants) >= BEDROOM_CAPACITY:
                    note = "无法进入"
                    host.append(f"{name} 未能进入{want}（真实原因：房内满员——{'、'.join(occupants)}）")
                elif not is_invited and door_locked(s, want):
                    means = unlock_means(s, name)
                    if means is None:
                        note = "无法进入"
                        host.append(f"{name} 未能进入{want}（真实原因：房门上锁且 TA 无开锁手段）")
                    else:
                        consume_unlock_means(s, name, means)
                        s["locks"][want] = False
                        s["bedroom_relock"].append(want)
                        placed = want
                        private[name] = f"你用{means}进入了{want}"
                else:
                    placed = want
                if placed == want and is_invited:
                    private[owner] = (private.get(owner, "") + "；" if private.get(owner) else "") + \
                        f"{name} 接受了你的邀请，进入了{want}——即使房间黑暗，你也知道 TA 来了。"
            elif want == HALL:
                # 会客室夜宿：夜间上锁（停电时锁失效可直接进入；第 4 天起全开）；人数不限，亮房
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
                is_invited = invited.get(name) in occupants  # 邀请者已在该房间 → 应邀成立
                if sealed and want == sealed["room"] and name != sealed["owner"] and name not in sealed["allowed"]:
                    note = "无法进入（房间被临时锁死）"
                    host.append(f"{name} 未能进入{want}（真实原因：被【封闭空间】锁死，主人：{sealed['owner']}）")
                elif len(occupants) >= ROOM_CAPACITY[want]:
                    note = f"无法进入（{want}已满员）"
                    host.append(f"{name} 未能进入{want}（真实原因：满员——{'、'.join(occupants)}）")
                elif occupants and not is_villain(p) and not is_invited:
                    # v3.22 平民慎入：平民不敢非受邀进入已有他人的功能房；反派与被邀请者不受此限
                    note = "已经有人了，出于安全考虑，你没有进入"
                    host.append(f"{name} 未进入{want}（平民慎入：房内已有——{'、'.join(occupants)}）")
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
                if placed == want and is_invited:
                    inviter = invited[name]
                    private[inviter] = (private.get(inviter, "") + "；" if private.get(inviter) else "") + \
                        f"{name} 接受了你的邀请，进入了{want}——即使房间黑暗，你也知道 TA 来了。"
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
        if host:
            ev["host"] = host
        if device_events:
            ev["device_triggers"] = [e["case"]["victim"] for e in device_events]
        if check_winner(s):
            ev["winner"] = s["winner"]
            ev["win_reason"] = s["win_reason"]
        return ev
