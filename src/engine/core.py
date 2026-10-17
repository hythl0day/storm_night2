"""引擎主体：阶段流转、房间分配结算与行动入口（M3）。

阶段流：开局准备 → 公共发言 →（第 2 天起）投票 → 自由活动 → 晚间休息 → 次日清晨案件调查 → …
- 第 1 天没有投票，公共发言时段改为侦探竞选（rules-6 §7.4，v3.3）；v3.29 起首日自由活动与晚间均可谋杀。
- 每天翻页时行动顺序首位移到最后。
- 第 4 天起除卧室外所有门锁自动打开。
- 无杀倒计时（v4.0）：从第 1 天起算（基准日 0），连续两个完整天无谋杀行为（含未遂与下毒）则下一清晨平民胜。
- 第 6 天为终审日（只有白天会议），其投票结束仍无结果 → 主神获胜（全员失败）。
- 自由活动合法去向 = 会客室/书房/监控室/地下室（白天卧室清场）；会客室不限人数、
  永不黑暗、夜间可夜宿（v3.4）；v3.5 起监控覆盖会客室门口。
- v3.26 昼夜统一为去向声明 → 独立开锁 → 正常进入：每扇门由拟进入者私密决定是否开锁，
  开锁者优先，其余按行动顺序；容量与其他进入规则不变。
- v3.22：晚间休息与自由活动同构（合法去向含卧室；卧室夜间黑暗；平民慎入功能房）；
  死亡/未遂不再阶段末即时公告，统一由次日清晨发布。
- v4.0：缓释毒剂的延迟死亡在晚间→清晨翻页时结算（死于自己卧室，计入案件，清晨公告前落账）；
  延时装置改为谋杀窗口手动启动（device_fire），进入结算不再自动触发；密道改为谋杀动作（passage_kill）；
  每窗口全队一次谋杀尝试（stage_murder_used），每天全队至多一起成功谋杀（day_kill_capped，翻页进自由活动重置）。
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
    countdown_text,
    current_period,
    discover_bodies,
    door_locked,
    ensure,
    get_player,
    is_bedroom,
    is_villain,
    mark_blackout_period,
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

    def pharmacology_kill(self, user: str, target: str, savior: str | None = None) -> dict:
        return murder_mod.pharmacology_kill(self.state, user, target, savior)

    def strong_attack(self, user: str, target: str) -> dict:
        return murder_mod.strong_attack(self.state, user, target)

    def slow_poison(self, user: str, target: str) -> dict:
        return murder_mod.slow_poison(self.state, user, target)

    def passage_kill(self, user: str, target: str, weapon: str | None = None, savior: str | None = None) -> dict:
        return murder_mod.passage_kill(self.state, user, target, weapon, savior)

    def set_device(self, user: str, room: str) -> dict:
        return murder_mod.set_device(self.state, user, room)

    def device_fire(self, user: str) -> dict:
        return murder_mod.device_fire(self.state, user)

    def pending_devices(self) -> list[dict]:
        """机关推送查询（谋杀窗口菜单用）：当前可启动的延时装置列表。"""
        return murder_mod.pending_devices(self.state)

    def stage_magic(self, user: str, target: str, savior: str | None = None) -> dict:
        return murder_mod.stage_magic(self.state, user, target, savior)

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

    def remote_view_monitoring(self, user: str) -> dict:
        """【远程入侵】/「木马程序U盘」：远程查看当前监控记录（不要求身处监控室）。"""
        return ledger.remote_view_monitoring(self.state, user)

    def remote_tamper_monitoring(self, user: str, period: str) -> dict:
        """【远程入侵】/「木马程序U盘」反派用途：远程抹除某天某时段的全部监控记录（永不恢复）。"""
        return ledger.remote_tamper_monitoring(self.state, user, period)

    def investigate(self, user: str, case_id: int) -> dict:
        """现场调查（v4.0）：一次获知全部四项结论，结果经事件 public 字段全员公告；每案一次。"""
        return ledger.investigate(self.state, user, case_id)

    def tamper_investigation(self, user: str, case_id: int) -> dict:
        """篡改案件调查（v4.0）：反派消耗一次调查次数，随机篡改两项；一次定型。"""
        return ledger.tamper_investigation(self.state, user, case_id)

    def investigation_queue(self) -> list[dict]:
        """清晨调查队列（v4.0，供编排器驱动）：当日清晨公告、尚未定型的案件 × 持调查手段者
        （按行动顺序，含反派）。编排器按队列顺序处理：平民自动 investigate，反派私密菜单
        （tamper_investigation / investigate / 跳过）。"""
        s = self.state
        out = []
        if s["phase"]["stage"] != "案件调查":
            return out
        day = s["phase"]["day"]
        for c in s["cases"]:
            if not c.get("announced") or c.get("investigated") or c["day"] != day - 1:
                continue
            holders = []
            for name in s["order"]:
                p = get_player(s, name)
                if p and p["alive"] and ledger.investigation_means(s, p):
                    holders.append({"name": name, "villain": is_villain(p)})
            out.append({"case_id": c["id"], "victim": c["victim"], "room": c["room"], "holders": holders})
        return out

    def see_through(self, user: str, target: str, claims: dict) -> dict:
        return ledger.see_through(self.state, user, target, claims)

    def seal_room(self, user: str, allowed: list[str] | None = None) -> dict:
        return ledger.seal_room(self.state, user, allowed)

    def place_bug(self, user: str, room: str) -> dict:
        return ledger.place_bug(self.state, user, room)

    def bug_listen(self, user: str) -> dict:
        return ledger.bug_listen(self.state, user)

    def take_table_item(self, user: str, item: str) -> dict:
        return ledger.take_table_item(self.state, user, item)

    def report(self, user: str) -> dict:
        return ledger.report(self.state, user)

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
            # v4.0：缓释毒剂延迟死亡在翻页时、清晨公告前结算——中毒者死于自己卧室
            # （案发房间=其卧室，时段=晚间休息，伤痕=毒或药物；已被投票出局者不再结算）
            for e in list(s["poisoned"]):
                if e["day"] != day:
                    continue
                s["poisoned"].remove(e)
                tp = get_player(s, e["name"])
                if tp and tp["alive"]:
                    murder_mod._apply_kill(s, e["name"], "缓释毒剂", bedroom_of(e["name"]), e["by"])
            new_day, new_stage = day + 1, "案件调查"
            s["order"] = s["order"][1:] + s["order"][:1]
            alive_names = [p["name"] for p in s["players"] if p["alive"]]
            shown_order = " → ".join(n for n in s["order"] if n in alive_names)
            ann = f"第 {new_day}/6 天开始。今日行动顺序：{shown_order}"
            if s["detective"] and s["detective"] in alive_names:
                ann += f"；侦探：{s['detective']}"
            public["announcements"].append(ann)
            # 清晨状态行：host 算好的全局状态，避免玩家自行推算规则状态出错（game/42 复盘项）
            public["announcements"].append(f"目前存活 {len(alive_names)} 人：{'、'.join(alive_names)}")
            # 无杀倒计时从第 1 天起算（rules-6 §8，v4.0）：尚无谋杀行为时基准为第 0 天
            # （第 1、2 天均无行为 → 第 3 天清晨平民直接获胜）；公告口径见 countdown_text
            last_behavior = s["last_murder_day"] if s["last_murder_day"] is not None else 0
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
        if stage in ("自由活动", "晚间休息") and s["power_out"]:
            mark_blackout_period(s)
        if stage in MEETING_STAGES:
            s["positions"] = {p["name"]: "会客室" for p in s["players"] if p["alive"]}
            s["locks"][HALL] = False  # 会客室夜锁随夜晚结束解除（v3.4）
        if stage == "自由活动":
            s["stage_murder_used"] = False
            s["day_kill_capped"] = False  # v4.0：每日成功封顶随新一天自由活动重置
            s["flashlight_lit"] = []
            s["passage_stealth"] = []
            s["sealed_room"] = None
            if s["power_out"] and s.get("blackout_news") != current_period(s):
                # 当天新声明的停电在选房收尾后统一公布（rules-6 §7.5）；这里只公告跨日延续的停电
                public["announcements"].append("停电中：除会客室外所有房间一片黑暗（可前往地下室修复发电机恢复供电——未修复则停电一直持续）")
        if stage == "晚间休息":
            # 每窗口一次尝试：进入晚间窗口重置（v4.0：白天成功击杀后的晚间封顶由 day_kill_capped 承担）
            s["stage_murder_used"] = False
            s["flashlight_lit"] = []
            s["passage_stealth"] = []
            if s["phase"]["day"] < 4:
                s["locks"][HALL] = True  # v3.4：会客室夜间自动落锁（第 4 天起全开）
            if s["power_out"]:
                public["announcements"].append("停电持续中：除会客室外所有房间一片黑暗（含卧室——受邀组合仍可摸黑低语）；修复发电机前不会恢复")
            elif (s.get("power_restored") or "").startswith(f"第{s['phase']['day']}天"):
                # 当天修复后，晚间开始时明确告知供电已恢复；跨日停电按实际修复日判断
                public["announcements"].append("供电已恢复（发电机已于今日修复）：今晚门锁与监控正常工作；停电造成的黑暗已解除，地下室与夜间卧室仍黑暗")

    def _morning_report(self, day: int, public: dict, private: dict) -> None:
        """清晨公告：昨日死亡与未遂、强制开锁、职业公开、尸体道具封存、桌上物品变动、监控公开宣读。"""
        s = self.state
        yday = day - 1
        cases = [c for c in s["cases"] if c["day"] == yday]
        attempts = [a for a in s["attempts"] if a["day"] == yday and a["target"]]
        for c in cases:
            c["announced"] = True  # v3.30：案件自本清晨公告后进入公开状态（发现/报案窗口关闭）
            reporters = c.get("reported_by") or []
            suffix = f"（报案人：{'、'.join(reporters)}）" if reporters else ""
            public["announcements"].append(f"{c['victim']} 死亡，尸体发现于{c['room']}{suffix}")
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
            # v3.29：同日多案按时段合并房间集合，后一案不得覆盖前一案的案发房间与死者卧室
            for period in (f"第{yday}天自由活动", f"第{yday}天晚间休息"):
                records = s["monitoring"].get(period, {})
                period_rooms = monitoring_public.setdefault(period, {})
                for room in sorted(rooms):
                    period_rooms[room] = records.get(room, [])
        if monitoring_public:
            public["monitoring"] = monitoring_public
            public["announcements"].append("监控公开宣读：案发当日全天（自由活动 + 晚间休息）（案发房间 + 死者卧室）的进入者名单如下")
            free = f"第{yday}天自由活动"
            night = f"第{yday}天晚间休息"
            gaps = set(s.get("blackout_periods", []))
            missing = [p for p in (free, night) if p in gaps]
            if not gaps and yday in s.get("blackout_days", []):
                # 兼容手工构造/极旧存档：没有时段台账时沿用原来的按日推断口径
                missing = [free]
                if s.get("power_restored") != free:
                    missing.append(night)
            if missing:
                public["announcements"].append(
                    f"（昨日停电，{'与'.join(missing)}时段监控记录缺失——缺失不代表无人进入）")

    def unlock_for_entry(self, user: str, room: str, assignments: dict[str, str]) -> dict:
        """进入前的独立开锁轮：只为本人已声明的目标房间开门，不移动任何玩家。"""
        s = self.state
        stage = s["phase"]["stage"]
        if s["winner"]:
            return reject("entry_unlock", "游戏已结束")
        if stage not in ACTION_STAGES:
            return reject("entry_unlock", "当前不是进入前的开锁阶段")
        p = get_player(s, user)
        if p is None or not p["alive"]:
            return reject("entry_unlock", "玩家不存在或已死亡")
        target = assignments.get(user, bedroom_of(user) if stage == "晚间休息" else None)
        if target != room:
            return reject("entry_unlock", "只能为本人本轮声明的目标房间开锁")
        alive_names = [q["name"] for q in s["players"] if q["alive"]]
        valid = room in FREE_ROOMS or (
            stage == "晚间休息" and is_bedroom(room) and bedroom_owner(room) in alive_names
        )
        if not valid:
            return reject("entry_unlock", "该房间不是本阶段合法去向")
        if stage == "晚间休息" and room == bedroom_of(user):
            return reject("entry_unlock", "自己的卧室无需开锁")
        sealed = s["sealed_room"]
        if sealed and room == sealed["room"] and user != sealed["owner"] and user not in sealed["allowed"]:
            return reject("entry_unlock", "封闭空间无法用暴力破解打开")
        if not door_locked(s, room):
            return reject("entry_unlock", "房门当前无需开锁")
        means = unlock_means(s, user)
        if means is None:
            return reject("entry_unlock", "没有可用开锁手段")
        consume_unlock_means(s, user, means)
        s["locks"][room] = False
        if stage == "自由活动":
            s["selection_begun"] = current_period(s)
        if is_bedroom(room) and bedroom_owner(room) != user and room not in s["bedroom_relock"]:
            s["bedroom_relock"].append(room)
        return {
            "ok": True,
            "type": "entry_unlock",
            "public": None,
            "private": {user: f"你用{means}打开了{room}；本轮声明进入该房间的人将继续按进入规则结算"},
            "room": room,
            "unlocker": user,
            "means": means,
        }

    def _auto_entry_unlocks(self, assignments: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        """测试/批量入口的默认开锁轮：视同候选者均愿意，按行动顺序为每扇门选择开锁者。"""
        s = self.state
        stage = s["phase"]["stage"]
        unlockers: dict[str, str] = {}
        private: dict[str, str] = {}
        for name in s["order"]:
            p = get_player(s, name)
            if not p or not p["alive"]:
                continue
            room = assignments.get(name, bedroom_of(name) if stage == "晚间休息" else None)
            if room is None or room in unlockers or not door_locked(s, room):
                continue
            if stage == "晚间休息" and room == bedroom_of(name):
                continue
            if unlock_means(s, name) is None:
                continue
            ev = self.unlock_for_entry(name, room, assignments)
            if not ev["ok"]:
                continue
            unlockers[room] = name
            for target, msg in ev["private"].items():
                private[target] = (private.get(target, "") + "；" if target in private else "") + msg
        return unlockers, private

    def _entry_sequence(self, assignments: dict[str, str], unlockers: dict[str, str],
                        invited: dict[str, str]) -> list[str]:
        """开锁者优先，其余玩家保持原行动顺序。"""
        s = self.state
        stage = s["phase"]["stage"]
        base = [
            n for n in s["order"]
            if (get_player(s, n) or {}).get("alive") and (stage == "晚间休息" or n in assignments)
        ]

        def target(name: str) -> str:
            return assignments.get(name, bedroom_of(name) if stage == "晚间休息" else HALL)

        openers = {
            name for room, name in unlockers.items()
            if name in base and target(name) == room
        }
        return [n for n in base if n in openers] + [n for n in base if n not in openers]

    def choose_room_one(self, name: str, room: str) -> dict:
        """单人进入的低层入口；门锁须已在此前的开锁轮处理，本方法不再自动消耗手段。"""
        s = self.state
        if s["winner"]:
            return reject("choose_room", "游戏已结束")
        if s["phase"]["stage"] != "自由活动":
            return reject("choose_room", "当前不是自由活动阶段")
        p = get_player(s, name)
        if p is None or not p["alive"]:
            return reject("choose_room", "玩家不存在或已死亡")
        private: dict = {}
        s["selection_begun"] = current_period(s)
        placed, note = self._settle_entry(name, room)
        s["positions"][name] = placed
        record_entry(s, name, placed)
        if note:
            private[name] = note
        return {"ok": True, "type": "choose_room", "public": None, "private": private,
                "room": placed, "stranded": placed != room}

    def _settle_entry(self, name: str, room: str) -> tuple[str, str | None]:
        """按当前已结算位置判定单人进入结果。返回（实际房间, 私密提示|None）。"""
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
            return HALL, f"{room} 上锁，本轮开锁行动轮无人开门，你滞留在会客室"
        return room, None

    def finish_room_entries(self) -> dict:
        """选房收尾（v3.5）：落结算标记；v3.30 之后统一判定凶案发现。
        v4.0：延时装置不再在进入结算时自动触发（改谋杀窗口手动 device_fire），此处只发现尸体。"""
        s = self.state
        if s["winner"]:
            return reject("finish_room_entries", "游戏已结束")
        if s["phase"]["stage"] != "自由活动":
            return reject("finish_room_entries", "当前不是自由活动阶段")
        s["rooms_entered"] = current_period(s)
        private: dict = {}
        for name, msg in discover_bodies(s).items():  # v3.30：发现只私下告知（当前阶段顺序下此处恒为空操作）
            private[name] = (private.get(name, "") + "；" if name in private else "") + msg
        ev: dict = {"ok": True, "type": "finish_room_entries", "public": None, "private": private}
        if check_winner(s):
            ev["winner"] = s["winner"]
            ev["win_reason"] = s["win_reason"]
        return ev

    def enter_rooms(self, assignments: dict[str, str], invited: dict[str, str] | None = None,
                    unlockers: dict[str, str] | None = None) -> dict:
        """自由活动统一进入结算：先独立开锁，再由开锁者优先、其余按行动顺序进入。

        assignments 须覆盖全体存活玩家。unlockers 为 {房间: 开锁者}；None 是测试/批量入口的
        默认开锁轮（视同候选者均愿意），显式传空 dict 表示本轮无人开锁。
        """
        s = self.state
        if s["winner"]:
            return reject("enter_rooms", "游戏已结束")
        if s["phase"]["stage"] != "自由活动":
            return reject("enter_rooms", "当前不是自由活动阶段")
        alive = [p["name"] for p in s["players"] if p["alive"]]
        if set(assignments) != set(alive):
            return reject("enter_rooms", "必须覆盖全体存活玩家")
        invited = invited or {}
        if unlockers is None:
            unlockers, private = self._auto_entry_unlocks(assignments)
        else:
            unlockers, private = dict(unlockers), {}
        s["positions"] = {}
        s["selection_begun"] = current_period(s)
        stranded: dict[str, str] = {}
        for name in self._entry_sequence(assignments, unlockers, invited):
            placed, note = self._settle_entry(name, assignments[name])
            s["positions"][name] = placed
            record_entry(s, name, placed)
            if note:
                private[name] = (private.get(name, "") + "；" if name in private else "") + note
            if placed != assignments[name]:
                stranded[name] = placed
        fin = self.finish_room_entries()
        for name, msg in fin["private"].items():
            private[name] = (private.get(name, "") + "；" if name in private else "") + msg
        public: dict = {"announcements": []}
        ev = {"ok": True, "type": "enter_rooms", "public": public, "private": private,
              "positions": dict(s["positions"]), "unlockers": dict(unlockers)}
        if stranded:
            ev["stranded"] = stranded
        if "winner" in fin:
            ev["winner"] = fin["winner"]
            ev["win_reason"] = fin["win_reason"]
        return ev

    def night_assign(self, assignments: dict[str, str] | None = None, invited: dict[str, str] | None = None,
                     unlockers: dict[str, str] | None = None) -> dict:
        """晚间统一进入结算：独立开锁后，开锁者优先，其余进入规则不变。

        assignments 缺省回自己卧室；invited={应邀者: 邀请者}。unlockers 为 None 时视同
        候选者均愿意并自动执行默认开锁轮；显式空 dict 表示本轮无人开锁。
        """
        s = self.state
        if s["winner"]:
            return reject("night_assign", "游戏已结束")
        if s["phase"]["stage"] != "晚间休息":
            return reject("night_assign", "当前不是晚间休息阶段")
        assignments = assignments or {}
        invited = invited or {}
        if unlockers is None:
            unlockers, private = self._auto_entry_unlocks(assignments)
        else:
            unlockers, private = dict(unlockers), {}
        positions: dict[str, str] = {}
        host: list[str] = []
        notified_invites: set[str] = set()
        for name in self._entry_sequence(assignments, unlockers, invited):
            p = get_player(s, name)
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
                    note = "无法进入"
                    host.append(f"{name} 未能进入{want}（真实原因：本轮开锁行动轮无人开门）")
                else:
                    placed = want
                if placed == want and is_invited:
                    private[owner] = (private.get(owner, "") + "；" if private.get(owner) else "") + \
                        f"{name} 接受了你的邀请，进入了{want}——即使房间黑暗，你也知道 TA 来了。"
                    notified_invites.add(name)
            elif want == HALL:
                if door_locked(s, HALL):
                    note = "无法进入（会客室夜间上锁，本轮开锁行动轮无人开门）"
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
                    note = "无法进入（房间上锁，本轮开锁行动轮无人开门）"
                else:
                    placed = want
                if placed == want and is_invited:
                    inviter = invited[name]
                    private[inviter] = (private.get(inviter, "") + "；" if private.get(inviter) else "") + \
                        f"{name} 接受了你的邀请，进入了{want}——即使房间黑暗，你也知道 TA 来了。"
                    notified_invites.add(name)
            if placed is None:
                placed = bedroom_of(name)
                if note:
                    private.setdefault(name, note)
            positions[name] = placed
            record_entry(s, name, placed)
        for invitee, inviter in invited.items():
            if invitee in notified_invites:
                continue
            room = positions.get(invitee)
            if room and room == positions.get(inviter):
                private[inviter] = (private.get(inviter, "") + "；" if private.get(inviter) else "") + \
                    f"{invitee} 接受了你的邀请，进入了{room}——即使房间黑暗，你也知道 TA 来了。"
        s["positions"] = positions
        s["night_assigned"] = current_period(s)
        for name, msg in discover_bodies(s).items():  # v3.30：晚间休息是凶案发现的唯一真实窗口（发现白天自由活动的谋杀）
            private[name] = (private.get(name, "") + "；" if name in private else "") + msg
        ev = {"ok": True, "type": "night_assign", "public": {"announcements": []},
              "private": private, "positions": dict(positions), "unlockers": dict(unlockers)}
        if host:
            ev["host"] = host
        if check_winner(s):
            ev["winner"] = s["winner"]
            ev["win_reason"] = s["win_reason"]
        return ev
