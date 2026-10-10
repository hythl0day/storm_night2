"""编排器主循环（M4）：按阶段生成简报 → 收集三通道 JSON → 引擎裁决 → 落盘。

用法：
    python -m src.orchestrator --game game/012345            # 跑通第 1 天全天（进入第 2 天清晨即停）
    python -m src.orchestrator --game game/012345 --resume   # 从 state.json 断点续跑（阶段粒度）

第 1 天流程：开局准备（仅反派建筑师声明密道、窃听器持有者声明放置，无义务者不调用）
→ 公共发言（按行动顺序每人一次）→ 交换环节（赠送先问赠送者，接受/拒绝再问接收者）
→ 自由活动（停电声明并入选房菜单；按顺序秘密选房+同入邀请；引擎结算；≥2 人且非黑暗房间
  一轮房间内交谈；逐人房间行动菜单）→ 晚间休息（留宿邀请/夜间房间行动，互相不可见选择）
→ advance() 进入第 2 天清晨即停。

消息结构：system = render(game_system.md)（人设+规则书+私密卡），user = 当次简报；
对话不累积，每阶段独立调用，记忆靠 notes 通道 + 增量简报。全部顺序执行，不并发。
观察者钩子（console 订阅；无头模式全为空操作）：on_call_start / on_token / on_call_end /
on_stage / pause_gate（阶段边界阻塞）/ should_stop（阶段边界安全退出）。

落盘（game/<id>/）：
  public/log.md                    公告/发言/交换/死亡公告（按阶段标题追加）
  private/<id>/inner.md            内心独白（追加）；private/<id>/notes.md 私人笔记（每阶段覆盖）
  private/<id>/log.md              该席听到的房间内交谈存档
  inbox/<id>/day{N}-<环节>.md      简报存档；outbox/<id>/ 原始回复存档
  host_log.jsonl                   每次 API 调用 {ts, player, stage, ok, latency, total_tokens, error}
  state.json                       每阶段结束落盘（断点续跑）

state 扩展（宿主侧，引擎字段之外新增）：
  state["orch"] = {"feed": [公开事件行], "cursor": {名字: 已读游标}, "private": {名字: [待送达私密事件]}}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from .engine import Engine
from .engine.state import (
    PUBLIC_ROOMS,
    bedroom_of,
    current_period,
    held_item,
    is_bedroom,
    is_dark,
    is_villain,
    skill_left,
)
from .prompts import render
from .providers import Player, chat, find_player, load_config
from .smoke import describe, load_personas

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
if sys.stdin.encoding and sys.stdin.encoding.lower() != "utf-8":
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
PERSONAS_PATH = Path(__file__).resolve().parent.parent / "personas.toml"

ROOM_CHOICES = PUBLIC_ROOMS  # 选房菜单中的公共房间；各人卧室单独列出


class Orchestrator:
    """单局编排器：持引擎与席位配置，按阶段驱动六席 AI。"""

    def __init__(self, game_dir: Path, hooks: dict | None = None):
        self.dir = game_dir
        self.state = json.loads((game_dir / "state.json").read_text(encoding="utf-8"))
        self.eng = Engine(self.state)
        self.players, _settings = load_config(str(game_dir / "players.toml"))
        self.personas = load_personas(PERSONAS_PATH)
        self.rules = (game_dir / "rules.md").read_text(encoding="utf-8")
        self.orch = self.state.setdefault("orch", {"feed": [], "cursor": {}, "private": {}})
        self._system_cache: dict[str, str] = {}
        self.hooks = hooks or {}

    def _fire(self, name: str, *args) -> None:
        """触发观察者钩子（console 订阅；无头模式不注册即为空操作）。"""
        cb = self.hooks.get(name)
        if cb:
            cb(*args)

    def seat(self, p: dict) -> str:
        return p["id"] or p["name"]

    def alive(self) -> list[dict]:
        return [p for p in self.state["players"] if p["alive"]]

    def player(self, name: str) -> dict | None:
        return next((p for p in self.state["players"] if p["name"] == name), None)

    def provider(self, p: dict) -> Player | None:
        return find_player(self.players, self.seat(p)) or find_player(self.players, p["name"])

    def system_prompt(self, p: dict) -> str:
        if p["name"] not in self._system_cache:
            card_path = self.dir / "private" / self.seat(p) / "card.md"
            card = card_path.read_text(encoding="utf-8") if card_path.exists() else "（私密卡缺失）"
            self._system_cache[p["name"]] = render(
                "game_system.md",
                name=p["name"],
                profile=describe(p["name"], self.personas),
                card=card,
                rules=self.rules,
            )
        return self._system_cache[p["name"]]

    def log_line(self, line: str) -> None:
        self.orch["feed"].append(line)
        with open(self.dir / "public" / "log.md", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def log_header(self, text: str) -> None:
        with open(self.dir / "public" / "log.md", "a", encoding="utf-8") as f:
            f.write(f"\n## {text}\n\n")

    def tell(self, name: str, msg: str) -> None:
        self.orch["private"].setdefault(name, []).append(msg)

    def route(self, ev: dict) -> None:
        """把引擎事件路由到公开信息流与各席私密队列。"""
        if not ev.get("ok"):
            return
        public = ev.get("public") or {}
        for line in public.get("announcements", []):
            self.log_line(f"- {line}")
        if "monitoring" in public:
            self.log_line(f"- 监控宣读：{json.dumps(public['monitoring'], ensure_ascii=False)}")
        for name, msg in (ev.get("private") or {}).items():
            self.tell(name, msg)

    def save_state(self) -> None:
        tmp = self.dir / "state.json.tmp"
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.dir / "state.json")  # 原子替换：中断也不会留下半个文件

    def host_log(self, player: str, stage: str, ok: bool, latency: float, usage: dict, error: str = "") -> None:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "player": player,
            "stage": stage,
            "ok": ok,
            "latency": round(latency, 2),
            "total_tokens": usage.get("total_tokens"),
        }
        if error:
            entry["error"] = error
        with open(self.dir / "host_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def extract_payload(text: str) -> dict:
        """从回复中提取 json 代码块；失败抛异常由调用方重试。"""
        m = re.search(r"```(?:json)?\s*(\{.*)", text, re.S)
        raw = m.group(1) if m else text
        obj, _ = json.JSONDecoder().raw_decode(raw.strip().rstrip("`").strip())
        if not isinstance(obj, dict):
            raise ValueError("顶层不是 JSON 对象")
        return obj

    @staticmethod
    def salvage_speech(text: str) -> str:
        m = re.search(r'"speech"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if not m:
            return ""
        try:
            return json.loads(f'"{m.group(1)}"')
        except Exception:
            return ""

    def call_ai(self, p: dict, label: str, briefing: str) -> dict:
        """一次席位调用：落盘简报与原始回复，解析三通道 JSON（失败重试 ≤2 次，仍失败记放弃行动）。"""
        prov = self.provider(p)
        seat_dir_in = self.dir / "inbox" / self.seat(p)
        seat_dir_out = self.dir / "outbox" / self.seat(p)
        seat_dir_in.mkdir(parents=True, exist_ok=True)
        seat_dir_out.mkdir(parents=True, exist_ok=True)
        day = self.state["phase"]["day"]
        (seat_dir_in / f"day{day}-{label}.md").write_text(briefing, encoding="utf-8")
        if prov is None:
            self.host_log(p["name"], label, False, 0.0, {}, "席位未在 players.toml 中找到")
            return {"speech": "", "inner": "", "notes": "", "actions": []}
        messages = [
            {"role": "system", "content": self.system_prompt(p)},
            {"role": "user", "content": briefing},
        ]
        token_cb = self.hooks.get("on_token")  # 传给 providers 走流式；无头模式为 None
        payload = None
        last_err = ""
        for attempt in range(3):
            self._fire("on_call_start", p["name"], label)
            result = chat(prov, messages, on_token=token_cb, reasoning_effort=prov.reasoning_effort)
            self._fire("on_call_end", p["name"], label, result)
            self.host_log(p["name"], label, result.ok, result.latency, result.usage, result.error)
            if not result.ok:
                last_err = result.error
                break
            (seat_dir_out / f"day{day}-{label}.md").write_text(result.content, encoding="utf-8")
            err = ""
            if result.finish_reason == "length":
                err = "你的上次回复过长被 max_tokens 截断，请大幅压缩 speech/inner/notes 的篇幅"
            else:
                try:
                    payload = self.extract_payload(result.content)
                    break
                except Exception as e:
                    err = (f"你的上次回复无法解析（{e}）。"
                           "请严格只输出一个 ```json 代码块，字段：speech/inner/notes/actions")
            last_err = err
            if attempt == 2:
                self.host_log(p["name"], label + "#parse", False, 0.0, {}, last_err)
                payload = {"speech": self.salvage_speech(result.content), "inner": "", "notes": "", "actions": []}
            else:
                messages = [
                    {"role": "system", "content": self.system_prompt(p)},
                    {"role": "user", "content": briefing + f"\n\n【主持人】{err}。"},
                ]
        if payload is None:
            print(f"  【{p['name']}】调用失败：{last_err}（记为放弃行动）")
            payload = {"speech": "", "inner": "", "notes": "", "actions": []}
        payload.setdefault("speech", "")
        payload.setdefault("inner", "")
        payload.setdefault("notes", "")
        actions = payload.get("actions")
        payload["actions"] = actions if isinstance(actions, list) else []
        self._fire("on_payload", p["name"], label, payload)
        self.archive_private(p, label, payload)
        return payload

    def archive_private(self, p: dict, label: str, payload: dict) -> None:
        seat = self.seat(p)
        day = self.state["phase"]["day"]
        inner = str(payload.get("inner") or "").strip()
        if inner:
            with open(self.dir / "private" / seat / "inner.md", "a", encoding="utf-8") as f:
                f.write(f"\n## 第{day}天 · {label}\n\n{inner}\n")
        notes = str(payload.get("notes") or "").strip()
        if notes:
            (self.dir / "private" / seat / "notes.md").write_text(notes + "\n", encoding="utf-8")

    def build_briefing(self, p: dict, title: str, menu: list[str], extra: str = "") -> str:
        """简报 = 增量公开事件 + 私密事件 + 位置与同房者 + notes 回放 + 合法动作菜单。"""
        name = p["name"]
        cursor = self.orch["cursor"].get(name, 0)
        feed = self.orch["feed"]
        new_public = feed[cursor:]
        self.orch["cursor"][name] = len(feed)
        private = self.orch["private"].pop(name, [])
        notes_path = self.dir / "private" / self.seat(p) / "notes.md"
        notes = notes_path.read_text(encoding="utf-8").strip() if notes_path.exists() else "（空）"
        pos = self.state["positions"].get(name, "会客室")
        roommates = []
        if pos and not is_dark(self.state, pos):
            roommates = [
                q["name"]
                for q in self.alive()
                if q["name"] != name and self.state["positions"].get(q["name"]) == pos
            ]
        lines = [f"# {title}", ""]
        lines.append("## 自你上次行动以来的公告")
        lines += [f"- {x}" for x in new_public] or ["- （无）"]
        lines.append("")
        lines.append("## 仅你可见的信息")
        lines += [f"- {x}" for x in private] or ["- （无）"]
        lines.append("")
        pos_line = f"你现在的位置：{pos}"
        if pos and pos != "会客室":
            pos_line += f"；同房者：{'、'.join(roommates) if roommates else '无'}"
            if is_dark(self.state, pos):
                pos_line += "（房间黑暗：你看不见这里有谁，也无法交谈）"
        lines.append(f"## 当前局面\n{pos_line}")
        lines.append("")
        lines.append(f"## 你的私人笔记（此前留存）\n{notes}")
        if extra:
            lines.append("")
            lines.append(extra)
        lines.append("")
        lines.append("## 本环节合法动作（JSON 示例）")
        lines += menu or ["- 本环节无动作可做，actions 返回 []"]
        return "\n".join(lines) + "\n"

    def reject_note(self, p: dict, ev: dict) -> None:
        self.tell(p["name"], f"你的动作被驳回：{ev.get('error', '未知原因')}")

    def apply_actions(self, p: dict, actions: list[dict]) -> None:
        """逐条派发动作到引擎；非法动作驳回并私下告知本人，不影响其余动作。"""
        for act in actions:
            if not isinstance(act, dict):
                continue
            self.dispatch(p, act)

    def dispatch(self, p: dict, act: dict) -> dict | None:
        name = p["name"]
        at = act.get("type", "")
        day = self.state["phase"]["day"]
        ev = None
        if at == "see_through":
            ev = self.eng.see_through(name, str(act.get("target", "")), act.get("claims") or {})
        elif at == "view_monitoring":
            ev = self.eng.view_monitoring(name, act.get("period"), act.get("room"))
        elif at == "tamper":
            ev = self.eng.tamper_monitoring(
                name, str(act.get("period", "")), str(act.get("room", "")),
                str(act.get("op", "")), str(act.get("name", "")), act.get("new_name"),
            )
        elif at == "open_safe":
            ev = self.eng.open_safe(name)
        elif at == "unlock":
            ev = self.eng.unlock(name, str(act.get("room", "")))
        elif at == "use_flashlight":
            ev = self.eng.use_flashlight(name)
        elif at == "fix_generator":
            ev = self.eng.fix_generator(name)
        elif at == "bug_listen":
            ev = self.eng.bug_listen(name)
        elif at == "seal_room":
            ev = self.eng.seal_room(name, act.get("allowed") or [])
        elif at == "use_passage":
            ev = self.eng.use_passage(name)
        elif at == "set_device":
            ev = self.eng.set_device(name, str(act.get("room", "")))
        elif at == "murder":
            ev = self.eng.murder(name, str(act.get("target", "")), act.get("weapon"), act.get("savior"))
        elif at == "stage_magic":
            ev = self.eng.stage_magic(name, str(act.get("target", "")))
        elif at == "declare_blackout":
            ev = self.eng.declare_blackout(name)
        elif at == "falsify":
            ev = self.eng.falsify(name, int(act.get("case_id", 0)), str(act.get("kind", "")))
        elif at == "move_body":
            ev = self.eng.move_body(name, int(act.get("case_id", 0)), str(act.get("to_room", "")))
        if ev is None:
            return None
        if not ev.get("ok"):
            self.reject_note(p, ev)
        else:
            self.route(ev)
        return ev

    def advance(self, title: str) -> dict:
        ev = self.eng.advance()
        self.log_header(title)
        self.route(ev)
        self.save_state()
        ph = self.state["phase"]
        print(f"== 进入 第{ph['day']}天 · {ph['stage']} ==")
        self._fire("on_stage", title)
        return ev

    def run_prep(self) -> None:
        """开局准备：仅反派建筑师（密道）与窃听器持有者（放置）需要声明。"""
        for p in self.alive():
            menu = []
            if is_villain(p) and p["profession"] == "建筑师" and skill_left(self.state, p["name"], "建造密道") > 0:
                menu.append('- 建造密道：{"type": "set_passage", "room": "书房|监控室|地下室"}')
            if held_item(p, "窃听器", 1) is not None and self.state["bug_room"] is None:
                menu.append('- 放置窃听器：{"type": "place_bug", "room": "书房|监控室|地下室"}')
            if not menu:
                continue
            briefing = self.build_briefing(p, "开局准备 · 秘密布置", menu,
                                         "开局前的秘密布置环节。这是你的开局义务，现在声明。")
            payload = self.call_ai(p, "开局准备", briefing)
            for act in payload["actions"]:
                if act.get("type") == "set_passage":
                    ev = self.eng.set_passage(p["name"], str(act.get("room", "")))
                    self.route(ev) if ev["ok"] else self.reject_note(p, ev)
                elif act.get("type") == "place_bug":
                    ev = self.eng.place_bug(p["name"], str(act.get("room", "")))
                    self.route(ev) if ev["ok"] else self.reject_note(p, ev)

    def run_speeches(self) -> None:
        """公共发言：按行动顺序每人一次；顺序靠后者可在简报中看到靠前者的发言。"""
        day = self.state["phase"]["day"]
        for name in self.state["order"]:
            p = self.player(name)
            if not p or not p["alive"]:
                continue
            menu = []
            if skill_left(self.state, name, "看破伪装") > 0 or held_item(p, "测谎仪", day) is not None:
                menu.append('- 看破伪装（自己发言前）：{"type": "see_through", "target": "玩家名", "claims": {"profession": "对方声称的职业", "item": "对方声称的道具", "visited_rooms": ["对方声称去过的房间"], "denied_rooms": ["对方声称没去过的房间"]}}')
            menu.append("- 发言写在 speech 字段；无需动作时 actions 为 []")
            briefing = self.build_briefing(p, f"第{day}天 · 公共发言（轮到你）", menu,
                                           "现在轮到你公开发言。分析、指控、质疑、声明、辩护均可。")
            payload = self.call_ai(p, "公共发言", briefing)
            speech = str(payload.get("speech") or "").strip()
            if speech:
                self.log_line(f"**{name}**：{speech}")
            self.apply_actions(p, payload["actions"])

    def run_exchange(self) -> None:
        """交换环节：按顺序每人可发起一次赠送；接受/拒绝由接收方决定。"""
        day = self.state["phase"]["day"]
        for name in self.state["order"]:
            p = self.player(name)
            if not p or not p["alive"]:
                continue
            held = [e["name"] for e in p["items"] if e["uses_left"] > 0 and e["available_day"] <= day]
            menu = ['- 赠送道具（每人限发起一次）：{"type": "gift", "to": "玩家名", "item": "道具名"}',
                    "- 不赠送则 actions 为 []"]
            briefing = self.build_briefing(
                p, f"第{day}天 · 交换环节", menu,
                f"你当前持有道具：{'、'.join(held) if held else '无'}。可向一名玩家发起赠送（对方可拒绝）。")
            payload = self.call_ai(p, "交换环节", briefing)
            for act in payload["actions"]:
                if act.get("type") != "gift":
                    self.dispatch(p, act)
                    continue
                target = self.player(str(act.get("to", "")))
                item = str(act.get("item", ""))
                if target is None or not target["alive"] or not item:
                    continue
                ask = self.build_briefing(
                    target, f"第{day}天 · 交换环节（赠送要约）",
                    ['- 接受：{"type": "accept"}', '- 拒绝：{"type": "decline"}'],
                    f"{name} 想把「{item}」赠送给你。是否接受？")
                reply = self.call_ai(target, f"交换环节-回复{name}", ask)
                accept = any(a.get("type") == "accept" for a in reply["actions"])
                ev = self.eng.transfer(name, target["name"], item, accept)
                if ev["ok"]:
                    self.route(ev)
                else:
                    self.reject_note(p, ev)

    def run_free_activity(self) -> None:
        """自由活动：停电声明并入选房菜单 → 秘密选房+邀请 → 结算 → 房间交谈 → 房间行动。"""
        day = self.state["phase"]["day"]
        period = current_period(self.state)
        if self.state["rooms_entered"] != period:
            self.select_rooms(day, period)
        if self.state["rooms_entered"] != period:
            return
        self.room_talks(day)
        self.room_actions(day)

    def select_rooms(self, day: int, period: str) -> None:
        assignments: dict[str, str] = {}
        pending_invite: dict[str, str] = {}
        alive_names = [p["name"] for p in self.alive()]
        rooms_menu = (
            [f'- 选择房间（必选一项）：{{"type": "choose_room", "room": "房间名"}}；可选：{"、".join(PUBLIC_ROOMS)}，或任何人的卧室（如 "{bedroom_of(alive_names[0])}"）',
             '- 邀请一名顺序在你之后的玩家同入（对方可改选）：{"type": "invite", "target": "玩家名"}']
        )
        for i, name in enumerate(self.state["order"]):
            p = self.player(name)
            if not p or not p["alive"]:
                continue
            menu = list(rooms_menu)
            if is_villain(p) and (skill_left(self.state, name, "暴力破解") > 0 or held_item(p, "螺丝起子", day) is not None):
                menu.append('- 秘密声明今天停电：{"type": "declare_blackout"}')
            later = [n for n in self.state["order"][i + 1:] if n in alive_names]
            extra = f"你可以邀请顺序在你之后的玩家同入：{'、'.join(later) if later else '无'}。"
            inviters = [v for t, v in pending_invite.items() if t == name]
            for v in inviters:
                extra += f"\n{v} 邀请你同往 {assignments.get(v, '（未知）')}——接受就把 choose_room 选为同一房间，不接受就自选。"
            briefing = self.build_briefing(p, f"第{day}天 · 自由活动（选房）", menu, extra)
            payload = self.call_ai(p, "自由活动-选房", briefing)
            room = bedroom_of(name)
            for act in payload["actions"]:
                at = act.get("type")
                if at == "choose_room":
                    room = str(act.get("room", room))
                elif at == "invite":
                    tgt = str(act.get("target", ""))
                    if tgt in later:
                        pending_invite[tgt] = name
                elif at == "declare_blackout":
                    ev = self.eng.declare_blackout(name)
                    self.route(ev) if ev["ok"] else self.reject_note(p, ev)
            assignments[name] = room
        invited = {}
        for t, v in pending_invite.items():
            if t in assignments and v in assignments and assignments[t] == assignments[v] == bedroom_of(v):
                invited[t] = v
        for _ in range(len(alive_names) + 2):
            ev = self.eng.enter_rooms(assignments, invited)
            if ev["ok"]:
                self.route(ev)
                return
            bad_room = next((r for r in self.all_rooms() if r in ev.get("error", "")), None)
            if bad_room is None:
                stranger = [n for n in alive_names if assignments.get(n) not in self.all_rooms()]
                for n in stranger:
                    assignments[n] = bedroom_of(n)
                if stranger:
                    continue
                print(f"  选房结算失败且无法回退：{ev.get('error')}")
                return
            movable = [n for n in reversed(self.state["order"]) if n in alive_names
                       and assignments.get(n) == bad_room and bedroom_of(n) != bad_room]
            if not movable:
                print(f"  选房结算失败且无法回退：{ev.get('error')}")
                return
            assignments[movable[0]] = bedroom_of(movable[0])
        print("  选房结算多次失败，已按回退处理")

    def all_rooms(self) -> list[str]:
        return PUBLIC_ROOMS + [bedroom_of(p["name"]) for p in self.state["players"]]

    def room_talks(self, day: int) -> None:
        positions = self.state["positions"]
        talked_rooms = set()
        for name in self.state["order"]:
            room = positions.get(name)
            if not room or room == "会客室" or room in talked_rooms:
                continue
            talked_rooms.add(room)
            occ = [n for n in self.state["order"]
                   if positions.get(n) == room and (self.player(n) or {}).get("alive")]
            if len(occ) < 2 or is_dark(self.state, room):
                continue
            if any(c["day"] == day and c["stage"] == "自由活动" and c["room"] == room for c in self.state["cases"]):
                continue
            transcript: list[str] = []
            for occ_name in occ:
                p = self.player(occ_name)
                others = [n for n in occ if n != occ_name]
                menu = ["- 本环节是房间内交谈：speech 只对同房者可见；actions 为 []"]
                extra = f"你在 {room}，同房者：{'、'.join(others)}。"
                if transcript:
                    extra += "\n本房间已发生的交谈：\n" + "\n".join(transcript)
                briefing = self.build_briefing(p, f"第{day}天 · 自由活动（{room}内交谈）", menu, extra)
                payload = self.call_ai(p, f"自由活动-交谈-{room}", briefing)
                speech = str(payload.get("speech") or "").strip()
                if speech:
                    transcript.append(f"{occ_name}：{speech}")
            if transcript:
                for occ_name in occ:
                    seat = self.seat(self.player(occ_name))
                    with open(self.dir / "private" / seat / "log.md", "a", encoding="utf-8") as f:
                        f.write(f"\n## 第{day}天 · {room}内交谈\n\n" + "\n".join(transcript) + "\n")
                    self.tell(occ_name, f"{room}内交谈：" + "；".join(transcript))

    def room_actions(self, day: int) -> None:
        for name in self.state["order"]:
            p = self.player(name)
            if not p or not p["alive"]:
                continue
            menu = self.action_menu(p, day)
            if not menu:
                continue
            briefing = self.build_briefing(p, f"第{day}天 · 自由活动（房间行动）", menu,
                                           "现在可使用房间设施或道具/技能。无动作可做则 actions 为 []。")
            payload = self.call_ai(p, "自由活动-房间行动", briefing)
            self.apply_actions(p, payload["actions"])

    def action_menu(self, p: dict, day: int) -> list[str]:
        """按当前位置与持有物列出合法房间行动菜单（引擎仍是最终裁决者）。"""
        s = self.state
        name = p["name"]
        room = s["positions"].get(name)
        menu: list[str] = []
        if not room or room == "会客室":
            return menu
        if room == "监控室":
            menu.append('- 查看监控：{"type": "view_monitoring"}（可加 "period"/"room" 过滤）')
            if is_villain(p) and (skill_left(s, name, "信号干扰") > 0 or held_item(p, "病毒U盘", day) is not None):
                menu.append('- 篡改一条监控记录：{"type": "tamper", "period": "第N天自由活动", "room": "场所", "op": "add|remove|replace", "name": "进入者", "new_name": "替换为"}')
        if room == "书房" and not s["safe_open"]:
            menu.append('- 打开保险箱：{"type": "open_safe"}（需要开锁手段）')
        if s["locks"].get(room):
            menu.append(f'- 打开当前房间的门锁：{{"type": "unlock", "room": "{room}"}}')
        if is_dark(s, room) and held_item(p, "手电筒", day) is not None:
            menu.append('- 使用手电筒（本房间本次不视为黑暗）：{"type": "use_flashlight"}')
        if room == "地下室" and s["power_out"]:
            menu.append('- 修复发电机：{"type": "fix_generator"}')
        if room == bedroom_of(name) and s["bug_room"] and held_item(p, "窃听器", day) is not None:
            menu.append('- 收听窃听器：{"type": "bug_listen"}')
        if p["profession"] == "建筑师" and skill_left(s, name, "封闭空间") > 0:
            menu.append('- 封闭空间（锁死当前房间至下一自由活动）：{"type": "seal_room", "allowed": ["许可留下的人"]}')
        if is_villain(p) and p["profession"] == "建筑师" and s["passage"] and room == bedroom_of(name):
            menu.append('- 使用密道：{"type": "use_passage"}')
        if is_villain(p) and p["profession"] == "机械师" and day >= 2:
            menu.append('- 设置延时装置：{"type": "set_device", "room": "当前房间名"}')
        if day >= 2 and p["identity"] == "杀手":
            targets = [q["name"] for q in self.alive()
                       if q["name"] != name and s["positions"].get(q["name"]) == room and not is_villain(q)]
            if targets:
                menu.append(f'- 谋杀（每阶段一次）：{{"type": "murder", "target": "{"|".join(targets)}"}}（可加 "weapon": "凶器名" 或环境凶器）')
        return menu

    def run_night(self) -> None:
        """晚间休息：留宿邀请与夜间房间行动（互相不可见选择），随后结算并执行夜间房间行动。"""
        day = self.state["phase"]["day"]
        period = current_period(self.state)
        if self.state["night_assigned"] == period:
            return
        assignments: dict[str, str] = {}
        night_do: dict[str, str] = {}
        pending_invite: dict[str, str] = {}
        invited: dict[str, str] = {}
        alive_names = [p["name"] for p in self.alive()]
        for i, name in enumerate(self.state["order"]):
            p = self.player(name)
            if not p or not p["alive"]:
                continue
            later = [n for n in self.state["order"][i + 1:] if n in alive_names]
            menu = [
                '- 回自己卧室过夜（默认）：{"type": "stay"}',
                '- 邀请一名顺序在你之后的玩家来你卧室过夜：{"type": "invite", "target": "玩家名"}',
                '- 不回卧室，夜间前往书房/监控室/地下室执行一项行动：{"type": "night_room", "room": "书房|监控室|地下室", "do": "open_safe|view_monitoring|fix_generator|unlock"}',
            ]
            extra = "夜间行动互相不可见。每个夜间房间只能进入 1 人（顺序靠后冲突者失败，默认回自己卧室）。"
            extra += "\n注意：上面「当前局面」里的位置是你自由活动结束时的实际位置；本环节决定的是**今晚去哪过夜**——不回卧室就选 night_room，什么都不做默认回自己卧室。"
            if later:
                extra += f"\n顺序在你之后的玩家：{'、'.join(later)}。"
            if name in pending_invite:
                v = pending_invite[name]
                menu.append(f'- 接受 {v} 的留宿邀请：{{"type": "accept_invite"}}')
                extra += f"\n{v} 邀请你今晚去 {bedroom_of(v)} 过夜。"
            briefing = self.build_briefing(p, f"第{day}天 · 晚间休息", menu, extra)
            payload = self.call_ai(p, "晚间休息", briefing)
            for act in payload["actions"]:
                at = act.get("type")
                if at == "invite":
                    tgt = str(act.get("target", ""))
                    if tgt in later:
                        pending_invite[tgt] = name
                elif at == "accept_invite" and name in pending_invite:
                    assignments[name] = bedroom_of(pending_invite[name])
                    invited[name] = pending_invite[name]
                elif at == "night_room":
                    room = str(act.get("room", ""))
                    if room in PUBLIC_ROOMS:
                        assignments[name] = room
                        night_do[name] = str(act.get("do", ""))
        ev = self.eng.night_assign(assignments, invited)
        if ev["ok"]:
            self.route(ev)
        else:
            print(f"  晚间归宿结算失败：{ev.get('error')}")
            return
        for name, do in night_do.items():
            if self.state["positions"].get(name) != assignments.get(name):
                continue
            p = self.player(name)
            if p is None or not p["alive"]:
                continue
            self.dispatch(p, {"type": do, "room": assignments[name]})


    def run_day(self) -> None:
        """主循环：从当前 state.phase 推进到第 2 天清晨或游戏结束（每阶段落盘，可 --resume）。

        无头模式由 main() 直接调用；console 在工作线程里调用并提供 hooks。
        """
        while True:
            day, stage = self.state["phase"]["day"], self.state["phase"]["stage"]
            if day >= 2 or self.state["winner"]:
                break
            gate = self.hooks.get("pause_gate")
            if gate:
                gate()  # 阶段边界检查点：console 暂停时阻塞在此
            stop = self.hooks.get("should_stop")
            if stop and stop():
                print("收到退出指令，已在阶段边界安全停止（可用 --resume 续跑）")
                break
            if stage == "开局准备":
                self.run_prep()
                self.advance(f"第{day}天 · 公共发言")
            elif stage == "公共发言":
                self.run_speeches()
                self.run_exchange()
                self.advance(f"第{day}天 · 自由活动")
            elif stage == "自由活动":
                self.run_free_activity()
                self.advance(f"第{day}天 · 晚间休息")
            elif stage == "晚间休息":
                self.run_night()
                self.advance(f"第{day + 1}天 · 清晨")
            else:
                self.advance(f"第{day}天 · {stage}")
            self.save_state()
        self.save_state()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True, help="对局目录（deal.py 产物，如 game/012345）")
    ap.add_argument("--resume", action="store_true", help="从 state.json 断点续跑（阶段粒度）")
    args = ap.parse_args()

    game_dir = Path(args.game)
    if not (game_dir / "state.json").exists():
        raise SystemExit(f"找不到 {game_dir / 'state.json'}（先用 python -m src.deal 生成对局）")
    orch = Orchestrator(game_dir)
    ph = orch.state["phase"]
    print(f"对局 {game_dir}：从 第{ph['day']}天 · {ph['stage']} 开始{'（续跑）' if args.resume else ''}")
    if not args.resume:
        orch.log_header(f"第{ph['day']}天 · 开局")
    try:
        orch.run_day()
    except KeyboardInterrupt:
        print("\n已中断（Ctrl+C）。进度已保存至最近阶段边界，可用 --resume 续跑。")
        raise SystemExit(130)
    ph = orch.state["phase"]
    print(f"第 1 天结束：state.phase = 第{ph['day']}天 · {ph['stage']}；winner = {orch.state['winner']}")

if __name__ == "__main__":
    main()
