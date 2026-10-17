"""编排器主循环（M4/M5）：按阶段生成简报 → 收集三通道 JSON → 引擎裁决 → 落盘。

用法：
    python -m src.orchestrator --game game/012345            # 跑完整局（直至产生胜者或主神结局）
    python -m src.orchestrator --game game/012345 --resume   # 从 state.json 断点续跑（席位行动粒度）

一天流程：开局准备（仅第 1 天：反派建筑师确认密道）→ 公共发言
（第 1 天改为侦探竞选：声明轮短发言+参选宣告 → 密封退选 → 未参选者投票；v3.5 起当选者获桌上凶器作侦探佩枪；
第 2 天起为常规发言，侦探末置发言、一票计 1.5）→ 投票（第 2 天起：收票/记忆植入/
遗言先于身份公布/封存道具与警徽遗嘱——v3.5 遗嘱道具当天可用、未指定或拒收放回会客室桌上/
无人出局时武装指认——v3.5 有侦探时仅侦探可指认，无侦探时持械平民或临时取用桌上凶器）→
自由活动（会客室/书房/监控室/地下室四选一，白天卧室清场；停电声明并入选房菜单；同入邀请；
v3.26：先收齐去向声明（靠后者可见先前声明），再经独立开锁行动轮统一进入；开锁者优先，其余进入规则不变；
v3.11：停电逐人发现——各选房者抵达所选房间时才各自得知停电；房间交谈；
房间行动（含当面赠送道具）；谋杀窗口（v3.27：谋杀/舞台魔术拆为独立环节，不占房间行动）→ 阶段末毁证窗口）→ 晚间休息（v3.22 与自由活动同构：去向=自己卧室/他人卧室/会客室/
书房/监控室/地下室，互不可见选择；同入邀请；密道（反派建筑师，出口四房间）；卧室夜间黑暗、
平民慎入有人的功能房；进入结算后同样是房间交谈（黑暗低语）→ 房间行动（含当面赠送道具/收听窃听器/
取桌上物品）→ 谋杀窗口（同白天，v3.27 拆出）→ 阶段末毁证窗口）→ advance() 进入次日案件调查
（清晨公告含状态行/昨日死亡与未遂（不公告死亡时段）/桌上物品变动/调查自荐/死者字条遗嘱）。
终局（任一胜负条件满足）自动收束并落盘结局——阶段内循环（房间交谈/房间行动/手电筒判定）在
winner 产生后即整环节停止调用（v3.24）；简报「游戏状态」段按清晨公告口径显示死亡与倒计时（v3.24）。

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
                   "talks": {f"{天}:{阶段}:{房间}": [房内交谈行]}（窃听器录音源，仅取自由活动时段）, "out_day": {出局者: 天数}（清晨状态表用）}
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
    ENV_WEAPON,
    FREE_ROOMS,
    HALL,
    MEETING_STAGES,
    PUBLIC_ROOMS,
    ROOM_CAPACITY,
    bedroom_of,
    bedroom_owner,
    countdown_text,
    current_period,
    door_locked,
    held_item,
    held_item_names,
    is_bedroom,
    is_dark,
    is_villain,
    reject,
    skill_left,
    unlock_means,
)
from .fmt import fmt_action, third_person
from .gamedata import ARMED_WEAPONS, WEAPONS, physique_desc
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

# 纯私密动作环节的固定提示（v3.8）：speech 在这些环节不会被任何人看到，明示避免 AI 白写
NO_SPEECH_NOTE = "本环节不发言——speech 返回空字符串（你的话不会被任何人看到；内心独白照常写 inner）"


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
        personas_path = game_dir / "personas.toml"
        self.personas = load_personas(personas_path if personas_path.exists() else PERSONAS_PATH)
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
        self._receipt_open = False  # script.md 行动回执块分隔头是否已打开（连续回执共享一块）
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

    def script_line(self, line: str, _receipt: bool = False) -> None:
        if not _receipt:
            self._receipt_open = False  # 非回执内容切断回执块，下一条回执重新开分隔头
        with open(self.script_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def script_header(self, text: str) -> None:
        self._receipt_open = False
        with open(self.script_path, "a", encoding="utf-8") as f:
            f.write(f"\n## {text}\n\n")

    def script_sub(self, text: str) -> None:
        """阶段内的子分段（###）：自由活动按房间分段等，让全记录层次可读。"""
        self._receipt_open = False
        with open(self.script_path, "a", encoding="utf-8") as f:
            f.write(f"\n### {text}\n\n")

    def _receipt_line(self, text: str) -> None:
        """行动回执：连续回执共享一个分隔块头，与正文（公告/发言/流程行）视觉上明确隔开。"""
        if not self._receipt_open:
            self.script_line("  ── 行动回执（host 档案 · 含秘密行动，剪辑勿公开）" + "─" * 10, _receipt=True)
        self.script_line(f"  {text}", _receipt=True)
        self._receipt_open = True

    def script_private(self, ev: dict) -> None:
        """把事件的私密回执逐条记入全记录（host 侧事实，不进 public/log.md）。console 经 on_private 上屏。
        回执原文是发给 AI 的第二人称，落全记录时转第三人称。"""
        for name, msg in (ev.get("private") or {}).items():
            if not isinstance(msg, str):
                msg = json.dumps(msg, ensure_ascii=False)
            self._receipt_line(f"◆ {name}｜{third_person(name, msg)}")
            self._fire("on_private", name, msg)
        for note in ev.get("host") or []:
            # host 备注：引擎裁定的真实原因（对玩家保密的部分），如"未能进入（房内是杀手）"
            self._receipt_line(f"◆ 主持备注｜{note}")
            self._fire("on_private", "主持备注", note)

    def script_action(self, name: str, act: dict, ev: dict) -> None:
        """记录一次动作声明与裁决结果（含驳回——host 侧可见，不公开）。console 经 on_result 上屏。"""
        ok = bool(ev.get("ok"))
        act_txt = fmt_action(act)
        if not ok:
            self._receipt_line(f"◆ {name}｜{act_txt} → 驳回（{ev.get('error')}）")
            self._fire("on_result", name, ok, f"{act_txt} → 驳回（{ev.get('error')}）")
            return
        priv = (ev.get("private") or {}).get(name)
        self._receipt_line(f"◆ {name}｜{act_txt} → 成功")
        detail = f"{act_txt} → 成功"
        if priv:
            if not isinstance(priv, str):
                priv = json.dumps(priv, ensure_ascii=False)
            self._receipt_line(f"    结果：{third_person(name, priv)}")  # 回执原文是第二人称，落全记录转第三人称
            detail += f"：{third_person(name, priv)}"
        self._fire("on_result", name, ok, detail)

    def _fire(self, name: str, *args) -> None:
        """触发观察者钩子（console 订阅；无头模式不注册即为空操作）。"""
        cb = self.hooks.get(name)
        if cb:
            cb(*args)

    @property
    def lite(self) -> bool:
        """本局是否为 lite 规则集（失忆版）：AI 的规则书没有机制词条与章节，
        凡 full 局指向"规则书 §X"的引用，lite 局改为把对应规则临场内联发给 AI。"""
        return bool(self.state.get("is_lite_rule"))

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

    def _stamp(self, line: str) -> str:
        """给事件行打天数·阶段标记〔第N天·阶段〕（game/5 复盘：互赠/停电/投票结果等记录行无日期，
        跨天后在「关键记录」里极易混淆）。发言行（**名字**：）与"第 N/6 天开始"行自带头绪，不打。"""
        if line.startswith("**") or "/6 天开始" in line:
            return line
        ph = self.state.get("phase", {})
        tag = f"〔第{ph.get('day')}天·{ph.get('stage')}〕"
        return "- " + tag + line[2:] if line.startswith("- ") else tag + line

    def feed_line(self, line: str) -> None:
        """仅公开口径（feed + public/log.md），不进 script.md——供编排器已自记明细的事件去重。"""
        line = self._stamp(line)
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
        msg = self._stamp(msg)
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
        if usage:
            entry["usage"] = dict(usage)  # 全量 usage（input/output/prompt_cache 命中等），供缓存观测与成本核算
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
                if result.reasoning:
                    # 隐藏思考落盘（POV 素材备选；正文解析不受影响）
                    (seat_dir_out / f"day{day}-{label}-thinking.md").write_text(result.reasoning, encoding="utf-8")
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

    def _stage_hints(self, title: str) -> list[str]:
        """本环节规则速查：只放本环节最易误读的规则与关键数字（全文仍以系统提示中的规则书为准）。

        置于简报末尾（动作菜单之前），吃 recency；针对真实对局中暴露的事故——
        如 game/1 双平民把"超过半数"误算成需 3 票（实际 2.5 票即出局）。
        """
        n = len(self.alive())
        if "侦探竞选声明" in title:
            return ["声明时可附带宣告职业与道具——可以撒谎，但这类声明属于【看破伪装】可验证范围，谎言会留档。",
                    "侦探职权：桌上凶器归侦探 + 末置发言 + 一票计 1.5 + 垄断武装指认；出局时警徽随遗嘱传承。",
                    "流程：声明轮之后是密封退选（参选者各自私下决定去留），再由自始未参选者投票。"]
        if "侦探竞选退选" in title:
            return ["退选即失去当选资格；留任候选人只剩 1 人则直接当选。",
                    "无人留任（全部退选/无人参选/全员参选）→ 本局不设侦探，不再补选。"]
        if "侦探竞选投票" in title:
            return ["只有自始未参选者（不含退选者）有投票权，每人 1 票。", "平票 → 本局不设侦探，不再补选。"]
        if "公共发言" in title:
            return ["侦探末置发言（若侦探存活）。发言结束后（第 2 天起）进入投票。"]
        if "投票" in title:
            need = (n + 1) / 2  # 严格过半的最小票值（0.5 步进）：n=4→2.5，n=5→3，n=6→3.5
            accuse_ref = ("见规则书 §7.3" if not self.lite else
                          "持武器的平民可当众指认某人为杀手：指对则当前杀手出局（帮凶存活会继任，反派全灭才平民获胜）、"
                          "指错指认者出局；资格细节届时见菜单")
            return [f"存活 {n} 人：出局线 = 得票严格超过 {n}/2，即至少 {need:g} 票（普通票 1、侦探票 1.5）——"
                    f"{need - 0.5:g} 票不够。",
                    "可弃权；平票或未过线 → 本轮无人出局。",
                    f"本轮无人出局时，之后可能进入武装指认（{accuse_ref}）。"]
        if "武装指认" in title:
            return ["每次投票至多接受一次指认，按行动顺序先声明者优先。",
                    "指认对象确为杀手 → 当前杀手出局（帮凶存活会继任，反派全灭才平民获胜）；指错（帮凶/平民）→ 你直接出局。"]
        if "死者字条" in title or "遗言" in title:
            return ["遗言/死者字条在身份公布之前——可以撒谎。",
                    "有未使用的遗留道具时一并立遗嘱：逐件指定归属；接收人可拒收；未指定/被拒收的道具不销毁，"
                    "放回会客室桌上；接受的道具当天即可用。"]
        if "遗嘱回应" in title:
            return ["拒收的道具不销毁，放回会客室桌上；接受的道具当天即可用。"]
        if "案件调查" in title:
            return ["现场调查三项任选一项：武器伤痕 / 死亡时间 / 尸体转移；多起案件每起都可查，同一项可被多人重复验证。",
                    "结果仅你本人可见——公布、隐瞒或撒谎由你决定。"]
        if "选房" in title:
            return ["顺序靠后者可见顺序靠前者的去向声明；声明收齐后先统一开锁，再统一进入。",
                    "房间满员/上锁而无人开门/被【封闭空间】挡住 → 滞留会客室。"]
        if "开锁行动轮" in title:
            return ["本轮只处理门锁与开锁次数，不移动任何人；开锁者在随后的进入轮优先。",
                    "actions 为 [] 表示不愿意消耗手段；同批有人开门后，其他声明者仍可正常尝试进入。"]
        if "内交谈" in title:
            return ["按行动顺序各发言一次，后发言者能看到先发言者的话。",
                    "黑暗房间内无法交谈——除非同入邀请成立的伙伴互知在场，可摸黑低语：低语会被房间内所有在场者听到（包括你们看不见的潜伏者），而你们不会知道是否有人旁听。"]
        if "房间行动" in title:
            return ["非黑暗时，房间行动（查监控/开箱/修发电机/赠送道具/报案）会被同房者看见；黑暗中无人知晓是谁做的。",
                    "当面赠送：限同房者，对方可拒绝；赠送算你的本阶段房间行动（谋杀不在此环节，见谋杀窗口）。",
                    "报案也算你的本阶段房间行动；不报案则无人知道你发现了尸体（除非你亲口说出）。"]
        if "谋杀窗口" in title:
            return ["每阶段独立一次谋杀机会（自由活动与晚间休息各一次），放弃不消耗——没有目标也不消耗。",
                    "谋杀不占房间行动，房间行动也不占谋杀机会（v3.27 拆为两个独立环节）。",
                    "动作被驳回 = 未发生袭击，不消耗机会；谋杀失败 = 已进入攻击判定但被防御，消耗机会，且自第 2 天起重置无杀倒计时。"]
        if "密谈" in title or "夜谈" in title or "低语" in title:
            return ["密谈/低语内容只有房间内的人能听到（含你们看不见的潜伏者）；按行动顺序各发言一次。"]
        if "晚间休息" in title:
            return ["去向选择互不可见；他人卧室上锁（需开锁手段或受邀）；卧室夜间黑暗。",
                    "平民不敢非受邀进入已有他人的书房/监控室/地下室（被邀请者与反派不受此限）。",
                    "夜宿会客室：始终明亮、人数不限，但夜宿者名单次日清晨公开。",
                    "若你进入的房间有尚未公告的尸体，你会当场私下发现（见私密事件）；是否报案见房间行动菜单。"]
        if "秘密布置" in title:
            return ["反派建筑师只需确认密道网存在，无需选定出口；之后可在晚间休息时使用。"]
        if "毁证机会" in title:
            return ["仅当现场没有其他非反派角色时：杀手可缴获尸体上的未使用道具、可发动毁证类技能/道具。"]
        return []

    def _game_state_lines(self) -> list[str]:
        """「游戏状态」段：公开事实速查——角色表（性别/体格/国籍，开局已全员公布）、存活状态、
        侦探、无杀倒计时。出局者身份职业已公开（§7.3）故列出；被谋杀者只列死亡信息与
        已公开职业，身份不公开（§9.4）。声称的职业/道具不在此列——那是发言内容，由角色自己记笔记。
        每阶段重发是为了让 AI 不必从笔记里推算局面（game/8 复盘：角色反复搞错存活与道具台账）。
        死亡与倒计时一律按清晨公告口径（§7.1：死亡次日清晨才公告）——当天发生的谋杀在当天简报里
        仍显示遇害者存活、倒计时保持清晨公告值（game/45 复盘：谋杀后同阶段简报把死亡、案发房间与
        倒计时重置泄漏给未到场者，含本应无从得知的场外反派）。"""
        s = self.state
        day = s["phase"]["day"]
        pending = {c["victim"] for c in s["cases"] if c["day"] >= day}  # 当天已发生、次日清晨才公告的死亡
        announced = {c["victim"]: c for c in s["cases"] if c["day"] < day}
        alive_n = sum(1 for q in s["players"] if q["alive"]) + len(pending)
        # 谋杀行为（含未遂）当天不公告——倒计时显示以"昨天及以前"的末次行为为准，与清晨公告值一致
        attack_days = [c["day"] for c in s["cases"]] + [a["day"] for a in s["attempts"]]
        last_public = max([d for d in attack_days if d < day], default=None)
        lines = [f"- 进度：第 {day}/6 天 · {s['phase']['stage']}｜存活 {alive_n} 人｜侦探：{s.get('detective') or '无'}"
                 f"｜{countdown_text(day, last_public)}"]
        # 供电状态（v3.20）：仅停电中或当日停电已修复时显示——白天修复后 AI 仍误以为停电的案例见 game/838823
        # v3.23：blackout_news 非空 = 当天刚秘密拉闸、尚未在选房收尾公布（rules-6 §7.5）——
        # 此时「游戏状态」段不得显示供电行，否则选房者在进入房间前就能提前得知停电
        if s["power_out"] and not s.get("blackout_news"):
            lines.append("- 供电：停电中（除会客室外全部黑暗、门锁失效、监控停止记录；修复发电机前不会恢复）")
        elif (s.get("power_restored") or "").startswith(f"第{day}天"):
            lines.append("- 供电：今日停电已被修复（门锁与监控正常工作；停电造成的黑暗已解除，地下室与夜间卧室仍黑暗）")
        out_day = self.orch.get("out_day", {})
        prof_pub = set(s.get("profession_public", []))
        roster = []
        for q in s["players"]:
            n = q["name"]
            base = f"{n}（{q['gender']}·{physique_desc(q['physique'])}·{q['nationality']}"
            if q["alive"] or n in pending:
                roster.append(base + "·存活）")
            elif n in out_day:
                roster.append(base + f"·第{out_day[n]}天出局：{q['identity']}/{q['profession']}）")
            elif n in announced:
                c = announced[n]
                prof = f"，职业已公开：{q['profession']}" if n in prof_pub else ""
                roster.append(base + f"·第{c['day']}天死亡（{c['room']}）{prof}）")
            else:
                roster.append(base + "·出局）")
        lines.append("- 角色表（开局全员公布）：" + "、".join(roster))
        # v3.11：永久开启台账（替代选房简报的锁状态标注）——只列 locks 台账与保险箱，
        # 与停电的临时锁失效无关，因此不会从这一行泄漏停电
        opened = []
        if s["locks"].get("监控室") is False:
            opened.append("监控室的门")
        if s.get("safe_open"):
            opened.append("书房保险箱")
        lines.append("- 已永久开启：" + ("、".join(opened) if opened else "（暂无；监控室开局上锁，首开需【暴力破解】手段）"))
        return lines

    def _status_lines(self, p: dict) -> list[str]:
        """「你的状态」台账段：每阶段由系统明示身份/技能剩余/道具剩余（game/2 复盘：索尔笔记残留
        已消耗的刀 → 误报凶器被驳回。台账以引擎状态为准，不依赖 AI 自己的笔记）。"""
        name = p["name"]
        sk = self.state.get("skill_uses", {}).get(name, {})
        skills = "、".join(f"{k}×{v}" if v > 0 else f"{k}（已用完）" for k, v in sk.items()) or "无"
        items = "、".join(f"{e['name']}×{e['uses_left']}" for e in p.get("items", []) if e["uses_left"] > 0) or "无"
        lines = [f"- 身份：{p['identity']}｜职业：{p['profession']}",
                 f"- 技能剩余：{skills}",
                 f"- 道具：{items}"]
        if self.state.get("detective") == name:
            lines.append("- 你是侦探：公共发言末置发言、一票计 1.5、垄断武装指认（出局时警徽按遗嘱传承）")
        return lines

    def _dark_partners(self, name: str, room: str) -> list[str]:
        """同入邀请成立且当前同处一室的伙伴（黑暗房间低语资格——互知在场，rules-6 §7.5.6/§7.7）。"""
        invites = self.orch.get("night_invites") if self.state["phase"]["stage"] == "晚间休息" \
            else self.orch.get("free_invites")
        partners = []
        for t, v in (invites or {}).items():
            if t == name and self.state["positions"].get(v) == room:
                partners.append(v)
            elif v == name and self.state["positions"].get(t) == room:
                partners.append(t)
        return partners

    def _weapon_hint(self, p: dict, room: str | None) -> str:
        """谋杀菜单的实时凶器提示：持有凶器逐件列出（含次数），无凶器则明示只能空手。"""
        held = [f"{e['name']}×{e['uses_left']}" for e in p.get("items", [])
                if e["uses_left"] > 0 and e["name"] in WEAPONS]
        if held:
            parts = ["你当前持有凶器：" + "、".join(held)]
        elif self.lite:
            # lite 规则书无 §7.6：空手条件临场内联发给杀手
            parts = ["你当前未持有凶器（只能空手。空手谋杀需满足其一：帮凶在场 / 你体格不弱于目标 / "
                     "你男而目标女；黑暗房间（含夜间卧室）中无任何附加条件——空手不被凶器克制，"
                     "但仍可能被急救箱/敏捷闪避抵消）"]
        else:
            parts = ["你当前未持有凶器（只能空手，空手条件见规则 §7.6）"]
        env = ENV_WEAPON.get(room or "")
        if env:
            parts.append(f"本房间环境凶器：{env}（不限次数）")
        return "；".join(parts)

    @staticmethod
    def _compact_public(lines: list[str]) -> list[str]:
        """关键记录公共段压缩（仅简报呈现层；state/feed/public/script 全量保留）。

        - 竞选仪式折叠：已有竞选结果行时，丢弃宣告参选/不参选与退选结果行（只留结果行）；
        - 日序/存活/倒计时三类状态行各只保留最新一条；事件行（死亡/投票/遗嘱/监控宣读等）全保留。
        """
        has_result = any("侦探竞选：" in x or "本局不设侦探" in x for x in lines)
        kinds = ("/6 天开始", "目前存活", "无杀倒计时：")
        last = {k: max((i for i, x in enumerate(lines) if k in x), default=-1) for k in kinds}
        out = []
        for i, x in enumerate(lines):
            if has_result and ("宣告参选侦探" in x or "宣告不参选" in x or "退选结果：" in x):
                continue
            if any(k in x and i != last[k] for k in kinds):
                continue
            out.append(x)
        return out

    @staticmethod
    def _compact_private(lines: list[str]) -> list[str]:
        """关键记录私密段压缩：监控全量复读（格式本身累计历史）只留最新一条；其余事件全保留。"""
        last = max((i for i, x in enumerate(lines) if x.startswith("监控记录（公开口径")), default=-1)
        return [x for i, x in enumerate(lines) if not x.startswith("监控记录（公开口径") or i == last]

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
        dark = bool(pos and is_dark(self.state, pos))
        roommates = []
        if pos and (not dark or is_villain(p)):
            roommates = [
                q["name"]
                for q in self.alive()
                if q["name"] != name and self.state["positions"].get(q["name"]) == pos
            ]
        pub_hist = [self._bullet(self._clip(x)) for x in self._compact_public(feed[:cursor]) if not x.startswith("**")]
        hist = self.orch["hist"].get(name, [])
        priv_hist = self._compact_private(hist[: len(hist) - len(private)] if private else hist)
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
            if dark:
                partners = self._dark_partners(name, pos)
                if is_villain(p):
                    present = "、".join(roommates) if roommates else "无"
                    pos_line += f"（房间黑暗：你仍知道在场者：{present}；你只知道他们是谁，看不见具体行为"
                    if partners:
                        pos_line += "；受邀组合仍可摸黑低语，其余黑暗规则不变）"
                    else:
                        pos_line += "，也无法交谈；其余黑暗规则不变）"
                elif partners:
                    # v3.17：同入邀请成立的伙伴互知在场，可摸黑低语（rules-6 §7.5.6）
                    pos_line += (f"（房间黑暗：你看不见这里有谁——但你知道 {'、'.join(partners)} 应邀同来、就在身边，"
                                 "可以摸黑低声交谈；当心被你们看不见的潜伏者旁听）")
                else:
                    pos_line += "（房间黑暗：你看不见这里有谁，也无法交谈）"
            else:
                pos_line += f"；同房者：{'、'.join(roommates) if roommates else '无'}"
        lines.append(f"## 当前局面\n{pos_line}")
        lines.append("")
        lines.append("## 游戏状态（公开事实速查——与你的笔记冲突时以此为准）\n" + "\n".join(self._game_state_lines()))
        lines.append("")
        lines.append("## 你的状态\n" + "\n".join(self._status_lines(p)))
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
        hints = self._stage_hints(title)
        if hints:
            lines.append("")
            lines.append("## 本环节规则速查")
            lines += [f"- {h}" for h in hints]
        lines.append("")
        lines.append("## 本环节合法动作（JSON 示例）")
        lines += menu or ["- 本环节无动作可做，actions 返回 []"]
        return "\n".join(lines) + "\n"

    def reject_note(self, p: dict, ev: dict) -> None:
        msg = f"你的动作被驳回：{ev.get('error', '未知原因')}"
        if ev.get("type") in ("murder", "stage_magic"):
            msg += "（本次未发生袭击，不消耗本阶段谋杀机会，也不重置无杀倒计时）"
        self.tell(p["name"], msg)

    def apply_actions(self, p: dict, actions: list[dict], retry: dict | None = None,
                      max_actions: int | None = None) -> None:
        """逐条派发动作到引擎；非法动作驳回并私下告知本人，不影响其余动作。

        传 retry 上下文（label/title/menu/extra）时：驳回后立即私下重报一次（dev-plan「驳回并要求重报」，
        game/2 复盘：夜袭误报已消耗凶器被驳回且无重报机会，直接改写胜负）。重报以「label#驳回重报」
        独立存档（inbox/outbox/host_log/console 同口径）；二次驳回不再追问（reject_note 已留档）。
        选房/归宿等有兜底语义（滞留/默认回房）的环节不接重报。
        max_actions 用于房间行动等单动作环节：超量声明整批驳回，不先执行其中任何动作。
        """
        def batch_rejection(count: int) -> tuple[dict, dict]:
            act = {"type": "action_batch", "count": count}
            ev = reject("action_batch",
                        f"本环节每人只能选择 {max_actions} 个动作；你一次声明了 {count} 个，已全部驳回，请只保留一个")
            self.script_action(p["name"], act, ev)
            self.reject_note(p, ev)
            return act, ev

        rejected: list[tuple[dict, dict]] = []
        if max_actions is not None and len(actions) > max_actions:
            rejected.append(batch_rejection(len(actions)))
        else:
            for act in actions:
                if not isinstance(act, dict):
                    continue
                ev = self.dispatch(p, act)
                if ev is not None and not ev.get("ok"):
                    rejected.append((act, ev))
        if not rejected or retry is None:
            return
        reasons = "；".join(f"{fmt_action(a)} → 驳回（{e.get('error', '未知原因')}）" for a, e in rejected)
        extra = (retry.get("extra") or "") + \
            f"\n【主持人】你刚才的动作声明被驳回：{reasons}。请重新声明本阶段动作（放弃则 actions 为 []；speech 可留空，不会重复公开）。"
        briefing = self.build_briefing(p, retry["title"], retry["menu"], extra)
        payload = self.call_ai(p, retry["label"] + "#驳回重报", briefing)
        retry_actions = payload["actions"]
        if max_actions is not None and len(retry_actions) > max_actions:
            batch_rejection(len(retry_actions))
            return
        for act in retry_actions:
            if isinstance(act, dict):
                self.dispatch(p, act)

    def _resolve_case(self, act: dict) -> tuple[dict | None, dict | None]:
        """把 AI 动作中的案件引用解析为案件 dict。返回 (案件, None) 或 (None, 驳回事件)。

        主口径：受害者名字（"case" 字段，容忍 "victim" 字段、「」/引号与"案"后缀）；
        兼容旧式整数 case_id（含 "案件1"/"#1" 等可提取数字的写法）；
        全局只剩 1 起案件时缺省或写错自动归一。解析失败 → 驳回并附当前案件名单。
        """
        cases = self.state["cases"]
        raw = act.get("case", act.get("victim"))
        if raw is not None:
            name = str(raw).strip().strip("「」\"'").removesuffix("案").strip()
            hit = next((c for c in cases if c["victim"] == name), None)
            if hit:
                return hit, None
        raw_id = act.get("case_id")
        if raw_id is not None:
            m = re.search(r"\d+", str(raw_id))
            if m:
                hit = next((c for c in cases if c["id"] == int(m.group())), None)
                if hit:
                    return hit, None
        if len(cases) == 1:
            return cases[0], None
        listing = "、".join(f"{c['victim']}（{c['room']}）" for c in cases) or "（无）"
        return None, reject(str(act.get("type", "?")),
                            f"未找到对应案件——请用受害者名字指认（当前案件：{listing}）")

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
        elif at == "place_bug":
            ev = self.eng.place_bug(name, str(act.get("room", "")))
        elif at == "bug_listen":
            ev = self.eng.bug_listen(name)
            if ev.get("ok"):
                placed = self.state.get("bug_placed") or {}
                if placed.get("day") == day:
                    content = "（窃听器放置后，当天自由活动尚无新的交谈记录；此前交谈不追溯）"
                else:
                    lines = self.orch["talks"].get(f"{day}:自由活动:{ev.get('room')}", [])
                    content = "；".join(lines) if lines else "（当天该房间无人交谈）"
                self.tell(name, f"窃听器录音（{ev.get('room')} · 今天自由活动）：{content}")
                self.script_line(f"  窃听器录音下发给 {name}（{ev.get('room')}）：{content}")
        elif at == "take_table":
            ev = self.eng.take_table_item(name, str(act.get("item", "")))
        elif at == "gift":
            return self._gift(p, act)  # v3.27：当面赠送——含接收方同意流，自行记录与路由
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
        elif at == "report":
            ev = self.eng.report(name)
        elif at == "declare_blackout":
            ev = self.eng.declare_blackout(name)
        elif at in ("falsify", "move_body", "investigate", "profile"):
            # 案件指认主口径 = 受害者名字（"case"/"victim" 字段）；兼容旧式整数 case_id。
            # 解析失败走驳回重报，绝不允许异常炸穿跑局（game/18 复盘）
            case, err = self._resolve_case(act)
            if err is not None:
                ev = err
            elif at == "falsify":
                ev = self.eng.falsify(name, case["id"], str(act.get("kind", "")))
            elif at == "move_body":
                ev = self.eng.move_body(name, case["id"], str(act.get("to_room", "")))
            elif at == "investigate":
                ev = self.eng.investigate(name, case["id"], str(act.get("choice", "")))
            else:
                ev = self.eng.profile(name, case["id"], str(act.get("aspect", "")))
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
        ph = self.state["phase"]
        if not self.hooks.get("on_stage"):  # 无头模式的阶段标记；console 由 on_stage 画双线横幅
            print(f"== 进入 第{ph['day']}天 · {ph['stage']} ==")
        # 阶段横幅先于本阶段公告上屏（console 复盘：清晨的行动顺序/存活/倒计时曾跑到上阶段结束分隔符上方）
        self._fire("on_stage", title)
        self.route(ev)
        if self.state["phase"]["stage"] == "案件调查":
            self.script_morning_status()
        self.save_state()
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
        """开局准备：仅反派建筑师需要确认密道；窃听器改为后续到场放置。"""
        if not self.orch["feed"]:
            # 开局公告（保险箱内容/桌上凶器，§2 公开信息）此前只进 script.md/console，从未送达玩家
            # （game/5 复盘：全员引用"开局公告"但内容靠编）。新局首发进 feed。
            # script.md 开头 _init_script 已写「开局公告（主人字条）」整节——这里只送 feed/public/console，不进 script（避免重复）
            ann = self.dir / "public" / "announcement.md"
            if ann.exists():
                for ln in ann.read_text(encoding="utf-8").strip().splitlines():
                    ln = ln.strip()
                    if ln and not ln.startswith("#"):
                        self.feed_line(ln)
                        self._fire("on_public", ln)
        day = self.state["phase"]["day"]
        w = self.wip(f"{day}:开局准备:布置", done=[])
        for p in self.alive():
            if p["name"] in w["done"]:
                continue
            menu = []
            if is_villain(p) and p["profession"] == "建筑师" and skill_left(self.state, p["name"], "建造密道") > 0:
                menu.append('- 确认密道（无需指定出口；晚间休息时可经密道前往书房/监控室/地下室）：{"type": "set_passage"}')
            if not menu:
                continue
            briefing = self.build_briefing(p, "开局准备 · 秘密布置", menu,
                                           f"开局前的秘密布置环节。这是你的开局义务，现在声明。\n{NO_SPEECH_NOTE}")
            payload = self.call_ai(p, "开局准备", briefing)
            for act in payload["actions"]:
                if act.get("type") == "set_passage":
                    ev = self.eng.set_passage(p["name"])
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
                     stay=[], voted=[], votes={}, resolved=False, speeches={}, vote_speeches={})
        alive_names = [p["name"] for p in self.alive()]
        for name in self.state["order"]:
            p = self.player(name)
            if not p or not p["alive"] or name in w["declared"]:
                continue
            menu = ['- 宣告是否参选侦探：{"type": "declare", "run": true|false}',
                    "- 声明写在 speech 字段（简短表态；可顺带宣告你的职业与道具——可以撒谎，但谎言会留档）"]
            briefing = self.build_briefing(
                p, f"第{day}天 · 侦探竞选声明", menu,
                "现在竞选侦探。侦探职权：当选即获得会客室桌上凶器（侦探佩枪）、公共发言末置发言、投票时一票计 1.5 票、"
                "垄断武装指认（有侦探时仅侦探可指认）；出局时警徽按遗嘱传承（可指定归属或销毁），佩枪按普通道具处理。"
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
            speeches: dict[str, str] = dict(w["speeches"])
            for name in w["ran"]:
                if name in decided:
                    continue
                p = self.player(name)
                menu = ['- 继续参选：{"type": "elect_stay"}',
                        '- 退选（退选者同样无投票权）：{"type": "withdraw"}']
                briefing = self.build_briefing(
                    p, f"第{day}天 · 侦探竞选退选", menu,
                    f"参选者：{'、'.join(w['ran'])}。留任/退选决定在所有人作答后一齐公布——你可以在 speech 里附一句理由，"
                    "将随结果一齐公开。若最终留任多人，由未参选者投票；平票或全员退选则本局不设侦探。")
                payload = self.call_ai(p, "竞选退选", briefing)
                sp = str(payload.get("speech") or "").strip()
                if sp:
                    speeches[name] = sp
                decided[name] = not any(a.get("type") == "withdraw" for a in payload["actions"])
                w["decided"] = dict(decided)
                w["speeches"] = dict(speeches)
                self.save_state()
                self.check_stop()
            w["stay"] = [n for n in w["ran"] if decided.get(n, True)]
            w["withdrew"] = True
            dropped = [n for n in w["ran"] if n not in w["stay"]]
            self.log_line(f"退选结果：{('、'.join(dropped) + ' 退选；') if dropped else '无人退选；'}"
                          f"留任候选人：{'、'.join(w['stay']) or '无'}")
            for n in w["ran"]:  # 密封的是决定而非内容：附言随结果一齐公开（v3.8）
                if speeches.get(n):
                    self.log_line(f"**{n}**（退选揭晓）：{speeches[n]}")
            self.save_state()
        stay = list(w["stay"]) if w["withdrew"] else list(w["ran"])
        voters = [n for n in alive_names if n not in w["ran"]]
        if len(stay) >= 2 and voters and not w["resolved"]:
            votes: dict[str, str | None] = dict(w["votes"])
            vote_speeches: dict[str, str] = dict(w["vote_speeches"])
            for name in self.state["order"]:
                if name not in voters or name in w["voted"]:
                    continue
                p = self.player(name)
                menu = [f'- 投票选侦探：{{"type": "elect", "target": "{"|".join(stay)}"}}',
                        '- 弃权：{"type": "abstain"}']
                briefing = self.build_briefing(
                    p, f"第{day}天 · 侦探竞选投票", menu,
                    f"留任候选人：{'、'.join(stay)}。未参选者投票，简单多数当选；平票则本局不设侦探。"
                    "票型不公开，但你可以在 speech 里附一句理由，开票后公开。")
                payload = self.call_ai(p, "竞选投票", briefing)
                sp = str(payload.get("speech") or "").strip()
                if sp:
                    vote_speeches[name] = sp
                votes[name] = None
                for act in payload["actions"]:
                    if act.get("type") == "elect" and str(act.get("target", "")) in stay:
                        votes[name] = str(act["target"])
                w["votes"] = dict(votes)
                w["vote_speeches"] = dict(vote_speeches)
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
            for n in self.state["order"]:  # 竞选投票附言随开票公开（v3.8）
                if w["vote_speeches"].get(n):
                    self.log_line(f"**{n}**（竞选投票）：{w['vote_speeches'][n]}")
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
                if self.lite:
                    # lite：不下发职业/道具全表，示例不得泄漏合法名目——一律"照抄对方声明中的原名"
                    menu.append('- 看破伪装（自己发言前。claims 只填你想验证的项；值照抄对方声明中的原名——对方自称什么职业就填什么职业名、'
                                '对方提到什么道具就填什么道具名、房间填房间名；填整句描述属于非法名目，全部非法则驳回、不消耗次数。'
                                '示例——验证对方声称去过书房：{"type": "see_through", "target": "对方名字", "claims": {"visited_rooms": ["书房"]}}。'
                                '行踪以监控记录为准；存在停电空白时"无记录"不构成谎言证据（判"无法验证"，不计入谎言判定）')
                else:
                    menu.append('- 看破伪装（自己发言前。claims 只填你想验证的项，值必须是规则书原名——职业如 "黑客"、道具如 "刀"、房间如 "书房"；填整句描述属于非法名目，全部非法则驳回、不消耗次数。'
                                '示例——验证对方自称黑客且声称去过书房：{"type": "see_through", "target": "对方名字", "claims": {"profession": "黑客", "visited_rooms": ["书房"]}}。'
                                '行踪以监控记录为准；存在停电空白时"无记录"不构成谎言证据（判"无法验证"，不计入谎言判定）')
            menu.append("- 发言写在 speech 字段；无需动作时 actions 为 []")
            briefing = self.build_briefing(p, f"第{day}天 · 公共发言（轮到你）", menu,
                                           "现在轮到你公开发言。分析、指控、质疑、声明、辩护均可。")
            payload = self.call_ai(p, "公共发言", briefing)
            speech = str(payload.get("speech") or "").strip()
            if speech:
                self.log_line(f"**{name}**：{speech}")
            self.apply_actions(p, payload["actions"], retry={
                "label": "公共发言", "title": f"第{day}天 · 公共发言（轮到你）", "menu": menu,
                "extra": "现在轮到你公开发言。分析、指控、质疑、声明、辩护均可。（发言已收录，无需重复）"})
            w["done"].append(name)
            self.save_state()
            self.check_stop()

    def _gift(self, p: dict, act: dict) -> dict:
        """当面赠送（v3.27）：房间行动中的赠予动作——引擎校验（同房/每天限一次/持有道具），
        接收方单独回应接受/拒绝（回应不消耗其房间行动）。明亮房间同房者可见，黑暗无人知晓。"""
        name = p["name"]
        to = str(act.get("to", ""))
        item = str(act.get("item", ""))
        if not item:
            ev = reject("gift", "请指定要赠送的道具名")
            self.script_action(name, act, ev)
            self.reject_note(p, ev)
            return ev
        target = self.player(to)
        if target is None or not target["alive"]:
            ev = reject("gift", "接收方必须是存活玩家")
            self.script_action(name, act, ev)
            self.reject_note(p, ev)
            return ev
        ask = self.build_briefing(
            target, f"第{self.state['phase']['day']}天 · 赠送回应",
            ['- 接受：{"type": "accept"}', '- 拒绝：{"type": "decline"}'],
            f"{name} 想把「{item}」当面送给你。是否接受？" + f"\n{NO_SPEECH_NOTE}")
        reply = self.call_ai(target, f"赠送回应-{name}", ask)
        accept = any(a.get("type") == "accept" for a in reply["actions"])
        ev = self.eng.transfer(name, to, item, accept)
        self.script_action(name, act, ev)
        if ev["ok"]:
            self.route(ev)
        else:
            self.reject_note(p, ev)
        return ev

    def murder_menu(self, p: dict, day: int) -> list[str]:
        """谋杀窗口菜单（v3.27）：谋杀/舞台魔术从房间行动拆出后的独立小菜单。"""
        s = self.state
        name = p["name"]
        if s["stage_murder_used"]:
            return []
        room = s["positions"].get(name)
        targets = [q["name"] for q in self.alive()
                   if q["name"] != name and s["positions"].get(q["name"]) == room and not is_villain(q)]
        if not targets:
            return []
        menu = []
        if p["identity"] == "杀手" or name in s["passage_stealth"]:
            # v3.32：经密道进场的帮凶同获当阶段谋杀权（击杀归属发动者本人）
            menu.append(f'- 谋杀（每阶段一次）：{{"type": "murder", "target": "{"|".join(targets)}"}}（可加 "weapon": "凶器名"；{self._weapon_hint(p, room)}）')
            if room == HALL:
                # v3.31：会客室桌上凶器视同环境凶器临时取用（无需持有，用后消耗）
                table_weapons = [e["name"] for e in s["table_items"] if e["name"] in WEAPONS]
                if table_weapons:
                    menu.append('- 使用会客室桌上凶器（视同环境凶器临时取用，无需持有，用后消耗，'
                                f'次日清晨公告桌上变动）：{{"type": "murder", "target": "{"|".join(targets)}", '
                                f'"weapon": "{"|".join(table_weapons)}"}}')
        if is_villain(p) and p["profession"] == "魔术师" and skill_left(s, name, "舞台魔术") > 0:
            menu.append(f'- 舞台魔术（任意人数在场也可空手强杀，不可防御；占用当阶段谋杀机会）：{{"type": "stage_magic", "target": "{"|".join(targets)}"}}')
        return menu

    def murder_window(self, day: int, stage: str) -> None:
        """谋杀窗口（v3.27）：房间行动结束后的独立环节——杀手/反派魔术师单独决定是否动用
        本阶段谋杀机会。从房间行动拆出（试玩期实测杀手胜率偏低：拆出后"杀人"不再与"用设施"
        互斥，杀手每阶段可多做一个非谋杀行动）。谋杀/舞台魔术仍是本阶段唯一一次谋杀机会。"""
        w = self.wip(f"{day}:{stage}:谋杀", done=[])
        for name in self.state["order"]:
            if self.state["winner"]:
                return
            if name in w["done"]:
                continue
            p = self.player(name)
            if not p or not p["alive"]:
                continue
            menu = self.murder_menu(p, day)
            if not menu:
                if p["identity"] == "杀手" and not self.state["stage_murder_used"]:
                    room = self.state["positions"].get(name) or "当前位置"
                    self.tell(name, f"本阶段（{stage}）你在{room}没有可攻击目标；未发生袭击，"
                                    "不消耗本阶段谋杀机会，也不重置无杀倒计时")
                    w["done"].append(name)
                    self.save_state()
                continue
            extra = (f"这是本阶段（{stage}）的谋杀窗口——房间行动已结束，现在只需决定：动手，还是收手。"
                     "声明发动即消耗机会——失败（被自卫/技能抵消）同样消耗；放弃或没有目标则不消耗。"
                     f"\n{NO_SPEECH_NOTE}")
            briefing = self.build_briefing(p, f"第{day}天 · {stage}（谋杀窗口）", menu, extra)
            payload = self.call_ai(p, f"{stage}-谋杀窗口", briefing)
            self.apply_actions(p, payload["actions"], retry={
                "label": f"{stage}-谋杀窗口", "title": f"第{day}天 · {stage}（谋杀窗口）",
                "menu": menu, "extra": extra}, max_actions=1)
            w["done"].append(name)
            self.save_state()
            self.check_stop()

    def run_free_activity(self) -> None:
        """自由活动：停电声明轮（选房前）→ 秘密选房+邀请 → 结算 → 手电筒判定 → 房间交谈 → 房间行动 → 谋杀窗口。"""
        day = self.state["phase"]["day"]
        period = current_period(self.state)
        if self.state["rooms_entered"] != period:
            self.select_rooms(day, period)
        if self.state["rooms_entered"] != period:
            return
        self.flashlight_phase(day, "自由活动")
        self.room_talks(day)
        self.room_actions(day)
        self.murder_window(day, "自由活动")

    def flashlight_phase(self, day: int, stage: str) -> None:
        """手电筒判定（v3.23 独立结算，进入结算后、交谈与行动之前）：身处黑暗房间（地下室/停电
        房间/夜间卧室）且持有手电筒者按行动顺序逐一决定是否使用——点亮后房间本阶段视为明亮：
        全员互相可见、可正常交谈、谋杀按亮房条件判定（§7.5/§7.7）。已被点亮的房间不再重复询问。"""
        w = self.wip(f"{day}:{stage}:手电筒", done=[])
        for name in self.state["order"]:
            if self.state["winner"]:
                return
            if name in w["done"]:
                continue
            p = self.player(name)
            if not p or not p["alive"]:
                continue
            room = self.state["positions"].get(name)
            if not room or not is_dark(self.state, room) or held_item(p, "手电筒", day) is None:
                continue  # 无手电筒/房间不黑暗/已被他人点亮 → 不询问（不占 done，天亮前变化与本环节无关）
            menu = ['- 使用手电筒（点亮本房间：本阶段视为明亮——所有人互相可见、可正常交谈、谋杀按亮房条件判定）：{"type": "use_flashlight"}',
                    "- 不使用则 actions 为 []"]
            extra = (f"你在{room}——这里一片漆黑，你看不见这里有谁。你可以现在点亮手电筒"
                     f"（消耗 1 次，点亮后包括你在内的所有人都藏不住了）。\n{NO_SPEECH_NOTE}")
            briefing = self.build_briefing(p, f"第{day}天 · {stage}（手电筒）", menu, extra)
            payload = self.call_ai(p, f"{stage}-手电筒", briefing)
            self.apply_actions(p, payload["actions"])
            w["done"].append(name)
            self.save_state()
            self.check_stop()

    def blackout_declarations(self, day: int) -> None:
        """停电声明轮（rules-6 §7.5：本阶段开始前秘密声明，进入房间后才向众人公布）。

        存活反派中持任一【暴力破解】手段者按行动顺序逐一秘密决定；声明即时生效——选房整段
        监控缺失、门锁失效、除会客室外黑暗（首位进入落定前完成，保证记录不虚报、开锁成本一致）；
        公开公告由 select_rooms 在选房收尾后统一发出。无符合条件者则整轮零调用。
        """
        s = self.state
        if s["winner"] or s["power_out"] or s.get("selection_begun") == current_period(s):
            return
        w = self.wip(f"{day}:自由活动:停电声明", done=[])
        for name in s["order"]:
            if s["power_out"]:
                break  # 已有一人声明成功，其余反派无需再被询问（重复声明也只会被引擎驳回）
            p = self.player(name)
            if not p or not p["alive"] or not is_villain(p) or name in w["done"]:
                continue
            if unlock_means(s, name) is None:
                continue
            menu = ['- 秘密声明停电（拉电闸制造过载，消耗一次【暴力破解】——职业技能/「万能钥匙」/「螺丝起子」任一；停电中门锁失效、监控缺失、除会客室外全部黑暗；停电持续到有人前往地下室修复发电机为止——无人修复则跨天延续，不会自动恢复；进入房间后才向众人公布）：{"type": "declare_blackout"}',
                    '- 不声明则 actions 为 []（本环节只有这一项决定，speech 可留空）',
                    f'- {NO_SPEECH_NOTE}']
            briefing = self.build_briefing(p, f"第{day}天 · 自由活动（停电声明）", menu,
                                           "自由活动即将开始。你可以在进入任何房间之前秘密拉电闸。")
            payload = self.call_ai(p, "自由活动-停电声明", briefing)
            for act in payload["actions"]:
                if not isinstance(act, dict) or act.get("type") != "declare_blackout":
                    continue
                ev = self.eng.declare_blackout(name)
                self.script_action(name, act, ev)  # 记入全记录并在 console 上屏（对玩家仍保密）
                if ev["ok"]:
                    self.route(ev)
                else:
                    self.reject_note(p, ev)
            w["done"].append(name)
            self.save_state()
            self.check_stop()

    def entry_unlock_round(self, day: int, stage: str, assignments: dict[str, str], w: dict) -> dict[str, str]:
        """统一开锁行动轮：只处理门锁与次数，不移动玩家；结果供随后的统一进入结算使用。"""
        s = self.state
        unlockers: dict[str, str] = dict(w.get("unlockers", {}))
        done: list[str] = w.setdefault("unlock_done", [])
        for name in s["order"]:
            p = self.player(name)
            if not p or not p["alive"]:
                continue
            room = assignments.get(name, bedroom_of(name) if stage == "晚间休息" else HALL)
            key = f"{name}→{room}"
            if key in done or room in unlockers:
                continue
            if stage == "晚间休息" and room == bedroom_of(name):
                continue
            if not door_locked(s, room) or unlock_means(s, name) is None:
                continue
            sealed = s["sealed_room"]
            if sealed and room == sealed["room"] and name != sealed["owner"] and name not in sealed["allowed"]:
                continue
            menu = [
                f'- 愿意消耗一次【暴力破解】手段打开{room}（你在随后的进入轮优先）：'
                f'{{"type": "unlock", "room": "{room}"}}',
                "- 不愿意则 actions 为 []；若同批拟进入者中有人开门，你仍可在进入轮正常尝试进入",
                f'- {NO_SPEECH_NOTE}',
            ]
            extra = (f"在{stage}，你已声明前往{room}，它当前上锁。现在是进入前的独立开锁行动轮；"
                     "此决定只处理门锁，不代表你已经进入。若最终无人开门，所有需要开门的人都会进入失败；"
                     "容量、邀请、封闭空间与平民慎入等规则仍在随后的进入轮照常判定。")
            briefing = self.build_briefing(p, f"第{day}天 · 开锁行动轮", menu, extra)
            payload = self.call_ai(p, f"{stage}-开锁", briefing)
            accepted = next((act for act in payload["actions"] if isinstance(act, dict)
                             and act.get("type") == "unlock"
                             and str(act.get("room", room)) == room), None)
            if accepted is not None:
                ev = self.eng.unlock_for_entry(name, room, assignments)
                self.script_action(name, accepted, ev)
                if ev["ok"]:
                    unlockers[room] = name
                    self.route(ev)
                else:
                    self.reject_note(p, ev)
            done.append(key)
            w["unlockers"] = dict(unlockers)
            self.save_state()
            self.check_stop()
        return unlockers

    def select_rooms(self, day: int, period: str) -> None:
        w = self.wip(f"{day}:自由活动:选房", done=[], assignments={}, invites={},
                     unlock_done=[], unlockers={})
        assignments: dict[str, str] = dict(w["assignments"])
        pending_invite: dict[str, str] = dict(w["invites"])
        alive_names = [p["name"] for p in self.alive()]
        if not w["done"]:
            self.state["positions"] = {}
            self.script_sub("选房与去向声明（按行动顺序声明，随后统一开锁与进入）")
        self.blackout_declarations(day)
        rooms_menu = (
            [f'- 声明去向（必选一项）：{{"type": "choose_room", "room": "房间名"}}；合法去向只有四种：{"、".join(ROOM_CHOICES)}',
             '- 注意：白天卧室一律清场不开放（含你自己的）。声明收齐后先进行独立开锁行动轮，再统一进入；满员、上锁而无人开门、被封闭空间挡住或选择无效者一律滞留会客室',
             '- 邀请一名顺序在你之后的玩家同入（对方可改选）：{"type": "invite", "target": "玩家名"}',
             f'- {NO_SPEECH_NOTE}']
        )
        for i, name in enumerate(self.state["order"]):
            p = self.player(name)
            if not p or not p["alive"] or name in w["done"]:
                continue
            menu = list(rooms_menu)
            later = [n for n in self.state["order"][i + 1:] if n in alive_names]
            extra = f"你可以邀请顺序在你之后的玩家同入：{'、'.join(later) if later else '无'}。"
            declared = [f"{n}→{assignments[n]}" for n in self.state["order"] if n in assignments]
            if declared:
                extra += "\n截至目前去向声明（尚未开锁或进入）：" + "；".join(declared) + "。"
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
        unlockers = self.entry_unlock_round(day, "自由活动", assignments, w)
        ev = self.eng.enter_rooms(assignments, invited, unlockers=unlockers)
        if ev["ok"]:
            self.route(ev)
            self.script_private(ev)
            if self.state.get("blackout_news") == period:
                for name in assignments:
                    placed = self.state["positions"].get(name, HALL)
                    if placed == HALL:
                        self.tell(name, "你留在会客室——壁炉的火光还亮着，但你注意到走廊与其他房间的灯全部熄灭了："
                                        "停电了（除会客室外一片黑暗，所有门锁失效，监控停止记录；发电机被修复前停电不会结束）。")
                    else:
                        self.tell(name, f"你走向{placed}——灯骤然熄灭，四周一片漆黑，门上的锁全部失效："
                                        "停电了（除会客室外一片黑暗，所有门锁失效，监控停止记录；发电机被修复前停电不会结束）。")
                self.log_line("- 停电了——除会客室外所有房间一片黑暗，所有门锁失效（保险箱除外），监控停止记录；可前往地下室修复发电机恢复供电——未修复则停电一直持续（跨天延续）")
                self.state["blackout_news"] = None
            self.orch["free_invites"] = invited
            for t, v in invited.items():
                self.tell(v, f"{t} 接受了你的同入邀请，进入了{self.state['positions'].get(t, '?')}——即使房间黑暗，你也知道 TA 来了。")
            summary = ["选房结果：" + "、".join(f"{n}→{self.state['positions'].get(n, HALL)}" for n in assignments)]
            if invited:
                summary.append("同入邀请成立：" + "、".join(f"{t} 随 {v}（{assignments[t]}）" for t, v in invited.items()))
            for ln in summary:
                self.script_line("- " + ln if ln.startswith("选房结果") else "  " + ln)
            self._fire("on_summary", "选房结果", summary)
            for v in ev.get("device_triggers", []):
                self.script_line(f"  延时装置触发：{v}")
            self.save_state()
        else:
            print(f"  选房收尾失败：{ev.get('error')}")

    def all_rooms(self, stage: str = "自由活动") -> list[str]:
        rooms = list(ROOM_CHOICES)
        if stage == "晚间休息":
            rooms += [bedroom_of(p["name"]) for p in self.alive()]
        return rooms

    def room_talks(self, day: int, stage: str = "自由活动") -> None:
        """房间交谈（v3.22 自由活动/晚间休息共用）：亮房正常交谈；黑暗房间仅同入邀请成立的组合
        可摸黑低语——全体在场者（含潜伏者）都能听到。晚间卧室黑暗，守夜两人即受邀组合低语。"""
        positions = self.state["positions"]
        invites = self.orch.get("night_invites") if stage == "晚间休息" else self.orch.get("free_invites")
        free_invites = invites or {}
        w = self.wip(f"{day}:{stage}:交谈", done_rooms=[])
        for name in self.state["order"]:
            if self.state["winner"]:
                return  # 进入结算的延时装置击杀可能直接终局——终局后不再交谈
            room = positions.get(name)
            if not room or room in w["done_rooms"]:
                continue
            occ = [n for n in self.state["order"]
                   if positions.get(n) == room and (self.player(n) or {}).get("alive")]
            if len(occ) < 2:
                continue
            dark = is_dark(self.state, room)
            listeners: list[str] = []
            if dark:
                # v3.17：黑暗房间仅"同入邀请成立的组合"（互知在场）可摸黑低语（rules-6 §7.5.6/§9.5）；
                # 低语全体在场者都能听到——含组合看不见的潜伏者，低语者不会知道是否被旁听
                aware = set()
                for t, v in free_invites.items():
                    if t in occ and v in occ:
                        aware.update((t, v))
                speakers = [n for n in occ if n in aware]
                listeners = [n for n in occ if n not in aware]
                if len(speakers) < 2:
                    continue
            else:
                speakers = occ
            if any(c["day"] == day and c["stage"] == stage and c["room"] == room for c in self.state["cases"]):
                continue
            # 一个房间是一次原子 checkpoint：中途被打断则整间重谈（transcript 只在谈妥后才投递）
            transcript: list[str] = []
            for occ_name in speakers:
                p = self.player(occ_name)
                if dark:
                    partners = [n for n in speakers if n != occ_name
                                and (free_invites.get(occ_name) == n or free_invites.get(n) == occ_name)]
                    menu = ["- 本环节是黑暗中的低语：speech 会被房间内所有在场者听到（包括你们看不见的人）；actions 为 []"]
                    extra = (f"{room}一片漆黑，你看不见任何人——但你知道 {'、'.join(partners)} 应邀而来、就在身边，"
                             "你们可以摸黑低声交谈。注意：房间里可能还有你们不知道的潜伏者，你们说的话会被在场所有人听到，"
                             "而你们不会知道是否有人旁听。")
                    if transcript:
                        extra += "\n已发生的低语：\n" + "\n".join(transcript)
                else:
                    others = [n for n in occ if n != occ_name]
                    menu = ["- 本环节是房间内交谈：speech 只对同房者可见；actions 为 []"]
                    extra = f"你在 {room}，同房者：{'、'.join(others)}。"
                    if transcript:
                        extra += "\n本房间已发生的交谈：\n" + "\n".join(transcript)
                briefing = self.build_briefing(p, f"第{day}天 · {stage}（{room}内交谈）", menu, extra)
                payload = self.call_ai(p, f"{stage}-交谈-{room}", briefing)
                speech = str(payload.get("speech") or "").strip()
                if speech:
                    transcript.append(f"{occ_name}：{speech}")
            if transcript:
                self.orch["talks"].setdefault(f"{day}:{stage}:{room}", []).extend(transcript)  # 窃听器录音源（仅自由活动）
                tag = "内低语（黑暗）" if dark else "内交谈"
                if dark:
                    self.script_sub(f"{room}{tag}（{'、'.join(speakers)}）"
                                    + (f"【旁听：{'、'.join(listeners)}】" if listeners else ""))
                else:
                    self.script_sub(f"{room}{tag}（{'、'.join(occ)}）")
                for line in transcript:
                    self.script_line(f"    {line}")
                content = "；".join(transcript)
                for occ_name in speakers:
                    seat = self.seat(self.player(occ_name))
                    with open(self.dir / "private" / seat / "log.md", "a", encoding="utf-8") as f:
                        f.write(f"\n## 第{day}天 · {room}{tag}\n\n" + "\n".join(transcript) + "\n")
                    self.tell(occ_name, f"{room}{tag}：" + content)
                for occ_name in listeners:  # 潜伏者得知全部内容；低语者不知情（不泄漏潜伏）
                    seat = self.seat(self.player(occ_name))
                    with open(self.dir / "private" / seat / "log.md", "a", encoding="utf-8") as f:
                        f.write(f"\n## 第{day}天 · {room}（你在黑暗中听到有人低语）\n\n" + "\n".join(transcript) + "\n")
                    self.tell(occ_name, f"你在{room}的黑暗中，听到有人低声交谈：" + content)
            w["done_rooms"].append(room)
            self.save_state()
            self.check_stop()

    def room_actions(self, day: int, stage: str = "自由活动") -> None:
        """房间行动：按房间分组处理（全记录按房间分段，剪辑可读；各席行动在引擎层面相互独立，
        组间顺序不影响裁决），组内仍按行动顺序。wip 断点仍是席位粒度。
        v3.22：自由活动/晚间休息共用——谋杀/舞台魔术/毁证机会在本环节发动（菜单按位置生成）。
        winner 产生（谋杀/装置击杀终局）即整环节收束：终局后不再有合法行动，继续调用只会空耗
        token 并放大未公告信息的泄漏窗口（game/45 复盘）。"""
        w = self.wip(f"{day}:{stage}:房间行动", done=[])
        positions = self.state["positions"]
        for room in self.all_rooms(stage):
            if self.state["winner"]:
                return
            occ = [n for n in self.state["order"]
                   if positions.get(n) == room and (self.player(n) or {}).get("alive")]
            room_opened = room in w.setdefault("rooms_sectioned", [])
            for name in occ:
                if self.state["winner"]:
                    return
                if name in w["done"]:
                    continue
                p = self.player(name)
                if not p or not p["alive"]:
                    continue  # occ 在进房时快照：同房者先行动可能已谋杀/舞台魔术击杀 TA——死者不再有行动
                menu = self.action_menu(p, day)
                if not menu:
                    continue
                if not room_opened:
                    self.script_sub(f"{room} · 房间行动（{'、'.join(occ)}）")
                    w["rooms_sectioned"].append(room)  # 续跑不重复写子段头
                    room_opened = True
                extra = f"现在可使用房间设施或道具/技能。每轮只能选择一个动作，多报会整体驳回；无动作可做则 actions 为 []。\n{NO_SPEECH_NOTE}"
                briefing = self.build_briefing(p, f"第{day}天 · {stage}（房间行动）", menu, extra)
                payload = self.call_ai(p, f"{stage}-房间行动", briefing)
                self.apply_actions(p, payload["actions"], retry={
                    "label": f"{stage}-房间行动", "title": f"第{day}天 · {stage}（房间行动）",
                    "menu": menu, "extra": extra}, max_actions=1)
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
        if s["locks"].get(room) and not is_bedroom(room):
            # 卧室门锁项对房内人无意义（房主进入永远免费；客人开门只有放第三人进来的冷门用法），
            # 不列入——卧室内的夜间菜单因此可为空（跳过不调 AI）
            menu.append(f'- 打开当前房间的门锁：{{"type": "unlock", "room": "{room}"}}')
        if room == "地下室" and s["power_out"]:
            menu.append('- 修复发电机（无需道具或技能；结束停电、恢复供电，但地下室仍永久黑暗；修复前停电一直持续，可跨天）：{"type": "fix_generator"}')
        if s["phase"]["stage"] == "自由活动" and p["profession"] == "建筑师" \
                and skill_left(s, name, "封闭空间") > 0 and room != HALL:
            # 封闭空间仅自由活动时间可发动（引擎口径），夜间不列噪音项
            menu.append('- 封闭空间（锁死当前房间至下一自由活动；不能对会客室使用）：{"type": "seal_room", "allowed": ["许可留下的人"]}')
        if s["phase"]["stage"] == "晚间休息":
            if room == bedroom_of(name) and s["bug_room"] and held_item(p, "窃听器", day) is not None:
                menu.append('- 收听窃听器（获得今天自由活动时段该房间内、放置后发生的交谈记录）：{"type": "bug_listen"}')
            if room == HALL and s["table_items"]:
                others = [q["name"] for q in self.alive()
                          if q["name"] != name and s["positions"].get(q["name"]) == HALL]
                if not others:
                    items_desc = "、".join(f"「{e['name']}」（剩 {e['uses_left']} 次）" for e in s["table_items"])
                    menu.append(f'- 取走桌上一物品（当前桌上：{items_desc}；取用无人看见，明早会公告物品不见了）：{{"type": "take_table", "item": "物品名"}}')
        if room in FREE_ROOMS and is_villain(p) and p["profession"] == "机械师":
            # v3.29：任何房间行动阶段均可设置；本阶段不触发，后续进入结算才检查
            menu.append('- 设置延时装置（本阶段不触发，后续进入结算时检查）：{"type": "set_device", "room": "当前房间名"}')
        if room in FREE_ROOMS and s["bug_room"] is None and held_item(p, "窃听器", day) is not None:
            menu.append('- 在当前房间放置窃听器（只记录放置后发生的交谈）：{"type": "place_bug", "room": "当前房间名"}')
        if any(not c.get("announced") and c["room"] == room for c in s["cases"]):
            # v3.30 报案：进入结算已私下发现尸体者可选择报案（消耗本阶段房间行动；同房各自决定）
            menu.append('- 报案（你发现本房间有尚未公告的尸体）：成为报案人，随次日清晨公告公开'
                        '；同房的人各自决定是否报案，本动作消耗你本阶段唯一的房间行动名额：{"type": "report"}')
        # 当面赠送（v3.27）：限同房者、不限每日次数；谋杀/舞台魔术已拆至独立谋杀窗口，不在本环节
        mates = [q["name"] for q in self.alive()
                 if q["name"] != name and s["positions"].get(q["name"]) == room]
        giftable = [e["name"] for e in p.get("items", []) if e["uses_left"] > 0 and e["available_day"] <= day]
        if mates and giftable:
            menu.append(f'- 赠送道具给同房者（限同房者；明亮房间同房者看得见（道具名不公开），黑暗中无人知晓）：{{"type": "gift", "to": "{"|".join(mates)}", "item": "道具名（你持有：{"、".join(giftable)}）"}}')
        return menu

    def run_night(self) -> None:
        """晚间休息（v3.22 与自由活动同构）：去向收集（互不可见）→ 进入结算 → 密道 → 手电筒判定 → 房间交谈 → 房间行动。"""
        day = self.state["phase"]["day"]
        period = current_period(self.state)
        if self.state["night_assigned"] != period:
            self.night_select(day, period)
        if self.state["night_assigned"] != period:
            return
        self.night_passages(day)
        self.flashlight_phase(day, "晚间休息")
        self.room_talks(day, "晚间休息")
        self.room_actions(day, "晚间休息")
        self.murder_window(day, "晚间休息")

    def night_select(self, day: int, period: str) -> None:
        """秘密收齐去向，随后统一执行开锁行动轮与进入轮。"""
        w = self.wip(f"{day}:晚间休息:归宿", done=[], assignments={}, invites={}, passages={},
                     settled=False, passage_done=[], unlock_done=[], unlockers={})
        assignments: dict[str, str] = dict(w["assignments"])
        pending_invite: dict[str, str] = dict(w["invites"])
        passages: dict[str, dict] = {k: dict(v) for k, v in w.get("passages", {}).items()}
        if w["settled"]:
            return
        alive_names = [p["name"] for p in self.alive()]
        self.script_sub("晚间去向（按行动顺序秘密选择，进入结果互不可见）")
        for i, name in enumerate(self.state["order"]):
            p = self.player(name)
            if not p or not p["alive"] or name in w["done"]:
                continue
            later = [n for n in self.state["order"][i + 1:] if n in alive_names]
            menu = [
                '- 回自己卧室过夜（默认；actions 为 [] 即回房）',
                '- 选择今晚去向：{"type": "choose_room", "room": "会客室|书房|监控室|地下室|某人的卧室"}',
                '- 邀请一名顺序在你之后的玩家同去你的目的地（对方可改选；成立则互知在场，黑暗中也知道）：{"type": "invite", "target": "玩家名"}',
            ]
            extra = ("夜间去向互相不可见。去向收齐后先进行独立开锁行动轮（开锁者在进入轮优先），再统一进入。"
                     "锁规则：他人卧室上锁（需【暴力破解】手段或受邀，对所有人一样）；"
                     "会客室夜间上锁（手段或停电/第 4 天起免费；人数不限、始终明亮，夜宿名单清晨公开）；"
                     "监控室上锁（首开需手段，开后永久开启）；书房/地下室无锁。\n"
                     "卧室夜间黑暗——你看不见房内有谁；地下室永久黑暗；会客室永不黑暗。\n"
                     "平民不敢非受邀进入已有他人的书房/监控室/地下室（被邀请者与反派不受此限）。\n"
                     "人数上限：书房 3 / 监控室 2 / 地下室 3 / 卧室 2 / 会客室不限；满员或无法进入者默认回自己卧室。\n"
                     + NO_SPEECH_NOTE)
            if self.lite:
                extra += ("\n你必须现在直接声明今晚的唯一去向，不能先回房待命、看到他人归宿后再移动；"
                          "想去某人的卧室就现在直接选择该卧室。若对方不在那里，你会独自进入，不会发生袭击。")
            if later:
                extra += f"\n顺序在你之后的玩家：{'、'.join(later)}。"
            if name in pending_invite:
                v = pending_invite[name]
                dest = assignments.get(v) or f"{bedroom_of(v)}（TA 的卧室，默认）"
                extra += f"\n{v} 邀请你今晚与 TA 同去——接受就把 choose_room 选为与 TA 相同的目的地（TA 的目的地：{dest}）。"
            if is_villain(p) and p["profession"] == "建筑师" and self.state["passage"] and not self.state["passage"]["used"]:
                menu.append('- 使用密道（当夜行动，与 choose_room 互斥——从卧室出发；监控无痕；出口人数须满足谋杀人数条件，否则当晚失败但密道保留）：{"type": "use_passage", "room": "会客室|书房|监控室|地下室", "do": "open_safe|view_monitoring|fix_generator|unlock（可选）"}')
            # v3.22：白天死亡不再即时公告——毁证提示只发给案件凶手本人（其他反派无从得知案发）
            coverable = [c for c in self._coverable_cases(day) if c["culprit"] == name]
            if coverable and is_villain(p) and self._has_coverup_means(p):
                rooms = "、".join(dict.fromkeys(c["room"] for c in coverable))
                extra += (f"\n提示：你制造的案件（{rooms}）现场仍可处理——毁证/移尸仅限案发当天，"
                          "明晨案件调查后现场即无法改动。今晚前往案发房间，房间行动阶段即可获得毁证机会。")
            briefing = self.build_briefing(p, f"第{day}天 · 晚间休息", menu, extra)
            payload = self.call_ai(p, "晚间休息", briefing)
            for act in payload["actions"]:
                at = act.get("type")
                if at == "invite":
                    tgt = str(act.get("target", ""))
                    if tgt in later:
                        pending_invite[tgt] = name
                elif at == "choose_room":
                    room = str(act.get("room", ""))
                    owner = bedroom_owner(room) if is_bedroom(room) else None
                    if room in FREE_ROOMS or (owner and owner != name and owner in alive_names):
                        assignments[name] = room
                        passages.pop(name, None)
                elif at == "stay":
                    assignments.pop(name, None)
                    passages.pop(name, None)
                elif at == "use_passage":
                    room = str(act.get("room", ""))
                    if room in FREE_ROOMS and name not in assignments:
                        passages[name] = {"room": room, "do": str(act.get("do", ""))}
            w["done"].append(name)
            w["assignments"] = dict(assignments)
            w["invites"] = dict(pending_invite)
            w["passages"] = {k: dict(v) for k, v in passages.items()}
            self.save_state()
            self.check_stop()
        unlockers = self.entry_unlock_round(day, "晚间休息", assignments, w)
        ev = self.eng.night_assign(assignments, dict(pending_invite), unlockers=unlockers)
        if not ev["ok"]:
            print(f"  晚间归宿结算失败：{ev.get('error')}")
            return
        self.route(ev)
        summary = ["晚间归宿：" + "、".join(f"{n}→{r}" for n, r in ev["positions"].items())]
        self.script_line("- " + summary[0])
        invited = {t: v for t, v in pending_invite.items()
                   if self.state["positions"].get(t) is not None
                   and self.state["positions"].get(t) == self.state["positions"].get(v)}
        self.orch["night_invites"] = invited  # 黑暗房间受邀组合低语（room_talks 用）
        if invited:
            summary.append("邀请成立：" + "、".join(
                f"{t} 随 {v}（{self.state['positions'][t]}）" for t, v in invited.items()))
            self.script_line("  " + summary[-1])
        self._fire("on_summary", "晚间归宿", summary)  # console：结算总结独立分隔段
        self.script_private(ev)
        for v in ev.get("device_triggers", []):
            self.script_line(f"  延时装置触发：{v}")
        w["settled"] = True
        self.save_state()

    def night_passages(self, day: int) -> None:
        """密道执行（归宿结算后）：成功则隐身转移到出口房间，随后参与该房间的交谈与行动。"""
        w = self.wip(f"{day}:晚间休息:归宿", done=[], assignments={}, invites={}, passages={},
                     settled=False, passage_done=[])
        passages: dict[str, dict] = {k: dict(v) for k, v in w.get("passages", {}).items()}
        for name, ps in passages.items():
            if name in w["passage_done"]:
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

    def run_investigation(self) -> None:
        """案件调查：调查自荐（现场调查/鲁米诺试剂/犯罪侧写）+ 死者字条遗嘱。

        清晨公告（死亡/未遂/强制开锁/职业公开/监控宣读）由 advance() 统一路由，此处只管调查动作。
        无案件的清晨不调用任何 AI（未遂不产生案件：无尸体无现场，无可调查对象），直接跳过。
        """
        day = self.state["phase"]["day"]
        yday = day - 1
        if not self.state["cases"]:
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
        case_lines = [f"{c['victim']}（第{c['day']}天，{c['room']}）" for c in self.state["cases"]]
        for name in self.state["order"]:
            if self.state["winner"]:
                return
            p = self.player(name)
            if not p or not p["alive"] or name in w["done"]:
                continue
            menu = []
            if skill_left(self.state, name, "现场调查") > 0 or held_item(p, "鲁米诺试剂", day) is not None:
                menu.append('- 现场调查（每起案件可选一项获知真实结论，可申报多次、次数以剩余为准）：{"type": "investigate", "case": "受害者名字", "choice": "武器伤痕|死亡时间|尸体转移"}')
            if skill_left(self.state, name, "犯罪侧写") > 0:
                menu.append('- 犯罪侧写（获知凶手本人的一项特征）：{"type": "profile", "case": "受害者名字", "aspect": "性别|国籍|体格"}')
            if not menu:
                continue
            extra = "已发生的案件（用受害者名字指认）：\n" + "\n".join(case_lines)
            extra += "\n调查结果仅你本人可见——是否公布、隐瞒或撒谎，由你在发言中决定。"
            extra += f"\n{NO_SPEECH_NOTE}"
            briefing = self.build_briefing(p, f"第{day}天 · 案件调查", menu, extra)
            payload = self.call_ai(p, "案件调查", briefing)
            self.apply_actions(p, payload["actions"], retry={
                "label": "案件调查", "title": f"第{day}天 · 案件调查", "menu": menu, "extra": extra})
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
                    f"现在投票。得票超过存活人数（{len(alive_names)} 人）一半者出局{det_note}；平票或无人过半则无人出局。"
                    f"\n{NO_SPEECH_NOTE}")
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
            ev = self.eng.resolve_vote(votes, implant, defer_out=True)
            if not ev["ok"] and implant is not None:
                ev = self.eng.resolve_vote(votes, None, defer_out=True)  # 植入声明非法（如目标已死）：作废植入，不影响投票本身
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
        # v3.21：出局执行前窗口——投票将导致最后一名平民出局时，先公布票型，
        # 该平民可行使武装指认（背水一战）；指认结算（宣言/遗言/公布/遗嘱）由 run_armed_accusation 接管
        if out and self.state.get("pending_out") and not w.get("pre_exec_done"):
            civilians = [q["name"] for q in self.alive() if q["identity"] == "平民"]
            if len(civilians) == 1 and civilians[0] == out:
                detail = "、".join(f"{t} {_fmt_votes(c)}票" for t, c in sorted(tally.items(), key=lambda kv: -kv[1]))
                voters = w.get("voters") or len(self.alive())
                self.log_line(f"投票结果：{detail}——{out} 过半数（存活 {voters} 人，需 >{voters / 2:g} 票）"
                              f"——出局结算前，{out} 仍可行使武装指认")
                self.run_armed_accusation()
                if not self.state.get("pending_out"):
                    w["pre_exec_done"] = True
                    w["routed"] = True
                    self.save_state()
                    return
            if self.state.get("pending_out"):
                exec_ev = self.eng.execute_pending_out()
                public["identity"] = exec_ev["public"].get("identity")
                public["profession"] = exec_ev["public"].get("profession")
                public.setdefault("announcements", [])
                public["announcements"] += exec_ev["public"]["announcements"]
                ev["private"] = {**ev.get("private", {}), **exec_ev.get("private", {})}
                w["ev"] = ev
            w["pre_exec_done"] = True
            self.save_state()
        if out and not w["last_words"]:
            op = self.player(out)
            owned = [e["name"] for e in self.state["sealed_items"] if e.get("from") == out]
            has_badge = out == self.state.get("detective")
            menu = ["- 本环节无动作可做，actions 返回 []"]
            extra = "你被投票出局了。发表你的遗言（可以撒谎）——遗言之后主持人将公布你的身份。"
            if owned or has_badge:
                menu = ['- 遗嘱：逐件指定遗留归属（未指定/被拒收的放回会客室桌上；遗嘱公开）：'
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
            threshold = f"{voters / 2:g}"  # 过半 = 严格大于一半；侦探 1.5 票下阈值可能带 .5（game/5 审计：// 下取整会误导）
            if out:
                verdict = f"{out} 过半数出局（存活 {voters} 人，需 >{threshold} 票）"
            else:
                verdict = f"无人过半数（存活 {voters} 人，需 >{threshold} 票），本轮无人出局"
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
        menu = ['- 遗嘱：逐件指定遗留归属（未指定/被拒收的放回会客室桌上；遗嘱公开）：'
                '{"type": "bequeath", "mapping": {"道具名": "存活玩家名"}}']
        extra += f"\n你留下的未使用道具：{'、'.join(owned) if owned else '无'}。"
        if has_badge:
            extra += "你是侦探——侦探徽章也在遗嘱之列（mapping 里以「警徽」为键指定一名存活玩家继任；不指定则销毁，本局不再设侦探）。"
        payload = self.call_ai(p, title, self.build_briefing(p, title, menu, extra))
        speech = str(payload.get("speech") or "").strip()
        if speech:
            # 遗嘱/死者字条附言公开（v3.8）——"字条"在设定上就是死者留下的公开文件
            label = "死者字条" if "死者字条" in title else "遗嘱"
            self.log_line(f"**{p['name']} 的{label}**：{speech}")
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
            menu = ['- 逐件回应（接受则当天即可用；拒绝则放回会客室桌上——夜间独处会客室的人可取走）：'
                    '{"type": "bequest_reply", "decisions": {"道具名": "接受|拒绝"}}']
            extra = (f"{owner} 的遗嘱把以下留给你：{'、'.join(items + (['侦探徽章'] if gets_badge else []))}。"
                     "此事全员公开——收下可能带来信任也可能带来嫌疑，由你判断。"
                     f"\n{NO_SPEECH_NOTE}")
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
        """武装指认（rules-6 §7.3，v3.22）：指对→杀手出局（帮凶继任，游戏继续），指错→指认者出局。
        触发：① 本轮投票无人出局；② 最后一名平民出局执行前（pending_out 为本人，背水一战）。
        出局者的遗言（先于身份公布）与遗嘱流程与投票出局一致。每次投票至多一次（引擎级守卫）。
        v3.22：指认消耗武器（等同攻击）——自己的武器销毁；临时取用的桌上凶器消耗掉、不再放回。"""
        day = self.state["phase"]["day"]
        w = self.wip(f"{day}:投票:武装指认", done=[], fired=False)
        if w["fired"]:
            return
        det = self.state.get("detective")
        pending = self.state.get("pending_out")
        table_armed = any(e["name"] in ARMED_WEAPONS for e in self.state.get("table_items", []))
        for name in self.state["order"]:
            if self.state["winner"]:
                return
            if name in w["done"]:
                continue
            p = self.player(name)
            if not p or not p["alive"] or p["identity"] != "平民":
                continue
            if pending is not None and name != pending:
                continue  # 触发②：仅即将出局的最后一名平民
            armed = bool(held_item_names(p, day) & set(ARMED_WEAPONS))
            if det is not None:
                if name != det or not armed:
                    continue  # 有侦探：仅侦探本人且须持有武器（侦探为反派时无人可指认）
            elif not armed and not table_armed:
                continue  # 无侦探：持武器平民，或桌上凶器仍在时的任意平民（临时取用）
            others = [q["name"] for q in self.alive() if q["name"] != name]
            payoff = "指对 → 杀手出局（帮凶继任，游戏继续）；指错 → 你出局；无论对错武器都消耗"
            if armed:
                menu = [f'- 武装指认某人为杀手（{payoff}）：{{"type": "armed_accuse", "target": "{"|".join(others)}"}}（可加 "weapon": "刀" 指定用哪件，缺省用第一件）',
                        "- 不指认则 actions 为 []"]
            else:
                menu = [f'- 临时拿起会客室桌上的凶器，武装指认某人为杀手（{payoff}——凶器用掉即消耗，不再放回桌上）：{{"type": "armed_accuse", "target": "{"|".join(others)}"}}',
                        "- 不指认则 actions 为 []"]
            if pending is not None:
                hint = ("投票结果将使你出局——这是你最后的机会：武装指认，背水一战。"
                        "指对，杀手伏法、你获救（游戏继续）；指错，你按原判决出局。也可以选择不指认。")
            elif name == det:
                hint = "本轮投票无人出局。你是侦探——本局仅你可行使武装指认；你持有武器，可以选择指认——机会只有一次，武器用掉即消耗，慎重。"
            elif armed:
                hint = "本轮投票无人出局。你持有武器，可以选择武装指认——机会只有一次，武器用掉即消耗，慎重。"
            else:
                hint = "本轮投票无人出局。桌上凶器仍在会客室桌上，你可以临时取用进行武装指认——机会只有一次，凶器用掉即消耗，慎重。"
            briefing = self.build_briefing(p, f"第{day}天 · 武装指认", menu, hint)
            payload = self.call_ai(p, "武装指认", briefing)
            fired = False
            for act in payload["actions"]:
                if act.get("type") == "armed_accuse" and not fired:
                    ev = self.eng.armed_accusation(name, str(act.get("target", "")), act.get("weapon"))
                    if ev["ok"]:
                        speech = str(payload.get("speech") or "").strip()
                        if speech:
                            self.log_line(f"**{name}**（武装指认宣言）：{speech}")  # 指认宣言公开（v3.8）
                        fired = True
                        out_name = ev["public"].get("out")
                        if out_name:
                            self._accuse_aftermath(out_name, ev, day)
                            self.orch["out_day"][out_name] = day
                    else:
                        self.reject_note(p, ev)
            w["done"].append(name)
            if fired:
                w["fired"] = True
                self.save_state()
                return  # 一次投票后至多一次指认
            self.save_state()
            self.check_stop()

    def _accuse_aftermath(self, out: str, ev: dict, day: int) -> None:
        """武装指认出局者的后事（v3.21，与投票出局同口径）：遗言（先于身份公布）→ 路由公布 → 遗嘱。"""
        op = self.player(out)
        owned = [e["name"] for e in self.state["sealed_items"] if e.get("from") == out]
        has_badge = out == self.state.get("detective")
        menu = ["- 本环节无动作可做，actions 返回 []"]
        extra = "你被武装指认出局了。发表你的遗言（可以撒谎）——遗言之后主持人将公布你的身份。"
        if owned or has_badge:
            menu = ['- 遗嘱：逐件指定遗留归属（未指定/被拒收的放回会客室桌上；遗嘱公开）：'
                    '{"type": "bequeath", "mapping": {"道具名": "存活玩家名"}}']
        if owned:
            extra += f"\n你留下的未使用道具：{'、'.join(owned)}。在遗言中一并指定归属。"
        if has_badge:
            extra += "\n你是侦探——侦探徽章也在遗嘱之列（mapping 里以「警徽」为键指定一名存活玩家继任；不指定则销毁，本局不再设侦探）。"
        payload = self.call_ai(op, "遗言", self.build_briefing(op, f"第{day}天 · 遗言", menu, extra))
        last_words = str(payload.get("speech") or "").strip()
        if last_words:
            self.log_line(f"**{out} 的遗言**：{last_words}")
        mapping = {}
        for act in payload["actions"]:
            if act.get("type") == "bequeath" and isinstance(act.get("mapping"), dict):
                mapping = {str(k): (None if v in (None, "销毁") else str(v)) for k, v in act["mapping"].items()}
        self.route(ev)  # 出局公告与身份公布、帮凶继任私密通知（遗言之后）
        if owned or has_badge:
            self.run_bequest(out, mapping)

    def _coverable_cases(self, day: int) -> list[dict]:
        """仍可毁证的案件（v3.19 起仅限案发当天；v3.18 起毁证者须身处案发现场）。"""
        return [c for c in self.state["cases"]
                if c["day"] == day and c["evidence_vulnerable"]
                and (c["falsified"] is None or not c["body_moved"])]

    def _has_coverup_means(self, p: dict) -> bool:
        s, day = self.state, self.state["phase"]["day"]
        return (any(skill_left(s, p["name"], sk) > 0 for sk in COVERUP_SKILLS)
                or held_item(p, "可疑的粉末", day) is not None
                or held_item(p, "裹尸袋", day) is not None)

    def run_coverup(self) -> None:
        """毁证窗口（自由活动/晚间休息结束前）：存在可毁证案件且反派有手段时，按 杀手→帮凶 顺序提供机会。
        v3.18：毁证须身处案发现场（rules-6 §7.6）——只通知当前位置在案件房间的反派，
        场外反派既不可远程毁证，也不再被即时告知案发信息。v3.19：仅限案发当天。"""
        s = self.state
        day = s["phase"]["day"]
        vulnerable = self._coverable_cases(day)
        if not vulnerable:
            return
        villains = sorted((p for p in self.alive() if is_villain(p) and self._has_coverup_means(p)),
                          key=lambda p: 0 if p["identity"] == "杀手" else 1)
        if not villains:
            return
        w = self.wip(f"{day}:{s['phase']['stage']}:毁证", done=[])
        for p in villains:
            if self.state["winner"]:
                return
            if p["name"] in w["done"]:
                continue
            here = [c for c in vulnerable if s["positions"].get(p["name"]) == c["room"]]
            if not here:
                w["done"].append(p["name"])  # 不在案发现场：本窗口与 TA 无关
                continue
            case_lines = [f"{c['victim']}（{c['room']}）" for c in here]
            menu = []
            if skill_left(s, p["name"], "伪造痕迹") > 0:
                menu.append('- 伪造痕迹（该案件除武器伤痕外的调查结果全部出错）：{"type": "falsify", "case": "受害者名字", "kind": "伪造痕迹"}')
            if skill_left(s, p["name"], "销毁证物") > 0:
                menu.append('- 销毁证物（该案件仅死亡时间保持正确）：{"type": "falsify", "case": "受害者名字", "kind": "销毁证物"}')
            if skill_left(s, p["name"], "伪造证物") > 0 or held_item(p, "可疑的粉末", day) is not None:
                menu.append('- 伪造证物（该案件除尸体转移外的调查结果全部出错）：{"type": "falsify", "case": "受害者名字", "kind": "伪造证物"}')
            if skill_left(s, p["name"], "转移尸体") > 0 or held_item(p, "裹尸袋", day) is not None:
                menu.append('- 转移尸体到另一房间：{"type": "move_body", "case": "受害者名字", "to_room": "会客室|书房|监控室|地下室|某人的卧室"}')
            extra = ("以下案件的现场没有其他非反派角色，且你就在案发现场——你可以动手毁证（也可以不动）：\n"
                     + "\n".join(case_lines) + f"\n{NO_SPEECH_NOTE}")
            briefing = self.build_briefing(p, f"第{day}天 · 毁证机会", menu, extra)
            payload = self.call_ai(p, "毁证机会", briefing)
            self.apply_actions(p, payload["actions"], retry={
                "label": "毁证机会", "title": f"第{day}天 · 毁证机会", "menu": menu, "extra": extra})
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
                        self.advance(f"第{day}天 · {'投票' if day >= 2 else '自由活动'}")
                elif stage == "投票":
                    self.run_vote()
                    if not self.state["winner"]:
                        # v3.15：第 6 天投票后由引擎直接判主神（终审日无自由活动）
                        self.advance(f"第{day}天 · {'终局宣判' if day >= 6 else '自由活动'}")
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
