"""编排器主循环（M4/M5）：按阶段生成简报 → 收集三通道 JSON → 引擎裁决 → 落盘。

用法：
    python -m src.orchestrator --game game/012345            # 跑完整局（直至产生胜者或主神结局）
    python -m src.orchestrator --game game/012345 --resume   # 从 state.json 断点续跑（席位行动粒度）

一天流程：开局准备（仅第 1 天：反派建筑师声明密道、窃听器持有者声明放置）→ 公共发言
（侦探最后发言）→ 交换环节 → 投票（第 2 天起：收票/记忆植入/遗言先于身份公布/封存道具分配/
第 4 天起武装指认）→ 自由活动（停电声明并入选房菜单；秘密选房+同入邀请；引擎结算；房间交谈；
房间行动；阶段末毁证窗口）→ 晚间休息（留宿邀请/夜间房间行动/杀手夜袭，互相不可见选择）
→ advance() 进入次日案件调查（清晨公告/调查自荐/侦探监控特权）。终局（任一胜负条件满足）
自动收束并落盘结局。

消息结构：system = render(game_system.md)（人设+规则书+私密卡），user = 当次简报（增量公告 +
增量私密事件 + 关键记录档案 + notes 回放 + 合法动作菜单）；对话不累积，每阶段独立调用。
全部顺序执行，不并发。观察者钩子（console 订阅；无头模式全为空操作）：on_call_start /
on_token / on_call_end / on_stage / should_stop（席位间隙安全暂停：pause/Ctrl+C/quit 共用）。

中断与续跑（席位行动粒度）：
- 每个席位行动完成后立即落盘 state.json（含 orch.wip 环节进度）；
- API 请求失败（providers 内部重试耗尽）→ 落盘并抛 AbortRun，对局中断，不再"记为放弃行动"；
- Ctrl+C → 落盘后透传；console 的 quit 在席位间隙触发 SoftStop 安全收束；
- --resume 重入当前环节，跳过 wip.done 里的已完成席位，被中断席位用相同简报重问
  （feed 游标与私密队列在 call_ai 成功后才提交，见 _commit_feed）。

落盘（game/<id>/）：
  public/log.md                    公告/发言/交换/死亡公告（按阶段标题追加）
  private/<id>/inner.md            内心独白（追加）；private/<id>/notes.md 私人笔记（每阶段覆盖）
  private/<id>/log.md              该席听到的房间内交谈存档
  inbox/<id>/day{N}-<环节>.md      简报存档；outbox/<id>/ 原始回复存档
  host_log.jsonl                   每次 API 调用 {ts, player, stage, ok, latency, total_tokens, error}
  state.json                       每个席位行动后落盘（断点续跑）
  script.md                        主持人侧全记录（背景/分配/发言/行动/裁决，不含内心独白）

state 扩展（宿主侧，引擎字段之外新增）：
  state["orch"] = {"feed": [公开事件行], "cursor": {名字: 已读游标}, "private": {名字: [待送达私密事件]},
                   "wip": {环节key(天:阶段:子环节): 席位粒度断点}, "hist": {名字: [私密事件历史，简报"关键记录"用]}}
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
    bedroom_owner,
    current_period,
    held_item,
    held_item_names,
    is_bedroom,
    is_dark,
    is_villain,
    skill_left,
)
from .gamedata import WEAPONS, physique_desc
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

COVERUP_SKILLS = ("伪造痕迹", "销毁证物", "伪造证物", "转移尸体")  # 毁证类反派技能（道具等价物：可疑的粉末/裹尸袋）

# 开局背景叙述：console 启动画面与 script.md 全记录共用（仅此一份）
OPENING = """\
暴风雨之夜。
你们在一座陌生古堡的石厅中醒来——没有人记得自己是谁、从何而来。
壁炉里的火烧得正旺，长桌上放着一本规则书，和一张主人留下的字条。
你们之中，混着杀手与帮凶。"""


class AbortRun(RuntimeError):
    """API 调用失败：状态已落盘，整个对局立即中断（修复后用 --resume 从被中断席位续跑）。"""


class SoftStop(Exception):
    """收到退出指令：在当前席位行动完成后安全停止（内部控制流，由 run_game 捕获收束）。"""


class Orchestrator:
    """单局编排器：持引擎与席位配置，按阶段驱动六席 AI。"""

    def __init__(self, game_dir: Path, hooks: dict | None = None):
        self.dir = game_dir
        self.state = json.loads((game_dir / "state.json").read_text(encoding="utf-8"))
        self.eng = Engine(self.state)
        self.players, _settings = load_config(str(game_dir / "players.toml"))
        self.personas = load_personas(PERSONAS_PATH)
        self.rules = (game_dir / "rules.md").read_text(encoding="utf-8")
        self.orch = self.state.setdefault("orch", {})
        self.orch.setdefault("feed", [])
        self.orch.setdefault("cursor", {})
        self.orch.setdefault("private", {})
        self.orch.setdefault("wip", {})   # 当前环节的席位粒度断点（done 名单 + 中间累计）
        self.orch.setdefault("hist", {})  # 各席私密事件历史（简报"关键记录"段的数据源）
        self._system_cache: dict[str, str] = {}
        self._pending: dict[str, int] = {}  # 席位名 → 待提交的 feed 游标（call_ai 成功后才提交）
        self.hooks = hooks or {}
        self.script_path = self.dir / "script.md"
        if not self.script_path.exists():
            self._init_script()

    # ---- script.md：主持人侧全记录（背景/阵容分配/全部发言与行动/裁决结果，不含内心独白）----

    def _init_script(self) -> None:
        """写全记录开头：背景 + 阵容分配（全剧透）+ 开局公告。仅在 script.md 不存在时调用。"""
        s = self.state
        lines = [
            f"# 《暴风夜》对局全记录 · seed {s.get('seed', '?')}",
            "",
            "> 主持人侧完整流程记录（含身份与全部秘密行动），不含内心独白。",
            "> 公开口径见 public/log.md；内心独白与私人笔记见 private/<席位>/。",
            "",
            "## 背景",
            "",
            OPENING,
            "",
            "## 阵容与分配（全剧透）",
            "",
            "| 角色 | 性别 | 体格 | 国籍 | 身份 | 职业 | 初始道具 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for p in s["players"]:
            items = "、".join(f"{e['name']}×{e['uses_left']}" for e in p.get("items", [])) or "无"
            lines.append(f"| {p['name']} | {p['gender']} | {physique_desc(p['physique'])} | "
                         f"{p['nationality']} | {p['identity']} | {p['profession']} | {items} |")
        lines += [
            "",
            f"- 首日行动顺序：{' → '.join(s['order'])}",
            f"- 书房保险箱内藏凶器：{s.get('safe_item', '?')}（玩家只知有凶器，不知具体哪件）",
            "",
        ]
        ann = self.dir / "public" / "announcement.md"
        if ann.exists():
            lines += ["## 开局公告（主人字条）", "", ann.read_text(encoding="utf-8").strip(), ""]
        self.script_path.write_text("\n".join(lines), encoding="utf-8")

    def script_line(self, line: str) -> None:
        with open(self.script_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def script_header(self, text: str) -> None:
        with open(self.script_path, "a", encoding="utf-8") as f:
            f.write(f"\n## {text}\n\n")

    def script_private(self, ev: dict) -> None:
        """把事件的私密回执逐条记入全记录（host 侧事实，不进 public/log.md）。"""
        for name, msg in (ev.get("private") or {}).items():
            if not isinstance(msg, str):
                msg = json.dumps(msg, ensure_ascii=False)
            self.script_line(f"  [{name}] {msg}")

    def script_action(self, name: str, act: dict, ev: dict) -> None:
        """记录一次动作声明与裁决结果（含驳回——host 侧可见，不公开）。"""
        line = f"- {name} 声明 {json.dumps(act, ensure_ascii=False)}"
        if not ev.get("ok"):
            line += f" → 驳回（{ev.get('error')}）"
        else:
            priv = (ev.get("private") or {}).get(name)
            if isinstance(priv, str) and priv:
                line += f" → 成功：{priv}"
            elif priv:
                line += f" → 成功：{json.dumps(priv, ensure_ascii=False)}"
            else:
                line += " → 成功"
        self.script_line(line)

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
        self.script_line(line)

    def log_header(self, text: str) -> None:
        with open(self.dir / "public" / "log.md", "a", encoding="utf-8") as f:
            f.write(f"\n## {text}\n\n")
        self.script_header(text)

    def tell(self, name: str, msg) -> None:
        """私密事件：入队待送达（下次简报"仅你可见"）+ 写入该席私密历史（简报"关键记录"段）。"""
        if not isinstance(msg, str):
            msg = json.dumps(msg, ensure_ascii=False)
        self.orch["private"].setdefault(name, []).append(msg)
        self.orch["hist"].setdefault(name, []).append(msg)

    def route(self, ev: dict) -> None:
        """把引擎事件路由到公开信息流与各席私密队列。"""
        if not ev.get("ok"):
            return
        public = ev.get("public") or {}
        for line in public.get("announcements", []):
            self.log_line(f"- {line}")
        if "monitoring" in public:
            self.log_line(f"- 监控宣读：{json.dumps(public['monitoring'], ensure_ascii=False)}")
        if "granted" in public or "destroyed" in public:
            granted = public.get("granted") or {}
            parts = [f"{it}→{who}" for who, items in granted.items() for it in items]
            parts += [f"{it}→销毁" for it in (public.get("destroyed") or [])]
            if parts:
                self.log_line(f"- 封存道具分配结果：{'、'.join(parts)}")
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
        """一次席位调用：落盘简报与原始回复，解析三通道 JSON（解析失败重试 ≤2 次）。

        中断语义（席位粒度断点的根基）：
        - API 请求失败（providers 内部重试已耗尽）→ 落盘 state 并抛 AbortRun，对局中断；
        - Ctrl+C → 同样先落盘再透传；
        - 只有拿到可用 payload 才提交该席的 feed 游标与私密队列（_commit_feed），
          因此被中断的席位在 --resume 时会收到与中断前完全相同的增量信息。
        """
        prov = self.provider(p)
        seat_dir_in = self.dir / "inbox" / self.seat(p)
        seat_dir_out = self.dir / "outbox" / self.seat(p)
        seat_dir_in.mkdir(parents=True, exist_ok=True)
        seat_dir_out.mkdir(parents=True, exist_ok=True)
        day = self.state["phase"]["day"]
        (seat_dir_in / f"day{day}-{label}.md").write_text(briefing, encoding="utf-8")
        if prov is None:
            self.host_log(p["name"], label, False, 0.0, {}, "席位未在 players.toml 中找到")
            self.save_state()
            raise AbortRun(f"{p['name']} 的席位未在 players.toml 中找到，无法调用")
        messages = [
            {"role": "system", "content": self.system_prompt(p)},
            {"role": "user", "content": briefing},
        ]
        token_cb = self.hooks.get("on_token")  # 传给 providers 走流式；无头模式为 None
        payload = None
        try:
            for attempt in range(3):
                self._fire("on_call_start", p["name"], label)
                result = chat(prov, messages, on_token=token_cb, reasoning_effort=prov.reasoning_effort)
                self._fire("on_call_end", p["name"], label, result)
                self.host_log(p["name"], label, result.ok, result.latency, result.usage, result.error)
                if not result.ok:
                    self.save_state()
                    raise AbortRun(f"{p['name']} · {label}：API 调用失败（{result.error}）")
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
                if attempt == 2:
                    self.host_log(p["name"], label + "#parse", False, 0.0, {}, err)
                    payload = {"speech": self.salvage_speech(result.content), "inner": "", "notes": "", "actions": []}
                else:
                    messages = [
                        {"role": "system", "content": self.system_prompt(p)},
                        {"role": "user", "content": briefing + f"\n\n【主持人】{err}。"},
                    ]
        except KeyboardInterrupt:
            self.save_state()
            raise
        assert payload is not None
        self._commit_feed(p["name"])
        payload.setdefault("speech", "")
        payload.setdefault("inner", "")
        payload.setdefault("notes", "")
        actions = payload.get("actions")
        payload["actions"] = actions if isinstance(actions, list) else []
        self._fire("on_payload", p["name"], label, payload)
        self.archive_private(p, label, payload)
        return payload

    def _commit_feed(self, name: str) -> None:
        """席位调用成功后提交信息游标：公告游标前移 + 私密事件队列清空（各只送达一次）。"""
        n = self._pending.pop(name, None)
        if n is not None:
            self.orch["cursor"][name] = n
            self.orch["private"].pop(name, None)

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

    @staticmethod
    def _clip(line: str, limit: int = 200) -> str:
        """关键记录逐条截断：房间交谈等长文本只留开头提示，全文在 private/<席>/log.md。"""
        return line if len(line) <= limit else line[:limit] + "…"

    @staticmethod
    def _bullet(line: str) -> str:
        """统一列表项前缀（feed 里的公告行自带 "- "，避免叠成双破折号）。"""
        return "- " + (line[2:] if line.startswith("- ") else line)

    def build_briefing(self, p: dict, title: str, menu: list[str], extra: str = "") -> str:
        """简报 = 增量公开事件 + 增量私密事件 + 当前局面 + 关键记录 + notes 回放 + 合法动作菜单。

        本方法不改任何状态：feed 游标与私密队列由 call_ai 成功后才提交（_commit_feed），
        因此被中断/失败的席位续跑时会收到与本次完全相同的增量信息。
        关键记录段：公开档案取 feed 历史（剔除发言行——发言要点由角色自己记笔记）；
        私密档案取该席私密事件历史（剔除本次增量部分，避免与同简报"仅你可见"重复）。
        """
        name = p["name"]
        cursor = self.orch["cursor"].get(name, 0)
        feed = self.orch["feed"]
        new_public = feed[cursor:]
        private = list(self.orch["private"].get(name, []))
        self._pending[name] = len(feed)
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
        pub_hist = [self._bullet(self._clip(x)) for x in feed[:cursor] if not x.startswith("**")]
        hist = self.orch["hist"].get(name, [])
        priv_hist = hist[: len(hist) - len(private)] if private else hist
        lines = [f"# {title}", ""]
        lines.append("## 自你上次行动以来的公告")
        lines += [self._bullet(x) for x in new_public] or ["- （无）"]
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
        lines.append("## 截至目前的关键记录（主持人档案：既定事实流水，不含任何发言内容——发言与推理要点请记在你的笔记里）")
        lines.append("### 公共记录")
        lines += pub_hist or ["- （暂无）"]
        lines.append("### 与你相关的私密记录")
        lines += [f"- {self._clip(x)}" for x in priv_hist] or ["- （暂无）"]
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
        elif at == "investigate":
            ev = self.eng.investigate(name, int(act.get("case_id", 0)), str(act.get("choice", "")))
        elif at == "profile":
            ev = self.eng.profile(name, int(act.get("case_id", 0)), str(act.get("aspect", "")))
        elif at == "detective_monitoring":
            ev = self.eng.detective_monitoring(
                name, str(act.get("period", "")), [str(r) for r in (act.get("rooms") or [])])
        if ev is None:
            return None
        self.script_action(name, act, ev)
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

    def wip(self, key: str, **defaults) -> dict:
        """指定环节（key = 天:阶段:子环节）的席位粒度工作数据：存在则取回（续跑），否则初始化。

        同一阶段的各子环节断点并存（如"公共发言:发言"与"公共发言:交换"），续跑重入阶段时
        每个 runner 都能跳到自己的断点继续。约定：每席处理完立刻写 done 并 save_state()
        ——那一刻的落盘就是断点；中断后 --resume 重入 runner，跳过 done 席位继续。
        """
        wips = self.orch["wip"]
        if key not in wips:
            wips[key] = dict(defaults)
        return wips[key]

    def check_stop(self) -> None:
        """席位行动间隙检查退出指令（console 的 quit/Ctrl+C）；触发则抛 SoftStop 由 run_game 收束。"""
        cb = self.hooks.get("should_stop")
        if cb and cb():
            raise SoftStop

    def run_prep(self) -> None:
        """开局准备：仅反派建筑师（密道）与窃听器持有者（放置）需要声明。"""
        day = self.state["phase"]["day"]
        w = self.wip(f"{day}:开局准备:布置", done=[])
        for p in self.alive():
            if p["name"] in w["done"]:
                continue
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
                    self.script_action(p["name"], act, ev)
                    self.route(ev) if ev["ok"] else self.reject_note(p, ev)
                elif act.get("type") == "place_bug":
                    ev = self.eng.place_bug(p["name"], str(act.get("room", "")))
                    self.script_action(p["name"], act, ev)
                    self.route(ev) if ev["ok"] else self.reject_note(p, ev)
            w["done"].append(p["name"])
            self.save_state()
            self.check_stop()

    def run_speeches(self) -> None:
        """公共发言：按行动顺序每人一次（侦探最后发言，规则 7.2）；顺序靠后者可在简报中看到靠前者的发言。"""
        day = self.state["phase"]["day"]
        det = self.state.get("detective")
        speech_order = [n for n in self.state["order"] if n != det]
        if det and (self.player(det) or {}).get("alive"):
            speech_order.append(det)
        w = self.wip(f"{day}:公共发言:发言", done=[])
        for name in speech_order:
            if name in w["done"]:
                continue
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
            w["done"].append(name)
            self.save_state()
            self.check_stop()

    def run_exchange(self) -> None:
        """交换环节：按顺序每人可发起一次赠送；接受/拒绝由接收方决定。"""
        day = self.state["phase"]["day"]
        w = self.wip(f"{day}:公共发言:交换", done=[])
        for name in self.state["order"]:
            if name in w["done"]:
                continue
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
                self.script_line(f"- {name} 向 {target['name']} 赠送「{item}」→ "
                                 f"{'接受' if accept else '拒绝'}" + ("" if ev["ok"] else f"（失败：{ev.get('error')}）"))
                if ev["ok"]:
                    self.route(ev)
                else:
                    self.reject_note(p, ev)
            w["done"].append(name)
            self.save_state()
            self.check_stop()

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
        w = self.wip(f"{day}:自由活动:选房", done=[], assignments={}, invites={})
        assignments: dict[str, str] = dict(w["assignments"])
        pending_invite: dict[str, str] = dict(w["invites"])
        alive_names = [p["name"] for p in self.alive()]
        rooms_menu = (
            [f'- 选择房间（必选一项）：{{"type": "choose_room", "room": "房间名"}}；可选：{"、".join(PUBLIC_ROOMS)}，或任何人的卧室（如 "{bedroom_of(alive_names[0])}"）',
             '- 邀请一名顺序在你之后的玩家同入（对方可改选）：{"type": "invite", "target": "玩家名"}']
        )
        for i, name in enumerate(self.state["order"]):
            p = self.player(name)
            if not p or not p["alive"] or name in w["done"]:
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
            w["done"].append(name)
            w["assignments"] = dict(assignments)
            w["invites"] = dict(pending_invite)
            self.save_state()
            self.check_stop()
        invited = {}
        for t, v in pending_invite.items():
            if t in assignments and v in assignments and assignments[t] == assignments[v] == bedroom_of(v):
                invited[t] = v
        for _ in range(len(alive_names) + 2):
            ev = self.eng.enter_rooms(assignments, invited)
            if ev["ok"]:
                self.route(ev)
                self.script_line("- 选房结果：" + "、".join(f"{n}→{r}" for n, r in assignments.items()))
                if invited:
                    self.script_line("  同入邀请成立：" + "、".join(f"{t} 随 {v}" for t, v in invited.items()))
                self.script_private(ev)
                for v in ev.get("device_triggers", []):
                    self.script_line(f"  延时装置触发：{v}")
                self.save_state()
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
        w = self.wip(f"{day}:自由活动:交谈", done_rooms=[])
        for name in self.state["order"]:
            room = positions.get(name)
            if not room or room == "会客室" or room in w["done_rooms"]:
                continue
            occ = [n for n in self.state["order"]
                   if positions.get(n) == room and (self.player(n) or {}).get("alive")]
            if len(occ) < 2 or is_dark(self.state, room):
                continue
            if any(c["day"] == day and c["stage"] == "自由活动" and c["room"] == room for c in self.state["cases"]):
                continue
            # 一个房间是一次原子 checkpoint：中途被打断则整间重谈（transcript 只在谈妥后才投递）
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
                self.script_line(f"- {room}内交谈（{'、'.join(occ)}）：")
                for line in transcript:
                    self.script_line(f"    {line}")
                for occ_name in occ:
                    seat = self.seat(self.player(occ_name))
                    with open(self.dir / "private" / seat / "log.md", "a", encoding="utf-8") as f:
                        f.write(f"\n## 第{day}天 · {room}内交谈\n\n" + "\n".join(transcript) + "\n")
                    self.tell(occ_name, f"{room}内交谈：" + "；".join(transcript))
            w["done_rooms"].append(room)
            self.save_state()
            self.check_stop()

    def room_actions(self, day: int) -> None:
        w = self.wip(f"{day}:自由活动:房间行动", done=[])
        for name in self.state["order"]:
            if name in w["done"]:
                continue
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
            w["done"].append(name)
            self.save_state()
            self.check_stop()

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
                if p["profession"] == "魔术师" and skill_left(s, name, "舞台魔术") > 0:
                    menu.append(f'- 舞台魔术（任意人数在场也可空手强杀，不可防御）：{{"type": "stage_magic", "target": "{"|".join(targets)}"}}')
        return menu

    def run_night(self) -> None:
        """晚间休息：留宿邀请/夜间房间行动/杀手夜袭（互相不可见选择），结算后执行夜间行动与夜间谋杀。"""
        day = self.state["phase"]["day"]
        period = current_period(self.state)
        w = self.wip(f"{day}:晚间休息:归宿", done=[], assignments={}, night_do={}, invites={},
                     invited={}, settled=False, ndo_done=[], murder_done=False)
        assignments: dict[str, str] = dict(w["assignments"])
        night_do: dict[str, str] = dict(w["night_do"])
        if not w["settled"]:
            if self.state["night_assigned"] == period:
                return  # 旧格式存档：已结算但无席位粒度断点，夜间行动无法安全恢复，按旧语义跳过
            pending_invite: dict[str, str] = dict(w["invites"])
            invited: dict[str, str] = dict(w["invited"])
            alive_names = [p["name"] for p in self.alive()]
            for i, name in enumerate(self.state["order"]):
                p = self.player(name)
                if not p or not p["alive"] or name in w["done"]:
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
                if p["identity"] == "杀手" and not self.state["stage_murder_used"]:
                    menu.append('- 夜袭：前往一名玩家的卧室动手（需开锁手段；若房内已有 2 人则无法进入，记为谋杀失败一次）：{"type": "night_raid", "room": "某人的卧室"}')
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
                    elif at == "night_raid" and p["identity"] == "杀手":
                        room = str(act.get("room", ""))
                        if is_bedroom(room) and bedroom_owner(room) != name:
                            assignments[name] = room
                w["done"].append(name)
                w["assignments"] = dict(assignments)
                w["night_do"] = dict(night_do)
                w["invites"] = dict(pending_invite)
                w["invited"] = dict(invited)
                self.save_state()
                self.check_stop()
            ev = self.eng.night_assign(assignments, invited)
            if not ev["ok"]:
                print(f"  晚间归宿结算失败：{ev.get('error')}")
                return
            self.route(ev)
            self.script_line("- 晚间归宿：" + "、".join(f"{n}→{r}" for n, r in ev["positions"].items()))
            if invited:
                self.script_line("  留宿：" + "、".join(f"{t} 接受 {v} 的邀请" for t, v in invited.items()))
            self.script_private(ev)
            for v in ev.get("device_triggers", []):
                self.script_line(f"  延时装置触发：{v}")
            w["settled"] = True
            self.save_state()
        for name, do in night_do.items():
            if name in w["ndo_done"]:
                continue
            w["ndo_done"].append(name)
            if self.state["positions"].get(name) == assignments.get(name):
                p = self.player(name)
                if p is not None and p["alive"]:
                    self.dispatch(p, {"type": do, "room": assignments[name]})
            self.save_state()
        if not w["murder_done"]:
            self.night_murder(day)
            w["murder_done"] = True
            self.save_state()

    def night_murder(self, day: int) -> None:
        """夜间谋杀窗口：杀手本阶段谋杀机会未用且与非反派同房时，提供一次动手机会（规则 7.7 夜袭）。"""
        if self.state["winner"] or self.state["stage_murder_used"]:
            return
        kp = next((p for p in self.alive() if p["identity"] == "杀手"), None)
        if kp is None:
            return
        room = self.state["positions"].get(kp["name"])
        targets = [q["name"] for q in self.alive()
                   if q["name"] != kp["name"] and not is_villain(q)
                   and self.state["positions"].get(q["name"]) == room]
        if not targets:
            return
        menu = [f'- 谋杀（本次不用则本阶段机会作废）：{{"type": "murder", "target": "{"|".join(targets)}"}}（可加 "weapon": "凶器名"；空手需满足体格/性别条件，在目标自己卧室偷袭门槛放宽）']
        if kp["profession"] == "魔术师" and skill_left(self.state, kp["name"], "舞台魔术") > 0:
            menu.append(f'- 舞台魔术（不可防御）：{{"type": "stage_magic", "target": "{"|".join(targets)}"}}')
        briefing = self.build_briefing(kp, f"第{day}天 · 晚间休息（夜袭）", menu,
                                       "夜深人静，机会只有一次。动手，或按兵不动（actions 为 []）。")
        payload = self.call_ai(kp, "晚间休息-夜袭", briefing)
        self.apply_actions(kp, payload["actions"])


    def run_investigation(self) -> None:
        """案件调查：调查自荐（现场调查/鲁米诺试剂/犯罪侧写）+ 侦探监控特权。

        清晨公告（死亡/未遂/强制开锁/职业公开/监控宣读）由 advance() 统一路由，此处只管调查动作。
        全无案件且无未遂的清晨（第 2 天恒如此）不调用任何 AI，直接跳过。
        """
        day = self.state["phase"]["day"]
        yday = day - 1
        had_attempts = any(a["day"] == yday and a["target"] for a in self.state["attempts"])
        if not self.state["cases"] and not had_attempts:
            return
        w = self.wip(f"{day}:案件调查:调查", done=[], det_done=False)
        case_lines = [f"案件 {c['id']}：{c['victim']} 死于{c['room']}（第{c['day']}天）" for c in self.state["cases"]]
        for name in self.state["order"]:
            if self.state["winner"]:
                return
            p = self.player(name)
            if not p or not p["alive"] or name in w["done"]:
                continue
            menu = []
            if skill_left(self.state, name, "现场调查") > 0 or held_item(p, "鲁米诺试剂", day) is not None:
                menu.append('- 现场调查（每起案件可选一项获知真实结论，可申报多次、次数以剩余为准）：{"type": "investigate", "case_id": 案件编号, "choice": "武器伤痕|死亡时间|尸体转移"}')
            if skill_left(self.state, name, "犯罪侧写") > 0:
                menu.append('- 犯罪侧写（获知凶手本人的一项特征）：{"type": "profile", "case_id": 案件编号, "aspect": "性别|国籍|体格"}')
            if not menu:
                continue
            extra = "已发生的案件：\n" + "\n".join(case_lines)
            extra += "\n调查结果仅你本人可见——是否公布、隐瞒或撒谎，由你在发言中决定。"
            briefing = self.build_briefing(p, f"第{day}天 · 案件调查", menu, extra)
            payload = self.call_ai(p, "案件调查", briefing)
            self.apply_actions(p, payload["actions"])
            w["done"].append(name)
            self.save_state()
            self.check_stop()
        det = self.state.get("detective")
        dp = self.player(det) if det else None
        if dp and dp["alive"] and not self.state["winner"] and not w["det_done"]:
            menu = ['- 侦探特权·额外查询监控（指定一个时间段 × 至多两个场所）：{"type": "detective_monitoring", "period": "第N天自由活动", "rooms": ["书房", "监控室"]}',
                    "- 不查询则 actions 为 []"]
            extra = ("你是今日侦探。除公开宣读的监控外，你可额外指定一个时间段 × 至多两个场所查看进入者名单，"
                     "结果仅你可见，由你在发言中向大家转述（你可以撒谎——这是你唯一的信息特权）。")
            briefing = self.build_briefing(dp, f"第{day}天 · 案件调查（侦探特权）", menu, extra)
            payload = self.call_ai(dp, "案件调查-侦探特权", briefing)
            self.apply_actions(dp, payload["actions"])
            w["det_done"] = True
            self.save_state()
            self.check_stop()

    def run_vote(self) -> None:
        """投票：逐席收票（反派心理医生可夹带记忆植入）→ 结算 → 遗言先于身份公布 → 封存道具分配 → 武装指认。

        断点设计：收票逐席落盘（wip.votes/implant）；结算结果 ev 本身也落盘（wip.ev），
        续跑从遗言/公布处继续，绝不会重复结算投票。
        """
        day = self.state["phase"]["day"]
        alive_names = [p["name"] for p in self.alive()]
        w = self.wip(f"{day}:投票:收票", done=[], votes={}, implant=None,
                     resolved=False, ev=None, error="", last_words=False, routed=False)
        votes: dict[str, str | None] = dict(w["votes"])
        implant = w["implant"]
        if not w["resolved"]:
            for name in self.state["order"]:
                p = self.player(name)
                if not p or not p["alive"] or name in w["done"]:
                    continue
                others = [n for n in alive_names if n != name]
                menu = [f'- 投给一名存活玩家：{{"type": "vote", "target": "{"|".join(others)}"}}',
                        '- 弃权：{"type": "abstain"}']
                if is_villain(p) and p["profession"] == "心理医生" and skill_left(self.state, name, "记忆植入") > 0:
                    menu.append('- 记忆植入（与你的投票一并声明，强迫一名玩家改投你指定的人；只会公布"票数被更改过"）：{"type": "memory_implant", "voter": "玩家名", "target": "玩家名"}')
                briefing = self.build_briefing(
                    p, f"第{day}天 · 投票", menu,
                    f"现在投票。得票超过存活人数（{len(alive_names)} 人）一半者出局；平票或无人过半则无人出局。")
                payload = self.call_ai(p, "投票", briefing)
                for act in payload["actions"]:
                    at = act.get("type")
                    if at == "vote":
                        tgt = str(act.get("target", ""))
                        votes[name] = tgt if tgt in others else None
                    elif at == "abstain":
                        votes[name] = None
                    elif at == "memory_implant" and implant is None:
                        implant = {"by": name, "voter": str(act.get("voter", "")), "target": str(act.get("target", ""))}
                self.tell(name, f"第{day}天投票记录：你{'投给了 ' + str(votes[name]) if votes.get(name) else '弃权'}")
                w["done"].append(name)
                w["votes"] = dict(votes)
                w["implant"] = implant
                self.save_state()
                self.check_stop()
            detail_votes = "、".join(f"{n}→{votes.get(n) or '弃权'}" for n in alive_names)
            self.script_line(f"- 投票明细：{detail_votes}")
            ev = self.eng.resolve_vote(votes, implant)
            if not ev["ok"] and implant is not None:
                ev = self.eng.resolve_vote(votes, None)  # 植入声明非法（如目标已死）：作废植入，不影响投票本身
            elif ev["ok"] and implant is not None:
                self.script_line(f"  记忆植入：{implant['by']} 强迫 {implant['voter']} 投给 {implant['target']}")
            w["resolved"] = True
            w["ev"] = ev if ev["ok"] else None
            w["error"] = "" if ev["ok"] else str(ev.get("error", ""))
            self.save_state()
        ev = w["ev"]
        if ev is None:
            if not w["routed"]:
                print(f"  投票结算失败：{w['error']}（按无人出局继续）")
                self.log_line("投票因结算异常未能产生结果，本轮无人出局")
                w["routed"] = True
                self.save_state()
            return
        public = ev.get("public") or {}
        out = public.get("out")
        tally = public.get("tally") or {}
        if out and not w["last_words"]:
            op = self.player(out)
            ask = self.build_briefing(op, f"第{day}天 · 遗言",
                                      ["- 本环节无动作可做，actions 返回 []"],
                                      "你被投票出局了。发表你的遗言（可以撒谎）——遗言之后主持人将公布你的身份。")
            payload = self.call_ai(op, "遗言", ask)
            last_words = str(payload.get("speech") or "").strip()
            if last_words:
                self.log_line(f"**{out} 的遗言**：{last_words}")
            w["last_words"] = True
            self.save_state()
        if not w["routed"]:
            detail = "、".join(f"{t} {c}票" for t, c in sorted(tally.items(), key=lambda kv: -kv[1]))
            self.log_line(f"投票结果：{detail or '无人得票'}")
            self.route(ev)  # 出局公告与身份公布、帮凶继任私密通知（遗言之后）
            w["routed"] = True
            self.save_state()
        if self.state["winner"]:
            return
        if self.state["sealed_items"]:
            self.run_assign_sealed()
            if self.state["winner"]:
                return
        if day >= 4 and out is None:
            self.run_armed_accusation()

    def run_assign_sealed(self) -> None:
        """封存道具分配：每席一票提出整体归属，逐道具多数决，平票取侦探票；无人投票的道具销毁。

        简化：不单设讨论轮（讨论已在公共发言发生），一次调用收齐全部道具的归属票。
        """
        day = self.state["phase"]["day"]
        items = [e["name"] for e in self.state["sealed_items"]]
        alive_names = [p["name"] for p in self.alive()]
        det = self.state.get("detective")
        w = self.wip(f"{day}:投票:道具分配", done=[], ballots={}, announced=False, assigned=False)
        if not w["announced"]:
            self.log_line(f"封存道具待分配：{'、'.join(items)}")
            w["announced"] = True
            self.save_state()
        ballots: dict[str, dict[str, str | None]] = {k: dict(v) for k, v in w["ballots"].items()}
        for name in self.state["order"]:
            p = self.player(name)
            if not p or not p["alive"] or name in w["done"]:
                continue
            menu = [('- 为每件封存道具投出归属：{"type": "assign_vote", "mapping": {'
                     + "、".join(f'"{it}": "玩家名 或 销毁"' for it in items) + "}}"),
                    f"- 可分配对象：{'、'.join(alive_names)}；写 \"销毁\" 则销毁该道具"]
            briefing = self.build_briefing(
                p, f"第{day}天 · 道具分配", menu,
                "封存道具（来自出局者与死者）现在分配。逐道具多数决；平票时以侦探的票为准。分配到的道具次日起可用。")
            payload = self.call_ai(p, "道具分配", briefing)
            for act in payload["actions"]:
                if act.get("type") == "assign_vote" and isinstance(act.get("mapping"), dict):
                    ballots[name] = {str(k): (None if v in (None, "销毁") else str(v))
                                     for k, v in act["mapping"].items()}
            w["done"].append(name)
            w["ballots"] = {k: dict(v) for k, v in ballots.items()}
            self.save_state()
            self.check_stop()
        if w["assigned"]:
            return
        if ballots:
            detail = "；".join(f"{n}：{m}" for n, m in ballots.items())
            self.script_line(f"- 道具分配票明细：{detail}")
        mapping: dict[str, str | None] = {}
        for item in items:
            tally: dict[str | None, int] = {}
            for m in ballots.values():
                if item not in m:
                    continue
                v = m[item]
                if v is not None and v not in alive_names:
                    continue
                tally[v] = tally.get(v, 0) + 1
            if not tally:
                mapping[item] = None
                continue
            top = max(tally.values())
            cands = [v for v, c in tally.items() if c == top]
            if len(cands) == 1:
                mapping[item] = cands[0]
            else:
                det_pick = (ballots.get(det) or {}).get(item) if det else None
                mapping[item] = det_pick if (det_pick is None or det_pick in alive_names) else None
        ev = self.eng.assign_sealed(mapping)
        if ev["ok"]:
            self.route(ev)
        else:
            print(f"  封存道具分配失败：{ev.get('error')}")
        w["assigned"] = True
        self.save_state()

    def run_armed_accusation(self) -> None:
        """武装指认（第 4 天起，本轮投票无人出局）：按顺序询问持武器平民；每次投票至多接受一次指认。"""
        day = self.state["phase"]["day"]
        w = self.wip(f"{day}:投票:武装指认", done=[], fired=False)
        if w["fired"]:
            return
        for name in self.state["order"]:
            if self.state["winner"]:
                return
            if name in w["done"]:
                continue
            p = self.player(name)
            if not p or not p["alive"] or p["identity"] != "平民":
                continue
            if not (held_item_names(p, day) & set(WEAPONS)):
                continue
            others = [q["name"] for q in self.alive() if q["name"] != name]
            menu = [f'- 武装指认某人为杀手（指对平民立即获胜；指错你直接出局）：{{"type": "armed_accuse", "target": "{"|".join(others)}"}}',
                    "- 不指认则 actions 为 []"]
            briefing = self.build_briefing(p, f"第{day}天 · 武装指认", menu,
                                           "本轮投票无人出局。你持有武器，可以选择武装指认——机会只有一次，慎重。")
            payload = self.call_ai(p, "武装指认", briefing)
            fired = False
            for act in payload["actions"]:
                if act.get("type") == "armed_accuse" and not fired:
                    ev = self.eng.armed_accusation(name, str(act.get("target", "")))
                    if ev["ok"]:
                        self.route(ev)
                        fired = True
                    else:
                        self.reject_note(p, ev)
            w["done"].append(name)
            if fired:
                w["fired"] = True
                self.save_state()
                return  # 一次投票后至多一次指认
            self.save_state()
            self.check_stop()

    def run_coverup(self) -> None:
        """毁证窗口（自由活动/晚间休息结束前）：存在可毁证案件且反派有手段时，按 杀手→帮凶 顺序提供机会。"""
        s = self.state
        day = s["phase"]["day"]
        vulnerable = [c for c in s["cases"]
                      if c["evidence_vulnerable"] and (c["falsified"] is None or not c["body_moved"])]
        if not vulnerable:
            return

        def has_means(p: dict) -> bool:
            return (any(skill_left(s, p["name"], sk) > 0 for sk in COVERUP_SKILLS)
                    or held_item(p, "可疑的粉末", day) is not None
                    or held_item(p, "裹尸袋", day) is not None)

        villains = sorted((p for p in self.alive() if is_villain(p) and has_means(p)),
                          key=lambda p: 0 if p["identity"] == "杀手" else 1)
        if not villains:
            return
        w = self.wip(f"{day}:{s['phase']['stage']}:毁证", done=[])
        case_lines = [f"案件 {c['id']}：{c['victim']}（{c['room']}）" for c in vulnerable]
        for p in villains:
            if self.state["winner"]:
                return
            if p["name"] in w["done"]:
                continue
            menu = []
            if skill_left(s, p["name"], "伪造痕迹") > 0:
                menu.append('- 伪造痕迹（该案件除武器伤痕外的调查结果全部出错）：{"type": "falsify", "case_id": 案件编号, "kind": "伪造痕迹"}')
            if skill_left(s, p["name"], "销毁证物") > 0:
                menu.append('- 销毁证物（该案件仅死亡时间保持正确）：{"type": "falsify", "case_id": 案件编号, "kind": "销毁证物"}')
            if skill_left(s, p["name"], "伪造证物") > 0 or held_item(p, "可疑的粉末", day) is not None:
                menu.append('- 伪造证物（该案件除尸体转移外的调查结果全部出错）：{"type": "falsify", "case_id": 案件编号, "kind": "伪造证物"}')
            if skill_left(s, p["name"], "转移尸体") > 0 or held_item(p, "裹尸袋", day) is not None:
                menu.append('- 转移尸体到另一房间：{"type": "move_body", "case_id": 案件编号, "to_room": "房间名"}')
            extra = ("以下案件的现场没有其他非反派角色，你可以动手毁证（也可以不动）：\n" + "\n".join(case_lines))
            briefing = self.build_briefing(p, f"第{day}天 · 毁证机会", menu, extra)
            payload = self.call_ai(p, "毁证机会", briefing)
            self.apply_actions(p, payload["actions"])
            w["done"].append(p["name"])
            self.save_state()
            self.check_stop()

    def run_game(self) -> None:
        """主循环：从当前 state.phase 推进到游戏结束（席位行动粒度断点，可 --resume）。

        无头模式由 main() 直接调用；console 在工作线程里调用并提供 hooks。
        任一阶段产生 winner 即提前收束（advance 在终局后会被引擎驳回，故每个分支先判 winner 再推进）。
        SoftStop：console 的 quit/Ctrl+C 在席位间隙触发，在此收束；API 失败（AbortRun）与
        键盘中断（KeyboardInterrupt）不在此捕获——call_ai 落盘后透传给调用方。
        """
        try:
            while not self.state["winner"]:
                day, stage = self.state["phase"]["day"], self.state["phase"]["stage"]
                stop = self.hooks.get("should_stop")
                if stop and stop():
                    print("收到退出指令，已在阶段边界安全停止（可用 --resume 续跑）")
                    break
                if stage == "开局准备":
                    self.run_prep()
                    self.advance(f"第{day}天 · 公共发言")
                elif stage == "案件调查":
                    self.run_investigation()
                    self.advance(f"第{day}天 · 公共发言")
                elif stage == "公共发言":
                    self.run_speeches()
                    if not self.state["winner"]:
                        self.run_exchange()
                    if not self.state["winner"]:
                        self.advance(f"第{day}天 · {'投票' if day >= 2 else '自由活动'}")
                elif stage == "投票":
                    self.run_vote()
                    if not self.state["winner"]:
                        self.advance(f"第{day}天 · 自由活动")
                elif stage == "自由活动":
                    self.run_free_activity()
                    if not self.state["winner"]:
                        self.run_coverup()
                    if not self.state["winner"]:
                        self.advance(f"第{day}天 · 晚间休息")
                elif stage == "晚间休息":
                    self.run_night()
                    if not self.state["winner"]:
                        self.run_coverup()
                    if not self.state["winner"]:
                        self.advance(f"第{day + 1}天 · 清晨")
                else:
                    self.advance(f"第{day}天 · {stage}")
                self.save_state()
        except SoftStop:
            print("收到退出指令，已在当前席位行动后安全停止（可用 --resume 续跑）")
        if self.state["winner"]:
            self.log_header("游戏结束")
            self.log_line(f"游戏结束：{self.state['winner']}获胜——{self.state.get('win_reason', '')}")
        self.save_state()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", required=True, help="对局目录（deal.py 产物，如 game/012345）")
    ap.add_argument("--resume", action="store_true", help="从 state.json 断点续跑（席位行动粒度）")
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
        orch.run_game()
    except AbortRun as e:
        print(f"\nAPI 调用失败，对局已中断并保存：{e}\n排除故障后用 --resume 续跑，被中断的席位会重新行动。")
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\n已中断（Ctrl+C）。进度已保存（席位行动粒度），可用 --resume 续跑，被中断的席位会重新行动。")
        raise SystemExit(130)
    ph = orch.state["phase"]
    if orch.state["winner"]:
        print(f"对局结束：{orch.state['winner']}获胜——{orch.state.get('win_reason', '')}")
    else:
        print(f"跑局停止：state.phase = 第{ph['day']}天 · {ph['stage']}；winner 未定，可用 --resume 续跑")

if __name__ == "__main__":
    main()
