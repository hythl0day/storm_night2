"""跑局评分卡（M6 修订版）：扫描对局存档目录，产出单局报告与多局报告。

背景：一局真实对局耗时一小时以上，批量自动跑局的长连接 API 不稳定——故不设批量跑局器；
手动跑局（换 seed、断点续跑），存档就在 game/ 下，本脚本默认直接扫描；跑完整的合格局可挑入
selected/ 作为精选池（--dir selected）。

用法：
    python -m src.scorecard                      # 多局报告（默认扫描 game/）
    python -m src.scorecard --dir selected       # 改扫别的目录（如精选池）
    python -m src.scorecard --game game/42       # 单局详细报告（完整目录路径；仅目录名时按 --dir 下的子目录解析）
    python -m src.scorecard --min-version v4.0   # 多局报告只统计 v4.0 及以后的存档

单局报告：基本台账 + 流程简报（script.md 化简，含密谋与行动回执）+ 未遂明细 + 戏剧性评分（分项透明）+ 主角弧线候选 + 成本（含缓存命中率）。
多局报告：版本分组胜率平衡 + 机制利用率 + 反派主动性 + 多样性分布 + 成本汇总。
注意：平衡数据完全按（版本 × 规则集）分组、不做任何合并——快速迭代期每次更新几乎都改规则，合并即混池。
winner 为空的中断局不计入胜负率。失忆版精简规则集（state 的 is_lite_rule）永不与高级规则同池：
lite 按版本单列（lite·vX.Y），与同名版本的全规则组分池；旧档缺该字段一律视为高级规则（None → False）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from . import ROOT, resolve_game_dir

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# script.md 中"- X 声明 {...} → 成功/驳回"行：机制利用率的数据源（含被驳回的尝试，尝试也算"规则被想到"）
ACTION_RE = re.compile(r"- \S+ 声明 (\{.*?\}) → (成功|驳回)")

ACTION_LABELS = {
    "see_through": "看破伪装/测谎仪", "investigate": "现场调查/鲁米诺/粉末",
    "tamper_investigation": "篡改案件调查",
    "open_safe": "开保险箱", "unlock": "开锁", "declare_blackout": "拉电闸停电", "fix_generator": "修发电机",
    "view_monitoring": "查监控", "remote_view_monitoring": "远程查看监控",
    "remote_tamper_monitoring": "远程抹除监控",
    "seal_room": "封闭空间", "place_bug": "窃听器放置",
    "bug_listen": "窃听器收听", "use_flashlight": "手电筒", "take_table": "取桌上物品",
    "set_device": "延时装置预设", "device_fire": "延时装置启动",
    "stage_magic": "舞台魔术", "strong_attack": "强袭", "slow_poison": "缓释毒剂",
    "pharmacology_kill": "药理学毒杀", "passage_kill": "密道谋杀",
    "murder": "谋杀行动", "tamper_vote": "篡改投票",
    "transfer": "赠送道具",
}


def load_game(d: Path) -> dict | None:
    """读取一个对局存档目录（缺文件降级；无 state.json 视为非存档）。"""
    sj = d / "state.json"
    if not sj.exists():
        return None
    st = json.loads(sj.read_text(encoding="utf-8"))

    def text(p: Path) -> str:
        return p.read_text(encoding="utf-8") if p.exists() else ""

    host = []
    for line in text(d / "host_log.jsonl").splitlines():
        if line.strip():
            try:
                host.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return {
        "dir": d, "state": st,
        "feed": st.get("orch", {}).get("feed", []),
        "script": text(d / "script.md"), "log": text(d / "public" / "log.md"),
        "host": host,
    }


def rules_version(st: dict) -> str:
    """规则版本：v3.5 起 deal 直接写入；旧档按内容推断（table_items 为 v3.5 新增字段）。"""
    return st.get("rules_version") or ("v3.5" if "table_items" in st else "≤v3.4")


def is_lite(st: dict) -> bool:
    """失忆版精简规则集标记：deal 自 v3.12 起写入 is_lite_rule；旧档缺字段/None 一律视为高级规则。"""
    return bool(st.get("is_lite_rule"))


def _ver_tuple(v: str) -> tuple[int, int] | None:
    """解析 "v3.8" 为 (3, 8)；无法解析（如 "≤v3.4"）返回 None（--min-version 过滤时被排除）。"""
    m = re.fullmatch(r"v(\d+)\.(\d+)", v)
    return (int(m.group(1)), int(m.group(2))) if m else None


def roster_identities(script: str) -> dict[str, str]:
    """从 script.md 的阵容表解析初始身份（state 里的身份会被继任改写，不可用于开局口径）。"""
    ids = {}
    for line in script.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 8 and cells[5] in ("平民", "帮凶", "杀手"):
            ids[cells[1]] = cells[5]
    return ids


def rules_group(st: dict) -> str:
    """平衡统计分组：完全按（版本 × 规则集）分组，不做任何合并——快速迭代期每次更新几乎都改规则，
    合并必然混池。lite 规则面不同，永不与高级规则同池：同版本的两池以 lite· 前缀区分。"""
    v = rules_version(st)
    return f"lite·{v}" if is_lite(st) else v


def roster_professions(script: str) -> dict[str, str]:
    """从 script.md 的阵容表解析职业（与 roster_identities 同表，身份可能被继任改写、职业不会）。"""
    profs = {}
    for line in script.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 8 and cells[5] in ("平民", "帮凶", "杀手"):
            profs[cells[1]] = cells[6]
    return profs


# script.md 行动回执（"◆ X｜动作描述 → 成功/驳回"）：机制利用率的补充数据源
RECEIPT_RE = re.compile(r"◆ \S+｜(.+?) → (成功|驳回)")
RECEIPT_KEYS = [
    ("声明今晚停电", "拉电闸停电"), ("修复发电机", "修发电机"), ("查看监控", "查监控"),
    ("远程抹除监控", "远程抹除监控"), ("篡改案件调查", "篡改案件调查"),
    ("现场调查", "现场调查"), ("鲁米诺", "鲁米诺试剂"), ("看破伪装", "看破伪装/测谎"),
    ("打开保险箱", "开保险箱"), ("延时装置", "延时装置"), ("舞台魔术", "舞台魔术"), ("篡改投票", "篡改投票"),
    ("强袭", "强袭"), ("缓释毒剂", "缓释毒剂"), ("药理学", "药理学毒杀"), ("密道谋杀", "密道谋杀"),
    ("封闭空间", "封闭空间"), ("窃听器", "窃听器"),
    ("手电筒", "手电筒"), ("取桌上物品", "取桌上物品"), ("谋杀", "谋杀行动"),
    ("赠送", "赠送道具"),
]


def receipt_label(text: str) -> str:
    for kw, label in RECEIPT_KEYS:
        if kw in text:
            return label
    return text.split("（")[0].split("：")[0][:12]


def win_path(st: dict, feed: list[str] | None = None) -> str:
    w, r = st.get("winner"), st.get("win_reason", "") or ""
    if w == "主神":
        return "主神局"
    if w == "反派":
        return "反派杀光"
    if "无杀倒计时" in r:
        return "无杀倒计时"
    if "武装指认" in r:
        return "武装指认"
    # 杀手全灭的平民胜：引擎 win_reason 统一写"杀手均已出局"，最后一击是投票还是指认只能看
    # 事件流——从最后一条杀手出局公告向前，遇到的首个决定性事件（指认/出局/投票结果）定性
    feed = feed or []
    last_out = max((i for i, l in enumerate(feed) if "出局。身份：杀手" in l), default=None)
    if last_out is not None:
        prev = next((l for l in reversed(feed[:last_out])
                     if "武装指认" in l or "出局。身份：" in l or "投票结果" in l), "")
        if "武装指认" in prev and "指认正确" in prev:
            return "武装指认"
    return "投票淘汰"


def analyze(g: dict) -> dict:
    """单局数据 → 指标包。"""
    st, feed, script = g["state"], g["feed"], g["script"]
    cases = st.get("cases", [])
    attempts = [a for a in st.get("attempts", []) if a.get("target")]
    ids = roster_identities(script)

    detectives: list[str] = []
    for line in feed:
        m = re.search(r"侦探竞选：([^，。 ]+).*?当选侦探", line)
        if m:
            detectives.append(m.group(1))
        m = re.search(r"侦探徽章（[^）]*）→ (\S+)", line)
        if m and m.group(1) not in detectives:
            detectives.append(m.group(1))
    accusations = [l for l in feed if "武装指认" in l and "为杀手" in l]
    blackout_days = st.get("blackout_days", [])
    blackout_periods = st.get("blackout_periods")
    if blackout_periods is None:
        blackout_kill_count = sum(1 for c in cases if c["day"] in blackout_days)
    else:
        blackout_kill_count = sum(1 for c in cases if c.get("period") in blackout_periods)
    actions: Counter = Counter()
    action_rejects: Counter = Counter()
    for m in ACTION_RE.finditer(script):
        try:
            at = json.loads(m.group(1)).get("type", "?")
        except json.JSONDecodeError:
            continue
        label = ACTION_LABELS.get(at, at)
        actions[label] += 1
        if m.group(2) == "驳回":
            action_rejects[label] += 1
    for m in RECEIPT_RE.finditer(script):
        label = receipt_label(m.group(1))
        actions[label] += 1
        if m.group(2) == "驳回":
            action_rejects[label] += 1

    gun_moves = (sum(1 for l in feed if "侦探佩枪" in l)
                 + sum(1 for l in feed if "会客室桌上的" in l and "不见了" in l)
                 + sum(1 for l in feed if "放回会客室桌上" in l)
                 + sum(1 for c in cases if "左轮手枪" in c.get("looted", [])))
    final_ids = {p["name"]: p["identity"] for p in st.get("players", [])}
    succession = any(ids.get(n) == "帮凶" and final_ids.get(n) == "杀手" for n in ids)
    if not succession:  # 缺阵容表时退化到私密历史文本（继任私聊会留在 orch.hist）
        hist = st.get("orch", {}).get("hist", {})
        succession = any("继任为杀手" in m for msgs in hist.values() for m in msgs)
    host_ok = [h for h in g["host"] if h.get("ok")]
    # 反派主动性：旧版窗口从第 2 天起；v3.28 起首日自由活动/晚间也有两个独立窗口
    days = st.get("phase", {}).get("day", 1)
    version = _ver_tuple(rules_version(st))
    first_day_windows = 2 if version and version >= (3, 28) else 0
    windows = max(1, 2 * (days - 2) + first_day_windows)
    behavior = len(cases) + len(attempts)
    retry_after_fail = any(
        any(c["day"] > a2["day"] for c in cases) or any(b["day"] > a2["day"] for b in attempts)
        for a2 in attempts)
    # 反派功能技能使用率（v4.0：八种反派职业技能全部计入）：阵容表出场 vs state 台账消耗
    profs = roster_professions(script)
    valve = {"魔术师": "舞台魔术", "机械师": "延时装置", "心理医生": "篡改投票", "黑客": "篡改投票",
             "医师": "药理学", "化学家": "缓释毒剂", "运动员": "强袭", "建筑师": "建造密道"}
    su = st.get("skill_uses", {})
    villain_valve = sum(1 for n, i in ids.items() if i in ("帮凶", "杀手") and profs.get(n) in valve)
    valve_used = sum(1 for n, i in ids.items()
                     if i in ("帮凶", "杀手") and profs.get(n) in valve
                     and su.get(n, {}).get(valve[profs[n]], 0) < 1)
    prompt_tokens = sum((h.get("usage") or {}).get("prompt_tokens") or 0 for h in g["host"])
    cached_tokens = sum(((h.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                        for h in g["host"])
    a = {
        "seed": st.get("seed", g["dir"].name),
        "dir_name": g["dir"].name,
        "version": rules_version(st),
        "lite": is_lite(st),
        "group": rules_group(st),
        "complete": st.get("winner") is not None,
        "winner": st.get("winner"), "win_reason": st.get("win_reason"),
        "path": win_path(st, feed) if st.get("winner") else None,
        "days": days,
        "players": st.get("players", []),
        "alive": [p["name"] for p in st.get("players", []) if p.get("alive")],
        "out_day": st.get("orch", {}).get("out_day", {}),
        "ids": ids,
        "murders": len(cases), "attempts": len(attempts), "cases": cases,
        "attempt_list": attempts,
        "aggression": behavior / windows,
        "retry_after_fail": retry_after_fail,
        "empty_raids": script.count("空房白跑"),
        "villain_valve": villain_valve,
        "valve_used": valve_used,
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "elected": bool(detectives), "detectives": detectives,
        "detective_murdered": any(c["victim"] in detectives for c in cases),
        "badge_passed": sum(1 for l in feed if "侦探徽章（" in l and "→" in l),
        "accusations": len(accusations),
        "accuse_win": any("指认正确" in l for l in accusations),
        "tie_votes": sum(1 for l in feed if "无人过半" in l or "无人得票" in l),
        "blackouts": len(blackout_days),
        "blackout_kills": blackout_kill_count,
        "table_fallbacks": sum(1 for l in feed if "放回会客室桌上" in l),
        "table_thefts": sum(1 for l in feed if "会客室桌上的" in l and "不见了" in l),
        "gun_moves": gun_moves,
        "succession": succession,
        "hall_talks": len(re.findall(r"- 会客室夜谈（", script)),
        "bedroom_talks": len(re.findall(r"- \S+的卧室密谈（", script)),
        "actions": actions, "action_rejects": action_rejects,
        "calls": len(g["host"]), "call_failures": len(g["host"]) - len(host_ok),
        "tokens": sum(h.get("total_tokens") or 0 for h in g["host"]),
        "avg_latency": round(sum(h.get("latency", 0) for h in host_ok) / len(host_ok), 1) if host_ok else 0.0,
    }
    return a


def drama(a: dict) -> tuple[float, list[str]]:
    """戏剧性评分（0–10，分项透明可解释）——供剪辑粗筛，不是质量判决。"""
    parts: list[tuple[str, float]] = []

    def add(label: str, pts: float) -> None:
        if pts:
            parts.append((label, pts))

    add(f"谋杀 {a['murders']} 起", min(3.0, a["murders"] * 1.0))
    add(f"谋杀未遂 {a['attempts']} 次", min(1.5, a["attempts"] * 0.5))
    if a["accusations"]:
        add("武装指认" + ("（指对获胜）" if a["accuse_win"] else "（指错出局）"),
            1.5 + (0.0 if a["accuse_win"] else 0.5))
    add(f"平票/无人出局 {a['tie_votes']} 次", min(1.0, a["tie_votes"] * 0.5))
    if not a["elected"]:
        add("竞选流产（本局无侦探）", 0.5)
    if a["detective_murdered"]:
        add("侦探遇害", 1.5)
    add("警徽传承", min(2.0, a["badge_passed"] * 1.0))
    add(f"佩枪/桌上物品变动 {a['gun_moves']} 次", min(1.6, a["gun_moves"] * 0.8))
    add(f"停电 {a['blackouts']} 次", min(1.6, a["blackouts"] * 0.8))
    if a["succession"]:
        add("杀手继任", 1.0)
    if a["days"] >= 6:
        add("鏖战至第 6 天", 1.0)
    elif a["days"] == 5:
        add("战至第 5 天", 0.5)
    if a["winner"] == "主神":
        add("主神结局（全员失败）", 1.0)
    if a["winner"] == "平民" and a["murders"] == 0 and a["attempts"] == 0:
        add("零出手消极局", -2.0)
    total = max(0.0, min(10.0, sum(p for _, p in parts)))
    return round(total, 1), [f"{label} {'+' if pts >= 0 else ''}{pts}" for label, pts in parts]


def protagonist_candidates(a: dict) -> list[str]:
    """主角弧线候选（剪辑用）：侦探线 / 击杀最多者 / 苟到最后的人。按名去重。"""
    cands: list[str] = []
    seen: set[str] = set()

    def add(name: str, note: str) -> None:
        if name and name not in seen:
            seen.add(name)
            cands.append(f"{name}（{note}）")

    if a["detectives"]:
        add(a["detectives"][0], "侦探" + ("，遇害" if a["detective_murdered"] else ""))
    kills = Counter(c["culprit"] for c in a["cases"])
    if kills:
        k, n = kills.most_common(1)[0]
        add(k, f"{n} 杀")
    if a["alive"]:
        add(a["alive"][0], "存活至终局")
    return cands or ["（无明显主角）"]


def script_briefing(script: str) -> list[str]:
    """流程简报：以 script.md（主持人侧全记录）为基础化简——剥掉开局 preamble
    （标题/背景/阵容表/主人字条，席位段已覆盖）与每日"状态一览"快照表，
    其余逐行保留（发言、公告、密谋频道、行动回执全在）。"""
    lines = script.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("## 第1天")), len(lines))
    out: list[str] = []
    in_status_table = False
    for line in lines[start:]:
        if line.startswith("### ") and "状态一览" in line:
            in_status_table = True
            continue
        if in_status_table:
            if line.startswith("|") or not line.strip():
                continue
            in_status_table = False
        out.append(line)
    return out


def single_report(d: Path) -> str:
    g = load_game(d)
    if g is None:
        return f"{d}：不是对局存档（缺 state.json）"
    a = analyze(g)
    score, parts = drama(a)
    L = [f"===== 单局报告：{a['dir_name']} ====="]
    head = f"seed {a['seed']}｜规则 {a['version']}{'·lite' if a['lite'] else ''}｜历时 {a['days']} 天｜"
    head += f"结局：{a['winner']} 胜（{a['win_reason']}）" if a["complete"] else "结局：（中断/未跑完）"
    L.append(head)
    L.append("")
    L.append("—— 席位 ——")
    for p in a["players"]:
        n = p["name"]
        st = "存活" if p["alive"] else (f"第{a['out_day'].get(n, '?')}天出局" if n in a["out_day"] else "死亡")
        idn = a["ids"].get(n, p["identity"])
        items = "、".join(f"{e['name']}×{e['uses_left']}" for e in p.get("items", [])) or "无"
        L.append(f"  {n}｜{idn}｜{p['profession']}｜{st}｜剩余道具：{items}")
    if a["cases"]:
        L.append("")
        L.append("—— 案件 ——")
        for c in a["cases"]:
            loot = f"｜缴获：{'、'.join(c['looted'])}" if c.get("looted") else ""
            L.append(f"  第{c['day']}天{c['stage']}：{c['victim']} 死于{c['room']}（{c['method']}）{loot}")
    if a["attempt_list"]:
        L.append("")
        L.append("—— 未遂 ——")
        for t in a["attempt_list"]:
            d = f"｜防御：{t['defense']}" if t.get("defense") else ""
            L.append(f"  第{t['day']}天{t['stage']}：{t['target']} 遇袭幸存（{t['room']}）{d}")
    L.append("")
    L.append("—— 流程简报（script.md 化简，含密谋与秘密行动回执）——")
    L.extend(script_briefing(g["script"]) or ["  （无全记录）"])
    L.append("")
    L.append(f"—— 戏剧性评分：{score} / 10 ——")
    L.extend(f"  {p}" for p in parts)
    L.append(f"  主角弧线候选：{'、'.join(protagonist_candidates(a))}")
    L.append("")
    L.append(f"—— 成本 ——\n  API 调用 {a['calls']} 次（失败 {a['call_failures']}）｜总 token {a['tokens']}｜平均延迟 {a['avg_latency']}s")
    return "\n".join(L)


def multi_report(base: Path, min_version: str | None = None) -> str:
    games = []
    for sub in sorted(base.iterdir()) if base.is_dir() else []:
        if sub.is_dir():
            g = load_game(sub)
            if g is not None:
                games.append(analyze(g))
    if not games:
        return f"{base}：没有对局存档（没有含 state.json 的子目录）"
    filtered = ""
    if min_version:
        lo = _ver_tuple(min_version)
        if lo is None:
            return f"--min-version 格式无法识别：{min_version!r}（应为 v3.8 形式）"
        kept = [a for a in games if (_ver_tuple(a["version"]) or (-1, -1)) >= lo]
        filtered = f"（--min-version {min_version} 过滤掉 {len(games) - len(kept)} 局）"
        games = kept
        if not games:
            return f"{base}：没有对局存档满足 --min-version {min_version}"
    done = [a for a in games if a["complete"]]
    L = [f"===== 多局报告：{base}{filtered}（共 {len(games)} 局：完整 {len(done)} / 中断 {len(games) - len(done)}） =====", ""]

    L.append("—— 逐局一览 ——")
    L.append(f"  {'存档':<10}{'版本':<12}{'结局':<12}{'路径':<10}{'天数':<5}{'谋杀':<5}{'戏剧性':<6}")
    for a in games:
        sc, _ = drama(a)
        ver = a["version"] + ("·lite" if a["lite"] else "")
        L.append(f"  {a['dir_name']:<10}{ver:<12}{(a['winner'] or '中断') + '胜' if a['complete'] else '中断':<12}"
                 f"{a['path'] or '-':<10}{a['days']:<5}{a['murders']:<5}{sc:<6}")

    L.append("")
    L.append("—— 胜率平衡（按 版本×规则集 分组、不合并；中断局不计入） ——")
    versions = sorted({a["group"] for a in done})
    for v in versions:
        grp = [a for a in done if a["group"] == v]
        n = len(grp)
        wins = Counter(a["winner"] for a in grp)
        avg_days = sum(a["days"] for a in grp) / n
        avg_kill = sum(a["murders"] for a in grp) / n
        avg_drama = sum(drama(a)[0] for a in grp) / n
        L.append(f"  {v} ×{n}：平民 {wins.get('平民', 0) / n:.0%}｜反派 {wins.get('反派', 0) / n:.0%}｜主神 {wins.get('主神', 0) / n:.0%}"
                 f"｜平均 {avg_days:.1f} 天｜平均谋杀 {avg_kill:.1f}｜平均戏剧性 {avg_drama:.1f}")
    if done:
        L.append(f"  胜负路径分布：{'｜'.join(f'{k} {v}' for k, v in Counter(a['path'] for a in done).most_common())}")

    L.append("")
    L.append("—— 机制利用率（规则是否如预期被使用；完整局） ——")
    if done:
        n = len(done)
        no_elect = sum(1 for a in done if not a["elected"])
        det_camp = Counter(a["ids"].get(a["detectives"][0], "?") for a in done if a["elected"])
        acc = sum(a["accusations"] for a in done)
        acc_win = sum(1 for a in done if a["accuse_win"])
        bl = sum(a["blackouts"] for a in done)
        blk = sum(a["blackout_kills"] for a in done)
        L.append(f"  竞选流产率：{no_elect}/{n}｜侦探初始阵营：{'、'.join(f'{k}×{v}' for k, v in det_camp.items()) or '无'}")
        L.append(f"  武装指认：{acc} 次（指对获胜 {acc_win} 次）｜平票/无人出局：{sum(a['tie_votes'] for a in done)} 次")
        L.append(f"  停电：{bl} 次，停电日谋杀 {blk} 起（转化率 {blk / bl:.0%}）" if bl else "  停电：0 次")
        L.append(f"  桌上凶器：佩枪授予 {sum(1 for a in done if a['elected'])}｜夜间偷取 {sum(a['table_thefts'] for a in done)}"
                 f"｜遗嘱回桌 {sum(a['table_fallbacks'] for a in done)}｜遇害缴获 {sum(1 for a in done for c in a['cases'] if '左轮手枪' in c.get('looted', []))}")
        L.append(f"  杀手继任：{sum(1 for a in done if a['succession'])} 局｜警徽传承：{sum(a['badge_passed'] for a in done)} 次")
        L.append(f"  守夜密谈：{sum(a['bedroom_talks'] for a in done)} 轮｜会客室夜谈：{sum(a['hall_talks'] for a in done)} 轮")
        L.append(f"  反派功能技能（魔术/装置/篡改投票/药理/毒剂/强袭/密道）：出场 {sum(a['villain_valve'] for a in done)} 次"
                 f"｜使用 {sum(a['valve_used'] for a in done)} 次")
        total_actions: Counter = Counter()
        total_rejects: Counter = Counter()
        for a in done:
            total_actions.update(a["actions"])
            total_rejects.update(a["action_rejects"])
        if total_actions:
            L.append("  动作声明（次数，驳回率）：" + "；".join(
                f"{k} {v}（{total_rejects[k] / v:.0%}）" if total_rejects[k] else f"{k} {v}"
                for k, v in total_actions.most_common()))

    L.append("")
    L.append("—— 反派主动性 ——")
    if done:
        n = len(done)
        att = sum(a["attempts"] for a in done)
        mur = sum(a["murders"] for a in done)
        L.append(f"  平均出手率：{sum(a['aggression'] for a in done) / n:.0%}"
                 f"（谋杀 {mur} + 未遂 {att}，窗口按规则版本估算；v3.28 起含首日 2 阶段）")
        defenses = Counter(t["defense"] for a in done for t in a["attempt_list"] if t.get("defense"))
        L.append(f"  未遂防御分布：{'、'.join(f'{k}×{v}' for k, v in defenses.most_common()) or '无记录（v3.10 前存档未留防御途径）'}")
        with_att = [a for a in done if a["attempts"]]
        retry = sum(1 for a in with_att if a["retry_after_fail"])
        L.append(f"  未遂后再出手：{retry}/{len(with_att)} 局｜夜袭空房：{sum(a['empty_raids'] for a in done)} 次"
                 f"｜零谋杀局：{sum(1 for a in done if not a['murders'])}/{n}")

    L.append("")
    L.append("—— 多样性 ——")
    if done:
        methods = Counter(c["method"] for a in done for c in a["cases"])
        rooms = Counter(c["room"] for a in done for c in a["cases"])
        profs = Counter(p["profession"] for a in done for p in a["players"])
        L.append(f"  凶器/手段分布：{'｜'.join(f'{k}×{v}' for k, v in methods.most_common()) or '无'}")
        L.append(f"  案发房间分布：{'｜'.join(f'{k}×{v}' for k, v in rooms.most_common()) or '无'}")
        L.append(f"  职业出场：{'｜'.join(f'{k}×{v}' for k, v in profs.most_common())}")

    L.append("")
    L.append("—— 成本 ——")
    if done:
        L.append(f"  平均每局 API {sum(a['calls'] for a in done) / len(done):.0f} 次"
                 f"｜失败率 {sum(a['call_failures'] for a in done) / max(1, sum(a['calls'] for a in done)):.1%}"
                 f"｜平均 token {sum(a['tokens'] for a in done) / len(done):.0f}"
                 f"｜平均延迟 {sum(a['avg_latency'] for a in done) / len(done):.1f}s")
        pt = sum(a["prompt_tokens"] for a in done)
        if pt:
            L.append(f"  输入缓存命中率：{sum(a['cached_tokens'] for a in done) / pt:.0%}"
                     f"（{sum(a['cached_tokens'] for a in done)}/{pt} tokens）")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="跑局评分卡：扫描对局存档目录（默认 game/），产出单局/多局报告")
    ap.add_argument("--dir", default=str(ROOT / "game"), help="存档目录（默认 game/；精选池传 --dir selected）")
    ap.add_argument("--game", default=None,
                    help="单局报告：存档目录（game/42）、仅 id（42 → game/42）或 --dir 下的子目录名")
    ap.add_argument("--min-version", default=None, metavar="v4.0",
                    help="多局报告只统计该版本及以后的存档（按 rules_version 比较，无法解析版本的旧档被排除）")
    args = ap.parse_args()
    base = Path(args.dir)
    if args.game:
        g = resolve_game_dir(args.game)  # 仅 id 时自动解析 game/<id>
        if not g.is_dir() and (base / args.game).is_dir():
            g = base / args.game  # 再按 --dir 下的子目录名兜底
        print(single_report(g))
    else:
        print(multi_report(base, min_version=args.min_version))


if __name__ == "__main__":
    main()
