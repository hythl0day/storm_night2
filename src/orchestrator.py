"""编排器主循环（M4/M5）：按阶段生成简报 → 收集三通道 JSON → 引擎裁决 → 落盘。

用法：
    python -m src.orchestrator --game game/012345            # 跑完整局（直至产生胜者或主神结局）
    python -m src.orchestrator --game game/012345 --resume   # 从 state.json 断点续跑（席位行动粒度）

一天流程：开局准备（仅第 1 天：反派建筑师确认密道、窃听器持有者声明放置）→ 公共发言
（第 1 天改为侦探竞选：声明轮短发言+参选宣告 → 密封退选 → 未参选者投票；第 2 天起为常规发言，
侦探末置发言、一票计 1.5）→ 交换环节 → 投票（第 2 天起：收票/记忆植入/
遗言先于身份公布/封存道具与警徽遗嘱/无人出局时武装指认）→ 自由活动（会客室/书房/监控室/地下室
四选一，白天卧室清场；停电声明并入选房菜单；同入邀请；引擎结算；房间交谈；房间行动；
阶段末毁证窗口）→ 晚间休息（守夜邀请与守夜密谈/夜间房间行动/密道/收听窃听器/杀手夜袭，
互相不可见选择）→ advance() 进入次日案件调查（清晨公告含状态行/调查自荐/死者字条遗嘱）。
终局（任一胜负条件满足）自动收束并落盘结局。

消息结构：system = render(game_system.md)（人设+规则书+私密卡），user = 当次简报（增量公告 +
增量私密事件 + 关键记录档案 + notes 回放 + 合法动作菜单）；对话不累积，每阶段独立调用。
全部顺序执行，不并发。观察者钩子（console 订阅；无头模式全为空操作）：on_call_start /
on_token / on_payload / on_call_end / on_stage / on_public（公开公告）/ on_result（动作裁决结果）/
on_private（私密回执）/ should_stop（席位间隙安全暂停：pause/Ctrl+C/quit 共用）。

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
                   "wip": {环节key(天:阶段:子环节): 席位粒度断点}, "hist": {名字: [私密事件历史，简报"关键记录"用]},
                   "talks": {f"{天}:{房间}": [房内交谈行]}（窃听器录音源）, "out_day": {出局者: 天数}（清晨状态表用）}
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
    FREE_ROOMS,
    HALL,
    MEETING_STAGES,
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
    unlock_means,
)
from .gamedata import ARMED_WEAPONS, physique_desc
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

ROOM_CHOICES = FREE_ROOMS  # 选房菜单：会客室/书房/监控室/地下室（v3：白天卧室清场）


def _fmt_votes(x) -> str:
    """票数显示：整票不带小数点，侦探加权的半票保留 .5。"""
    return str(int(x)) if float(x).is_integer() else str(x)


def fmt_monitoring(data: dict, day: int | None = None) -> str:
    """监控记录扁平化：今日新增置顶 + 历史平铺（替代嵌套 JSON，降低 AI 跨天扫行误读）。"""

    def flat(items) -> str:
        parts = []
        for period, rooms in items:
            seg = "；".join(f"{r}←{'、'.join(ns)}" for r, ns in rooms.items() if ns)
            if seg:
                parts.append(f"{period}[{seg}]")
        return "；".join(parts) or "无"

    entries = list(data.items())
    if day is None:
        return flat(entries)
    today = [kv for kv in entries if kv[0].startswith(f"第{day}天")]
    history = [kv for kv in entries if not kv[0].startswith(f"第{day}天")]
    out = f"今日新增：{flat(today)}"
    if history:
        out += f"｜历史：{flat(history)}"
    return out


def morning_roster_rows(state: dict, out_day: dict) -> list[tuple[str, str, str, str, str, str]]:
    """全剧透状态行（host 侧）：script.md 每日清晨状态表与 console 天亮一览共用。"""
    deaths = {c["victim"]: (c["day"], c["room"]) for c in state["cases"]}
    rows = []
    for p in state["players"]:
        n = p["name"]
        if p["alive"]:
            status = "存活"
        elif n in deaths:
            d, r = deaths[n]
            status = f"死于第{d}天（{r}）"
        elif n in out_day:
            status = f"第{out_day[n]}天出局"
        else:
            status = "出局"
        skills = "、".join(f"{k}×{v}" for k, v in state.get("skill_uses", {}).get(n, {}).items() if v > 0) or "无"
        # items 键存在即以其为准（空列表=真的无道具）；仅在键缺失时回退 deal 旧字段（兼容早期存档/夹具）
        if "items" in p:
            entries = p["items"]
        elif p.get("item"):
            entries = [{"name": p["item"], "uses_left": p.get("item_uses_left", 0)}]
        else:
            entries = []
        items = "、".join(f"{e['name']}×{e['uses_left']}" for e in entries if e["uses_left"] > 0) or "无"
        rows.append((n, status, p["identity"], p["profession"], skills, items))
    return rows

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
        self.orch.setdefault("wip", {})   # 各环节（天:阶段:子环节）的席位粒度断点
        self.orch.setdefault("hist", {})  # 各席私密事件历史（简报"关键记录"段的数据源）
        self.orch.setdefault("talks", {})  # 房内交谈存档 {f"{day}:{room}": [行]}（窃听器下发用）
        self.orch.setdefault("out_day", {})  # 出局/指认出局者 → 天数（script.md 清晨状态表用）
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
            f"- 书房保险箱：{s.get('safe_item', '?')}（开局公告已公开）",
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
        """把事件的私密回执逐条记入全记录（host 侧事实，不进 public/log.md）。console 经 on_private 上屏。"""
        for name, msg in (ev.get("private") or {}).items():
            if not isinstance(msg, str):
                msg = json.dumps(msg, ensure_ascii=False)
            self.script_line(f"  [{name}] {msg}")
            self._fire("on_private", name, msg)

    def script_action(self, name: str, act: dict, ev: dict) -> None:
        """记录一次动作声明与裁决结果（含驳回——host 侧可见，不公开）。console 经 on_result 上屏。"""
        ok = bool(ev.get("ok"))
        if not ok:
            detail = f"驳回（{ev.get('error')}）"
        else:
            priv = (ev.get("private") or {}).get(name)
            if isinstance(priv, str) and priv:
                detail = f"成功：{priv}"
            elif priv:
                detail = f"成功：{json.dumps(priv, ensure_ascii=False)}"
            else:
                detail = "成功"
        self.script_line(f"- {name} 声明 {json.dumps(act, ensure_ascii=False)} → {detail}")
        self._fire("on_result", name, ok, detail)

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
        """公开事件：feed + public/log.md + script.md 三处同写。"""
        self.feed_line(line)
        self.script_line(line)
        self._fire("on_public", line)

    def feed_line(self, line: str) -> None:
        """仅公开口径（feed + public/log.md），不进 script.md——供编排器已自记明细的事件去重。"""
        self.orch["feed"].append(line)
        with open(self.dir / "public" / "log.md", "a", encoding="utf-8") as f:
            f.write(line + "\n")

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

    def route(self, ev: dict, script: bool = True) -> None:
        """把引擎事件路由到公开信息流与各席私密队列。script=False 时公告不进 script.md（调用方自记明细，去重）。"""
        if not ev.get("ok"):
            return
        sink = self.log_line if script else self.feed_line
        public = ev.get("public") or {}
        for line in public.get("announcements", []):
            sink(f"- {line}")
        if "monitoring" in public:
            sink(f"- 监控宣读：{fmt_monitoring(public['monitoring'])}")
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
        pos = self.state["positions"].get(name, HALL)
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
        meeting_hall = pos == HALL and self.state["phase"]["stage"] in MEETING_STAGES
        if pos and not meeting_hall:
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
            ev = self.eng.view_monitoring(name)
            if ev.get("ok"):
                ev["private"] = {name: "监控记录（公开口径，今日新增置顶）：" + fmt_monitoring(ev["private"][name], day)}
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
            ev = self.eng.use_passage(name, str(act.get("room", "")))
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
        if self.state["phase"]["stage"] == "案件调查":
            self.script_morning_status()
        self.save_state()
        ph = self.state["phase"]
        if not self.hooks.get("on_stage"):  # 无头模式的阶段标记；console 由 on_stage 画双线横幅
            print(f"== 进入 第{ph['day']}天 · {ph['stage']} ==")
        self._fire("on_stage", title)
        return ev

    def script_morning_status(self) -> None:
        """每天清晨在 script.md 写全剧透状态表（仅 host 侧，不进任何 prompt）。"""
        day = self.state["phase"]["day"]
        lines = [f"### 第{day}天清晨 · 状态一览（全剧透）", "",
                 "| 角色 | 状态 | 身份 | 职业 | 技能剩余 | 道具 |", "| --- | --- | --- | --- | --- | --- |"]
        lines += ["| " + " | ".join(r) + " |" for r in morning_roster_rows(self.state, self.orch["out_day"])]
        self.script_line("\n".join(lines))

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
                menu.append('- 确认密道（无需指定出口；晚间休息时可经密道前往书房/监控室/地下室）：{"type": "set_passage"}')
            if held_item(p, "窃听器", 1) is not None and self.state["bug_room"] is None:
                menu.append('- 放置窃听器（含会客室——它无监控，窃听器是唯一能监视它的手段）：{"type": "place_bug", "room": "会客室|书房|监控室|地下室"}')
            if not menu:
                continue
            briefing = self.build_briefing(p, "开局准备 · 秘密布置", menu,
                                           "开局前的秘密布置环节。这是你的开局义务，现在声明。")
            payload = self.call_ai(p, "开局准备", briefing)
            for act in payload["actions"]:
                if act.get("type") == "set_passage":
                    ev = self.eng.set_passage(p["name"])
                    self.script_action(p["name"], act, ev)
                    self.route(ev) if ev["ok"] else self.reject_note(p, ev)
                elif act.get("type") == "place_bug":
                    ev = self.eng.place_bug(p["name"], str(act.get("room", "")))
                    self.script_action(p["name"], act, ev)
                    self.route(ev) if ev["ok"] else self.reject_note(p, ev)
            w["done"].append(p["name"])
            self.save_state()
            self.check_stop()

    def run_election(self) -> None:
        """侦探竞选（v3.3 rules-6 §7.4，取代第 1 天公共发言）：

        ① 声明轮：全员按顺序短发言并宣告参选与否（公开，可夹带职业/道具宣告——谎言留档）；
        ② 密封退选：参选者 ≥2 时逐一私下决定留任/退选，收齐后一齐公布（退选者无投票权）；
        ③ 未参选者投票，简单多数；无人报名/全员退选/全员参选/平票 → 本局不设侦探（引擎裁决）。
        """
        day = self.state["phase"]["day"]
        w = self.wip(f"{day}:公共发言:竞选", declared=[], ran=[], decided={}, withdrew=False,
                     stay=[], voted=[], votes={}, resolved=False)
        alive_names = [p["name"] for p in self.alive()]
        for name in self.state["order"]:
            p = self.player(name)
            if not p or not p["alive"] or name in w["declared"]:
                continue
            menu = ['- 宣告是否参选侦探：{"type": "declare", "run": true|false}',
                    "- 声明写在 speech 字段（简短表态；可顺带宣告你的职业与道具——可以撒谎，但谎言会留档）"]
            briefing = self.build_briefing(
                p, f"第{day}天 · 侦探竞选声明", menu,
                "现在竞选侦探。侦探职权：公共发言末置发言、投票时一票计 1.5 票；出局时警徽按遗嘱传承（可指定归属或销毁）。"
                "若留任候选人超过一人，由未参选者投票决出（参选者与退选者均无投票权）。")
            payload = self.call_ai(p, "竞选声明", briefing)
            speech = str(payload.get("speech") or "").strip()
            if speech:
                self.log_line(f"**{name}**：{speech}")
            run = any(a.get("type") == "declare" and bool(a.get("run")) for a in payload["actions"])
            if run:
                w["ran"].append(name)
            self.log_line(f"{name} 宣告{'参选侦探' if run else '不参选'}")
            w["declared"].append(name)
            self.save_state()
            self.check_stop()
        if len(w["ran"]) >= 2 and not w["withdrew"]:
            decided: dict[str, bool] = dict(w["decided"])
            for name in w["ran"]:
                if name in decided:
                    continue
                p = self.player(name)
                menu = ['- 继续参选：{"type": "elect_stay"}',
                        '- 退选（退选者同样无投票权）：{"type": "withdraw"}']
                briefing = self.build_briefing(
                    p, f"第{day}天 · 侦探竞选退选", menu,
                    f"参选者：{'、'.join(w['ran'])}。留任/退选决定在所有人作答后一齐公布。"
                    "若最终留任多人，由未参选者投票；平票或全员退选则本局不设侦探。")
                payload = self.call_ai(p, "竞选退选", briefing)
                decided[name] = not any(a.get("type") == "withdraw" for a in payload["actions"])
                w["decided"] = dict(decided)
                self.save_state()
                self.check_stop()
            w["stay"] = [n for n in w["ran"] if decided.get(n, True)]
            w["withdrew"] = True
            dropped = [n for n in w["ran"] if n not in w["stay"]]
            self.log_line(f"退选结果：{('、'.join(dropped) + ' 退选；') if dropped else '无人退选；'}"
                          f"留任候选人：{'、'.join(w['stay']) or '无'}")
            self.save_state()
        stay = list(w["stay"]) if w["withdrew"] else list(w["ran"])
        voters = [n for n in alive_names if n not in w["ran"]]
        if len(stay) >= 2 and voters and not w["resolved"]:
            votes: dict[str, str | None] = dict(w["votes"])
            for name in self.state["order"]:
                if name not in voters or name in w["voted"]:
                    continue
                p = self.player(name)
                menu = [f'- 投票选侦探：{{"type": "elect", "target": "{"|".join(stay)}"}}',
                        '- 弃权：{"type": "abstain"}']
                briefing = self.build_briefing(
                    p, f"第{day}天 · 侦探竞选投票", menu,
                    f"留任候选人：{'、'.join(stay)}。未参选者投票，简单多数当选；平票则本局不设侦探。")
                payload = self.call_ai(p, "竞选投票", briefing)
                votes[name] = None
                for act in payload["actions"]:
                    if act.get("type") == "elect" and str(act.get("target", "")) in stay:
                        votes[name] = str(act["target"])
                w["votes"] = dict(votes)
                w["voted"].append(name)
                self.save_state()
                self.check_stop()
        if not w["resolved"]:
            votes = dict(w["votes"])
            if len(stay) >= 2 and voters:
                detail = "、".join(f"{n}→{votes.get(n) or '弃权'}" for n in voters)
                self.script_line(f"- 竞选票型：{detail}")
                self._fire("on_public", f"竞选票型：{detail}")
            ev = self.eng.elect_detective(list(w["ran"]), stay, votes)
            if ev["ok"]:
                self.route(ev)
            else:
                self.log_line(f"侦探竞选未能结算：{ev.get('error', '')}（按本局不设侦探继续）")
            w["resolved"] = True
            self.save_state()

    def run_speeches(self) -> None:
        """公共发言（第 2 天起）：按行动顺序每人一次（侦探末置发言，规则 7.2）；顺序靠后者可在简报中看到靠前者的发言。"""
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
                menu.append('- 看破伪装（自己发言前；claims 只填对方实际声称过的内容——无效名目或无监控覆盖的类目会标注"无法验证"，不计入谎言判定）：{"type": "see_through", "target": "玩家名", "claims": {"profession": "对方声称的职业", "item": "对方声称的道具", "visited_rooms": ["对方声称去过的房间"], "denied_rooms": ["对方声称没去过的房间"]}}')
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
                    self.route(ev, script=False)  # 公开口径不含道具名；script 已由上行明细记录（去重）
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
            [f'- 选择房间（必选一项）：{{"type": "choose_room", "room": "房间名"}}；合法去向只有四种：{"、".join(ROOM_CHOICES)}',
             '- 注意：白天卧室一律清场不开放（含你自己的）；会客室夜间不开放。不作选择或选择无效将默认滞留会客室',
             '- 邀请一名顺序在你之后的玩家同入（对方可改选）：{"type": "invite", "target": "玩家名"}']
        )
        for i, name in enumerate(self.state["order"]):
            p = self.player(name)
            if not p or not p["alive"] or name in w["done"]:
                continue
            menu = list(rooms_menu)
            if is_villain(p) and unlock_means(self.state, name) is not None:
                menu.append('- 秘密声明今天停电（拉电闸制造过载，消耗一次开锁手段；停电中门锁失效、监控空白、除会客室外全部黑暗）：{"type": "declare_blackout"}')
            later = [n for n in self.state["order"][i + 1:] if n in alive_names]
            extra = f"你可以邀请顺序在你之后的玩家同入：{'、'.join(later) if later else '无'}。"
            inviters = [v for t, v in pending_invite.items() if t == name]
            for v in inviters:
                extra += f"\n{v} 邀请你同往 {assignments.get(v, '（未知）')}——接受就把 choose_room 选为同一房间，不接受就自选。"
            briefing = self.build_briefing(p, f"第{day}天 · 自由活动（选房）", menu, extra)
            payload = self.call_ai(p, "自由活动-选房", briefing)
            room = HALL
            for act in payload["actions"]:
                at = act.get("type")
                if at == "choose_room":
                    cand = str(act.get("room", ""))
                    if cand in ROOM_CHOICES:
                        room = cand
                    elif cand:
                        self.tell(name, f"「{cand}」不是自由活动的合法去处（白天卧室清场不开放），你滞留在会客室")
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
            if t in assignments and v in assignments and assignments[t] == assignments[v]:
                invited[t] = v
        for _ in range(len(alive_names) + 2):
            ev = self.eng.enter_rooms(assignments, invited)
            if ev["ok"]:
                self.route(ev)
                self.script_line("- 选房结果：" + "、".join(f"{n}→{r}" for n, r in assignments.items()))
                if invited:
                    self.script_line("  同入邀请成立：" + "、".join(f"{t} 随 {v}（{assignments[t]}）" for t, v in invited.items()))
                self.script_private(ev)
                for v in ev.get("device_triggers", []):
                    self.script_line(f"  延时装置触发：{v}")
                self.save_state()
                return
            bad_room = next((r for r in ROOM_CHOICES if r in ev.get("error", "")), None)
            if bad_room is None:
                stranger = [n for n in alive_names if assignments.get(n) not in ROOM_CHOICES]
                for n in stranger:
                    assignments[n] = HALL
                if stranger:
                    continue
                print(f"  选房结算失败且无法回退：{ev.get('error')}")
                return
            movable = [n for n in reversed(self.state["order"]) if n in alive_names
                       and assignments.get(n) == bad_room]
            if not movable:
                print(f"  选房结算失败且无法回退：{ev.get('error')}")
                return
            assignments[movable[0]] = HALL  # 满员/无法开锁者顺延滞留会客室（不限人数）
        print("  选房结算多次失败，已按回退处理")

    def all_rooms(self) -> list[str]:
        return list(ROOM_CHOICES)

    def room_talks(self, day: int) -> None:
        positions = self.state["positions"]
        w = self.wip(f"{day}:自由活动:交谈", done_rooms=[])
        for name in self.state["order"]:
            room = positions.get(name)
            if not room or room in w["done_rooms"]:
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
                self.orch["talks"].setdefault(f"{day}:{room}", []).extend(transcript)  # 窃听器录音源
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
        if not room:
            return menu
        # 会客室按普通亮房处理：无设施项（监控/保险箱/发电机均不在此），
        # 但谋杀/舞台魔术/延时装置合法——永不黑暗、不可封闭由下方各项自身条件保证。
        if room == "监控室":
            menu.append('- 查看监控（返回全部已有记录，无需参数）：{"type": "view_monitoring"}')
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
        if p["profession"] == "建筑师" and skill_left(s, name, "封闭空间") > 0 and room != HALL:
            menu.append('- 封闭空间（锁死当前房间至下一自由活动；不能对会客室使用）：{"type": "seal_room", "allowed": ["许可留下的人"]}')
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
        """晚间休息：守夜邀请/夜间房间行动/杀手夜袭（互相不可见选择），结算后执行夜间行动与夜间谋杀。"""
        day = self.state["phase"]["day"]
        period = current_period(self.state)
        w = self.wip(f"{day}:晚间休息:归宿", done=[], assignments={}, night_do={}, invites={},
                     invited={}, settled=False, passages={}, listens=[],
                     listen_done=[], passage_done=[], ndo_done=[], murder_done=False)
        assignments: dict[str, str] = dict(w["assignments"])
        night_do: dict[str, str] = dict(w["night_do"])
        passages: dict[str, dict] = {k: dict(v) for k, v in w.get("passages", {}).items()}
        listens: list[str] = list(w.get("listens", []))
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
                    '- 守夜邀请：邀请一名顺序在你之后的玩家来你卧室一同守夜（即夜间的同入邀请）：{"type": "invite", "target": "玩家名"}',
                    '- 不回卧室，夜间前往书房/监控室/地下室执行一项行动：{"type": "night_room", "room": "书房|监控室|地下室", "do": "open_safe|view_monitoring|fix_generator|unlock"}',
                ]
                extra = "夜间行动互相不可见。每个夜间房间只能进入 1 人（顺序靠后冲突者失败，默认回自己卧室）。会客室夜间不开放。"
                extra += "\n注意：上面「当前局面」里的位置是你自由活动结束时的实际位置；本环节决定的是**今晚去哪过夜**——不回卧室就选 night_room，什么都不做默认回自己卧室。"
                extra += "\n守夜邀请成立且房内恰好你们两人时，睡前有一轮密谈（仅彼此可见）。"
                if later:
                    extra += f"\n顺序在你之后的玩家：{'、'.join(later)}。"
                if p["identity"] == "杀手" and not self.state["stage_murder_used"]:
                    menu.append('- 夜袭：前往一名玩家的卧室动手（需开锁手段，停电期间门锁失效除外；房内满 2 人时仅停电黑暗中可潜入袭击其一，否则无法进入且当夜机会作废）：{"type": "night_raid", "room": "某人的卧室"}')
                if is_villain(p) and p["profession"] == "建筑师" and self.state["passage"] and not self.state["passage"]["used"]:
                    menu.append('- 使用密道（当夜行动，与夜间房间/夜袭互斥；监控无痕；出口人数须满足谋杀人数条件，否则当晚失败但密道保留）：{"type": "use_passage", "room": "书房|监控室|地下室", "do": "open_safe|view_monitoring|fix_generator|unlock（可选）"}')
                if self.state["bug_room"] and held_item(p, "窃听器", day) is not None:
                    menu.append('- 收听窃听器（仅当你今晚回自己卧室时生效）：{"type": "bug_listen"}')
                if name in pending_invite:
                    v = pending_invite[name]
                    menu.append(f'- 接受 {v} 的守夜邀请：{{"type": "accept_invite"}}')
                    extra += f"\n{v} 邀请你今晚去 {bedroom_of(v)} 守夜。"
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
                        passages.pop(name, None)
                    elif at == "night_room":
                        room = str(act.get("room", ""))
                        if room in PUBLIC_ROOMS:
                            assignments[name] = room
                            night_do[name] = str(act.get("do", ""))
                            passages.pop(name, None)
                    elif at == "night_raid" and p["identity"] == "杀手":
                        room = str(act.get("room", ""))
                        if is_bedroom(room) and bedroom_owner(room) != name:
                            assignments[name] = room
                            passages.pop(name, None)
                    elif at == "use_passage":
                        room = str(act.get("room", ""))
                        if room in PUBLIC_ROOMS and name not in assignments:
                            passages[name] = {"room": room, "do": str(act.get("do", ""))}
                    elif at == "bug_listen":
                        if name not in listens:
                            listens.append(name)
                w["done"].append(name)
                w["assignments"] = dict(assignments)
                w["night_do"] = dict(night_do)
                w["invites"] = dict(pending_invite)
                w["invited"] = dict(invited)
                w["passages"] = {k: dict(v) for k, v in passages.items()}
                w["listens"] = list(listens)
                self.save_state()
                self.check_stop()
            ev = self.eng.night_assign(assignments, invited)
            if not ev["ok"]:
                print(f"  晚间归宿结算失败：{ev.get('error')}")
                return
            self.route(ev)
            self.script_line("- 晚间归宿：" + "、".join(f"{n}→{r}" for n, r in ev["positions"].items()))
            if invited:
                self.script_line("  守夜：" + "、".join(f"{t} 接受 {v} 的邀请" for t, v in invited.items()))
            self.script_private(ev)
            for v in ev.get("device_triggers", []):
                self.script_line(f"  延时装置触发：{v}")
            w["settled"] = True
            self.save_state()
        else:
            invited = dict(w["invited"])
        self.sleep_talks(day, invited)
        for name in listens:
            if name in w.get("listen_done", []):
                continue
            w["listen_done"].append(name)
            p = self.player(name)
            if p is not None and p["alive"] and self.state["positions"].get(name) == bedroom_of(name):
                ev = self.dispatch(p, {"type": "bug_listen"})
                if ev and ev.get("ok"):
                    lines = self.orch["talks"].get(f"{day}:{ev.get('room')}", [])
                    content = "；".join(lines) if lines else "（当天该房间无人交谈）"
                    self.tell(name, f"窃听器录音（{ev.get('room')} · 今天自由活动）：{content}")
                    self.script_line(f"  窃听器录音下发给 {name}（{ev.get('room')}）：{content}")
            self.save_state()
        for name, ps in passages.items():
            if name in w.get("passage_done", []):
                continue
            w["passage_done"].append(name)
            p = self.player(name)
            if p is not None and p["alive"]:
                ev = self.eng.use_passage(name, ps["room"])
                self.script_action(name, {"type": "use_passage", "room": ps["room"]}, ev)
                self.route(ev) if ev["ok"] else self.reject_note(p, ev)
                if ev["ok"] and not ev.get("failed") and ps.get("do"):
                    self.dispatch(p, {"type": ps["do"], "room": ps["room"]})
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

    def sleep_talks(self, day: int, invited: dict[str, str]) -> None:
        """守夜密谈（v3）：守夜成立且房内恰好两人的卧室，睡前密谈一轮；夜袭进入的卧室不交谈。
        v3.2：黑暗房间无法交谈——停电期间卧室黑暗，密谈取消。"""
        if not invited:
            return
        w = self.wip(f"{day}:晚间休息:密谈", done=[])
        positions = self.state["positions"]
        for guest, host in invited.items():
            room = bedroom_of(host)
            if room in w["done"]:
                continue
            occ = [n for n in self.state["order"]
                   if positions.get(n) == room and (self.player(n) or {}).get("alive")]
            if set(occ) != {guest, host} or is_dark(self.state, room):
                w["done"].append(room)  # 邀请未成立/有第三者（夜袭）/房间黑暗（停电）→ 不交谈
                continue
            transcript: list[str] = []
            for occ_name in occ:
                p = self.player(occ_name)
                other = host if occ_name == guest else guest
                menu = ["- 本环节是守夜密谈：speech 只有对方能听到；actions 为 []"]
                extra = f"深夜，{room}，只有你和 {other} 两人。睡前可以密谈一轮。"
                if transcript:
                    extra += "\n对方刚才说：\n" + "\n".join(transcript)
                briefing = self.build_briefing(p, f"第{day}天 · 晚间休息（{room}密谈）", menu, extra)
                payload = self.call_ai(p, f"晚间休息-密谈-{host}", briefing)
                speech = str(payload.get("speech") or "").strip()
                if speech:
                    transcript.append(f"{occ_name}：{speech}")
            if transcript:
                self.script_line(f"- {room}密谈（{host}、{guest}）：")
                for line in transcript:
                    self.script_line(f"    {line}")
                for occ_name in occ:
                    seat = self.seat(self.player(occ_name))
                    with open(self.dir / "private" / seat / "log.md", "a", encoding="utf-8") as f:
                        f.write(f"\n## 第{day}天 · {room}密谈\n\n" + "\n".join(transcript) + "\n")
                    self.tell(occ_name, f"{room}密谈：" + "；".join(transcript))
            w["done"].append(room)
            self.save_state()
            self.check_stop()

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
        """案件调查：调查自荐（现场调查/鲁米诺试剂/犯罪侧写）+ 死者字条遗嘱。

        清晨公告（死亡/未遂/强制开锁/职业公开/监控宣读）由 advance() 统一路由，此处只管调查动作。
        全无案件且无未遂的清晨（第 2 天恒如此）不调用任何 AI，直接跳过。
        """
        day = self.state["phase"]["day"]
        yday = day - 1
        had_attempts = any(a["day"] == yday and a["target"] for a in self.state["attempts"])
        if not self.state["cases"] and not had_attempts:
            return
        # 遗嘱（v3.2）：昨日遇害者的封存道具以"死者字条"指定归属（公开），接收人可拒收
        w_be = self.wip(f"{day}:案件调查:遗嘱", done=[])
        for victim in [c["victim"] for c in self.state["cases"] if c["day"] == yday]:
            if victim in w_be["done"]:
                continue
            vp = self.player(victim)
            owned = [e["name"] for e in self.state["sealed_items"] if e.get("from") == victim]
            has_will = bool(owned) or victim == self.state.get("detective")  # 道具或侦探徽章皆可立遗嘱
            mapping = {}
            if has_will and vp is not None:
                mapping = self.ask_bequest_map(
                    vp, f"第{day}天 · 死者字条",
                    "你在昨夜遇害。以死者字条指定遗留道具的归属——这是你最后一次影响局势的机会（遗嘱公开）。")
            w_be["done"].append(victim)
            self.save_state()
            if has_will:
                self.run_bequest(victim, mapping)
            self.check_stop()
        w = self.wip(f"{day}:案件调查:调查", done=[])
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

    def run_vote(self) -> None:
        """投票：逐席收票（反派心理医生可夹带记忆植入）→ 结算 → 遗言先于身份公布 → 封存道具与警徽遗嘱 → 武装指认。

        断点设计：收票逐席落盘（wip.votes/implant）；结算结果 ev 本身也落盘（wip.ev），
        续跑从遗言/公布处继续，绝不会重复结算投票。
        """
        day = self.state["phase"]["day"]
        alive_names = [p["name"] for p in self.alive()]
        w = self.wip(f"{day}:投票:收票", done=[], votes={}, implant=None,
                     resolved=False, ev=None, error="", last_words=False, routed=False, bequest_map=None)
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
                det = self.state.get("detective")
                det_note = f"；侦探（{det}）一票计 1.5 票" if det in alive_names else ""
                briefing = self.build_briefing(
                    p, f"第{day}天 · 投票", menu,
                    f"现在投票。得票超过存活人数（{len(alive_names)} 人）一半者出局{det_note}；平票或无人过半则无人出局。")
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
            self._fire("on_public", f"投票明细：{detail_votes}")
            w["voters"] = len(alive_names)
            ev = self.eng.resolve_vote(votes, implant)
            if not ev["ok"] and implant is not None:
                ev = self.eng.resolve_vote(votes, None)  # 植入声明非法（如目标已死）：作废植入，不影响投票本身
            elif ev["ok"] and implant is not None:
                self.script_line(f"  记忆植入：{implant['by']} 强迫 {implant['voter']} 投给 {implant['target']}")
                self._fire("on_private", "记忆植入", f"{implant['by']} 强迫 {implant['voter']} 投给 {implant['target']}")
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
            owned = [e["name"] for e in self.state["sealed_items"] if e.get("from") == out]
            has_badge = out == self.state.get("detective")
            menu = ["- 本环节无动作可做，actions 返回 []"]
            extra = "你被投票出局了。发表你的遗言（可以撒谎）——遗言之后主持人将公布你的身份。"
            if owned or has_badge:
                menu = ['- 遗嘱：逐件指定遗留归属（未指定的销毁；接收人可拒收；遗嘱公开）：'
                        '{"type": "bequeath", "mapping": {"道具名": "存活玩家名"}}']
            if owned:
                extra += f"\n你留下的未使用道具：{'、'.join(owned)}。在遗言中一并指定归属。"
            if has_badge:
                extra += "\n你是侦探——侦探徽章也在遗嘱之列（mapping 里以「警徽」为键指定一名存活玩家继任；不指定则销毁，本局不再设侦探）。"
            ask = self.build_briefing(op, f"第{day}天 · 遗言", menu, extra)
            payload = self.call_ai(op, "遗言", ask)
            last_words = str(payload.get("speech") or "").strip()
            if last_words:
                self.log_line(f"**{out} 的遗言**：{last_words}")
            for act in payload["actions"]:
                if act.get("type") == "bequeath" and isinstance(act.get("mapping"), dict):
                    w["bequest_map"] = {str(k): (None if v in (None, "销毁") else str(v))
                                        for k, v in act["mapping"].items()}
            w["last_words"] = True
            self.save_state()
        if not w["routed"]:
            detail = "、".join(f"{t} {_fmt_votes(c)}票" for t, c in sorted(tally.items(), key=lambda kv: -kv[1]))
            voters = w.get("voters") or len(self.alive())
            if out:
                verdict = f"{out} 过半数出局（存活 {voters} 人，需 >{voters // 2} 票）"
            else:
                verdict = f"无人过半数（存活 {voters} 人，需 >{voters // 2} 票），本轮无人出局"
            self.log_line(f"投票结果：{detail or '无人得票'}——{verdict}")
            self.route(ev)  # 出局公告与身份公布、帮凶继任私密通知（遗言之后）
            if out:
                self.orch["out_day"][out] = day
            w["routed"] = True
            self.save_state()
        if self.state["winner"]:
            return
        if out is not None and (any(e.get("from") == out for e in self.state["sealed_items"])
                                or out == self.state.get("detective")):
            self.run_bequest(out, w.get("bequest_map") or {})
        if out is None:
            self.run_armed_accusation()

    def ask_bequest_map(self, p: dict, title: str, extra: str) -> dict:
        """请即将离场者/死者立遗嘱：返回 {道具名|"警徽": 接收人}（空=全部销毁）。"""
        owned = [e["name"] for e in self.state["sealed_items"] if e.get("from") == p["name"]]
        has_badge = p["name"] == self.state.get("detective")
        if not owned and not has_badge:
            return {}
        menu = ['- 遗嘱：逐件指定遗留归属（未指定的销毁；接收人可拒收；遗嘱公开）：'
                '{"type": "bequeath", "mapping": {"道具名": "存活玩家名"}}']
        extra += f"\n你留下的未使用道具：{'、'.join(owned) if owned else '无'}。"
        if has_badge:
            extra += "你是侦探——侦探徽章也在遗嘱之列（mapping 里以「警徽」为键指定一名存活玩家继任；不指定则销毁，本局不再设侦探）。"
        payload = self.call_ai(p, title, self.build_briefing(p, title, menu, extra))
        for act in payload["actions"]:
            if act.get("type") == "bequeath" and isinstance(act.get("mapping"), dict):
                return {str(k): (None if v in (None, "销毁") else str(v)) for k, v in act["mapping"].items()}
        return {}

    def run_bequest(self, owner: str, mapping: dict) -> None:
        """遗嘱流程（v3.2/v3.3）：本人指定归属后，逐位接收人回应（接受/拒绝，无回应默认接受），
        引擎落账并公告。侦探出局时警徽随遗嘱一并处理（mapping 以「警徽」为键，见 rules-6 §7.4）。"""
        day = self.state["phase"]["day"]
        w = self.wip(f"{day}:遗嘱:{owner}", replied=[], replies={}, settled=False)
        owned = [e["name"] for e in self.state["sealed_items"] if e.get("from") == owner]
        badge = owner == self.state.get("detective")
        mapping = dict(mapping or {})
        raw_badge = mapping.pop("警徽", None)
        badge_to = str(raw_badge) if badge and raw_badge else None
        mapping = {it: tg for it, tg in mapping.items() if it in owned}
        replies: dict[str, dict[str, bool]] = {k: dict(v) for k, v in w["replies"].items()}
        targets = list(dict.fromkeys([t for t in mapping.values() if t] + ([badge_to] if badge_to else [])))
        for tgt in targets:
            if tgt in w["replied"]:
                continue
            p = self.player(tgt)
            if not p or not p["alive"]:
                continue
            items = [it for it, t in mapping.items() if t == tgt]
            gets_badge = badge_to == tgt
            menu = ['- 逐件回应（接受则次日起可用；拒绝则销毁）：{"type": "bequest_reply", "decisions": {"道具名": "接受|拒绝"}}']
            extra = (f"{owner} 的遗嘱把以下留给你：{'、'.join(items + (['侦探徽章'] if gets_badge else []))}。"
                     "此事全员公开——收下可能带来信任也可能带来嫌疑，由你判断。")
            if gets_badge:
                extra += "接受侦探徽章即继任侦探（末置发言、一票计 1.5）。"
            payload = self.call_ai(p, "遗嘱回应", self.build_briefing(p, f"第{day}天 · 遗嘱回应", menu, extra))
            dec: dict[str, bool] = {}
            for act in payload["actions"]:
                if act.get("type") == "bequest_reply" and isinstance(act.get("decisions"), dict):
                    for k, v in act["decisions"].items():
                        dec[str(k)] = str(v) == "接受"
            for it in items + (["警徽"] if gets_badge else []):
                dec.setdefault(it, True)  # 无回应默认接受（不惩罚沉默；拒绝须明示）
            replies[tgt] = dec
            w["replied"].append(tgt)
            w["replies"] = {k: dict(v) for k, v in replies.items()}
            self.save_state()
            self.check_stop()
        if w["settled"]:
            return
        if owned:
            self.route(self.eng.bequest(owner, mapping, replies))
        if badge:
            accept = bool(badge_to and replies.get(badge_to, {}).get("警徽"))
            evb = self.eng.bequest_badge(owner, badge_to, accept)
            if evb["ok"]:
                self.route(evb)
        w["settled"] = True
        self.save_state()

    def run_armed_accusation(self) -> None:
        """武装指认（v3：第 2 天首次投票起，本轮投票无人出局）：按顺序询问持武器平民；每次投票至多一次指认。"""
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
            if not (held_item_names(p, day) & set(ARMED_WEAPONS)):
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
                        if not self.player(name)["alive"]:
                            self.orch["out_day"][name] = day
                    else:
                        self.reject_note(p, ev)
            w["done"].append(name)
            if fired:
                w["fired"] = True
                self.save_state()
                # 指认错误出局者的遗留道具（与侦探徽章）同样走遗嘱流程（v3.2/v3.3）
                if not self.player(name)["alive"] and (any(e.get("from") == name for e in self.state["sealed_items"])
                                                       or name == self.state.get("detective")):
                    mapping = self.ask_bequest_map(
                        p, f"第{day}天 · 遗嘱",
                        "你的指认错误，你出局了（身份已公布）。你的遗留道具由你指定归属。")
                    self.run_bequest(name, mapping)
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
                menu.append('- 转移尸体到另一房间：{"type": "move_body", "case_id": 案件编号, "to_room": "会客室|书房|监控室|地下室|某人的卧室"}')
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
                    self.advance(f"第{day}天 · {'侦探竞选' if day == 1 else '公共发言'}")
                elif stage == "案件调查":
                    self.run_investigation()
                    self.advance(f"第{day}天 · 公共发言")
                elif stage == "公共发言":
                    if day == 1:
                        self.run_election()  # v3.3：第 1 天公共发言时段改为侦探竞选
                    else:
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
