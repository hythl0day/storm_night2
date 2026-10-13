"""交互驾驶舱：进程内驱动 orchestrator 跑局 + 流式渲染 + 随时可输入命令。

用法：
    python -m src.console --game game/012345 [--resume]

显示设计（纯 ANSI，无第三方依赖）：
  - 所有输出走正常终端滚动流，**滚轮可回看全部历史**；
  - 阶段开始/结束用 ══ 双线横幅（前留空行），席位行动用 ── 单线分隔，层级分明；
  - 底部固定三行块：分隔线 / 输入行（> 缓冲 + 固定光标▌）/ 状态行，
    输出时先擦掉整块、输出完重画；思考流按"行"批量吐出，提示区只在换行边界重画，
    因此**思考流持续期间输入行与状态行始终可见、打字即时回显**；
  - 每次调用开始先打印角色标题行，思考内容灰色滚动；JSON 正文不刷屏，
    解析完成后渲染人读卡片（发言亮白加粗 / 内心灰色 / 行动黄色），
    动作裁决结果与私密回执（调查/侧写/植入明细等 host 侧信息）以暗色行随附。

命令：help / status / log [n] / peek <席位> / inner <席位> / host [n] /
      follow [席位] / pause / continue / save / quit

中断统一语义（席位行动粒度）：pause、Ctrl+C、API 调用失败三者等价——当前席位行动结束
（API 失败则该席未完成）后落盘暂停；输入 continue 在进程内续跑，被中断/待行动的席位自动
重新执行；quit 退出进程（同样已落盘，可 --resume 恢复）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import msvcrt
except ImportError:  # 非 Windows 无 msvcrt；本项目环境为 Windows
    msvcrt = None

from . import viewer
from .orchestrator import OPENING, AbortRun, Orchestrator, morning_roster_rows

SPINNER = "⠋⠙⠹⠸⠼⠴⦷⦯⦮⦿"
SEPARATOR = "─" * 40
STAGE_SEP = "═" * 46  # 阶段级双线分隔符（比席位级的 ── 更醒目）
CURSOR = "▌"

HELP = """命令：
  status            阶段/存活/身份台账/道具技能次数（读最近落盘）
  log [n]           公开日志末尾 n 行（默认 20）
  peek <席位>       私密卡 + 私人笔记 + 内心独白
  inner <席位>      仅内心独白
  host [n]          API 调用台账（耗时/成败，默认 10 条）
  follow [席位]     跟随某席思考流；不带参数 = 跟随当前调用席（默认）
  pause             在当前席位行动结束后暂停（Ctrl+C 等效；API 失败会自动暂停）
  continue          暂停后继续跑局（resume 是别名；被中断/待行动席位自动重新执行）
  save              立即落盘 state.json
  quit              退出进程（进度已按席位粒度落盘，可 --resume 恢复）"""


class Display:
    """终端显示：输出走正常滚动流（滚轮可回看）；底部三行提示块（分隔线/输入行/状态行）。

    同时充当 sys.stdout：所有 print / 流式 token 经 write() 输出。
    不变式：提示块只存在于输出处于行首（非半行）时；真实光标始终停在状态行末尾，
    输入行里的 ▌ 是画上去的"固定光标"。erase/draw 都按三行整块处理。
    """

    def __init__(self) -> None:
        self.real = sys.stdout
        self.lock = threading.RLock()
        self.input_buf = ""
        self.status = ""
        self._partial = False  # 输出停在半行（提示块当前被擦除）
        self._draw_block()

    def _draw_block(self) -> None:
        """在光标处绘制三行提示块，结束时光标停在状态行末尾。"""
        self.real.write(
            f"\x1b[90m{SEPARATOR}\x1b[0m\r\n"
            f"\x1b[K> {self.input_buf}{CURSOR}\r\n"
            f"\x1b[K\x1b[90m{self.status}\x1b[0m"
        )
        self.real.flush()

    def _erase_block(self) -> None:
        """擦掉三行提示块，光标回到分隔线行首（= 输出应写入的位置）。"""
        self.real.write("\r\x1b[K"        # 清状态行
                        "\x1b[1A\r\x1b[K"   # 清输入行
                        "\x1b[1A\r\x1b[K")  # 清分隔线

    def write(self, text: str) -> int:      # sys.stdout 接口
        if not text:
            return 0
        with self.lock:
            if not self._partial:
                self._erase_block()
            self.real.write(text.replace("\n", "\r\n"))
            self._partial = not text.endswith("\n")
            if not self._partial:
                self._draw_block()
            else:
                self.real.flush()
        return len(text)

    def flush(self) -> None:
        self.real.flush()

    @property
    def encoding(self) -> str:
        return self.real.encoding or "utf-8"

    def _redraw_if_idle(self) -> None:
        if not self._partial:
            self._erase_block()
            self._draw_block()

    def set_status(self, text: str) -> None:
        with self.lock:
            self.status = text
            self._redraw_if_idle()

    def set_input(self, buf: str) -> None:
        with self.lock:
            self.input_buf = buf
            self._redraw_if_idle()

    def newline(self) -> None:
        """强制结束半行（一次调用结束时用，让提示块回归）。"""
        with self.lock:
            if self._partial:
                self.real.write("\r\n")
                self._partial = False
                self._draw_block()

    def close(self) -> None:
        with self.lock:
            if not self._partial:
                self._erase_block()
            self.real.flush()


def clean_text(s: str) -> str:
    """去掉模型输出里的多余空行（单个回车保留），两端留白裁剪。"""
    return "\n".join(ln for ln in (ln.rstrip() for ln in s.strip().splitlines()) if ln)


def fmt_action(a: dict) -> str:
    """动作 JSON → 人读文本（控制台展示用；落盘的原始 JSON 不受影响）。"""
    if not isinstance(a, dict):
        return str(a)
    t = a.get("type", "")
    if t == "choose_room":
        return f"选择房间：{a.get('room', '?')}"
    if t == "invite":
        return f"邀请 {a.get('target', '?')} 同入/守夜"
    if t == "stay":
        return "回自己卧室过夜"
    if t == "accept_invite":
        return "接受守夜邀请"
    if t == "night_room":
        return f"夜间前往 {a.get('room', '?')} 执行 {a.get('do', '?')}"
    if t == "night_hall":
        return "夜宿会客室"
    if t == "gift":
        return f"把「{a.get('item', '?')}」赠送给 {a.get('to', '?')}"
    if t == "accept":
        return "接受赠送"
    if t == "decline":
        return "拒绝赠送"
    if t == "see_through":
        return f"看破伪装 → {a.get('target', '?')}"
    if t == "view_monitoring":
        return "查看监控"
    if t == "tamper":
        return f"篡改监控（{a.get('op', '?')}：{a.get('period', '?')}/{a.get('room', '?')}）"
    if t == "open_safe":
        return "打开保险箱"
    if t == "unlock":
        return f"开锁：{a.get('room', '?')}"
    if t == "use_flashlight":
        return "使用手电筒"
    if t == "fix_generator":
        return "修复发电机"
    if t == "bug_listen":
        return "收听窃听器"
    if t == "seal_room":
        allowed = "、".join(a.get("allowed") or []) or "无"
        return f"封闭当前房间（许可留下：{allowed}）"
    if t == "set_passage":
        return f"建造密道：{a.get('room', '?')}"
    if t == "place_bug":
        return f"放置窃听器：{a.get('room', '?')}"
    if t == "use_passage":
        return "使用密道"
    if t == "set_device":
        return f"设置延时装置：{a.get('room', '?')}"
    if t == "declare_blackout":
        return "声明今晚停电"
    if t == "murder":
        w = a.get("weapon")
        return f"谋杀 {a.get('target', '?')}" + (f"（凶器：{w}）" if w else "")
    if t == "stage_magic":
        return f"舞台魔术 → {a.get('target', '?')}"
    if t == "falsify":
        return f"伪造痕迹（案件 {a.get('case_id', '?')}）"
    if t == "move_body":
        return f"转移尸体（案件 {a.get('case_id', '?')} → {a.get('to_room', '?')}）"
    if t == "investigate":
        return f"现场调查（案件 {a.get('case_id', '?')}：{a.get('choice', '?')}）"
    if t == "profile":
        return f"犯罪侧写（案件 {a.get('case_id', '?')}：{a.get('aspect', '?')}）"
    if t == "declare":
        return "宣告参选侦探" if a.get("run") else "宣告不参选"
    if t == "elect_stay":
        return "继续参选（竞选留任）"
    if t == "withdraw":
        return "退选"
    if t == "elect":
        return f"竞选投票 → {a.get('target', '?')}"
    if t == "vote":
        return f"投票 → {a.get('target', '?')}"
    if t == "abstain":
        return "弃权"
    if t == "memory_implant":
        return f"记忆植入：{a.get('voter', '?')} → {a.get('target', '?')}"
    if t == "bequeath":
        return f"立遗嘱：{a.get('mapping', {})}"
    if t == "bequest_reply":
        return f"遗嘱回应：{a.get('decisions', {})}"
    if t == "armed_accuse":
        return f"武装指认 → {a.get('target', '?')}"
    if t == "night_raid":
        return f"夜袭 → {a.get('room', '?')}"
    if t == "take_table":
        return f"取桌上物品：「{a.get('item', '?')}」"
    return json.dumps(a, ensure_ascii=False)


def day_roster(orch: Orchestrator) -> str:
    """天亮一览：保险箱状态 + 全员状态（含淘汰者）+ 职业/技能/道具（宿主侧，不进 prompt、不落盘）。"""
    s = orch.state
    safe = f"  保险箱：「{s.get('safe_item', '?')}」" + ("（已打开）" if s.get("safe_open") else "（未打开）")
    safe += f"｜侦探：{s.get('detective') or '无'}"
    table = "、".join(f"「{e['name']}」×{e['uses_left']}" for e in s.get("table_items", [])) or "（空）"
    safe += f"｜桌上：{table}"
    if s.get("power_out"):
        safe += "｜停电中"
    rows = morning_roster_rows(s, orch.orch.get("out_day", {}))
    return safe + "\n" + "\n".join(f"  {n}｜{st}｜{ident}｜{prof}｜技能：{sk}｜道具：{it}"
                     for n, st, ident, prof, sk, it in rows)


# 阶段环境描写（宿主侧氛围渲染，不进 prompt、不落盘）
STAGE_FLAVOR = {
    "开局准备": "众人从昏迷中醒来，发现自己身处一座陌生的古堡……",
    "侦探竞选": "会客室里烛火摇曳。第一天的第一件事：推选一位领头人。",
    "公共发言": "会客室里烛火摇曳。众人围坐长桌，目光在彼此之间游移。",
    "自由活动": "人群散去，古堡的走廊里响起四散的脚步声。",
    "晚间休息": "夜深了，暴雨拍打着窗棂。各自回房——或者，不回。",
    "清晨": "清晨。众人重新聚到会客室。",
    "案件调查": "气氛骤紧——出事了。",
    "投票": "投票的时刻到了。",
}


class Ctx:
    def __init__(self, orch: Orchestrator, display: Display):
        self.orch = orch
        self.display = display
        self.halt = False                    # 暂停请求（pause/Ctrl+C）：在席位断点生效
        self.quit_app = False                # 退出进程
        self.paused = False                  # worker 已停在断点，等待 continue
        self.continue_ev = threading.Event() # continue 命令置位，唤醒 worker 续跑
        self.done = threading.Event()
        self.follow: str | None = None      # None = 跟随当前调用席
        self.current_seat = ""
        self.current_label = ""
        self.current_stage = ""            # 当前阶段标题（阶段结束分隔符用）
        self.call_start = 0.0
        self.tokens = 0
        self.reason_buf = ""                # 思考流按行批量吐出的缓冲
        self.day_shown = 0                  # 已显示"天亮一览"的天数（每天开始只显示一次）


def make_hooks(ctx: Ctx) -> dict:
    def flush_reason() -> None:
        """把思考缓冲吐到屏幕上：只吐到最后一个换行符（保持输出行首不变式）。"""
        buf = ctx.reason_buf
        cut = buf.rfind("\n") + 1
        if cut == 0:
            return
        ctx.reason_buf = buf[cut:]
        ctx.display.write(f"\x1b[94m{buf[:cut]}\x1b[0m")

    def flush_remaining() -> None:
        if ctx.reason_buf:
            buf, ctx.reason_buf = ctx.reason_buf, ""
            ctx.display.write(f"\x1b[94m{buf}\x1b[0m")

    def on_call_start(name: str, label: str) -> None:
        ctx.current_seat, ctx.current_label = name, label
        ctx.call_start, ctx.tokens = time.monotonic(), 0
        ctx.reason_buf = ""
        print(f"\n\x1b[1;36m── {name} · {label} {SEPARATOR}\x1b[0m")

    def on_token(text: str, is_reasoning: bool) -> None:
        ctx.tokens += len(text)
        # 只有思考内容实时显示（蓝色、按行批量）；JSON 正文等解析完后统一渲染卡片
        if is_reasoning and (ctx.follow is None or ctx.follow in ctx.current_seat):
            ctx.reason_buf += text
            flush_reason()

    def on_payload(name: str, label: str, payload: dict) -> None:
        """解析完成后渲染人读卡片（标题行已在 on_call_start 打印）。"""
        flush_remaining()
        ctx.display.newline()
        speech = clean_text(str(payload.get("speech") or ""))
        inner = clean_text(str(payload.get("inner") or ""))
        actions = payload.get("actions") or []
        if speech:
            print(f"\x1b[33m{name}：{speech}\x1b[0m")
        if inner:
            print(f"\x1b[90m（内心）{inner}\x1b[0m")
        for a in actions:
            print(f"\x1b[91m→ {fmt_action(a)}\x1b[0m")
        if not speech and not actions:
            print("\x1b[90m（无发言，无动作）\x1b[0m")

    def on_call_end(name: str, label: str, result) -> None:
        flush_remaining()
        ctx.display.newline()
        ctx.current_seat = ""
        if result.ok:
            tok = result.usage.get("total_tokens")
            print(f"\x1b[90m  ({result.latency:.1f}s" + (f" · {tok} tok" if tok else "") + ")\x1b[0m")
        else:
            print(f"\x1b[90m  ✗ {result.error}\x1b[0m")

    def on_stage(title: str) -> None:
        if ctx.current_stage and ctx.current_stage != title:
            print(f"\n\x1b[90m── {ctx.current_stage} · 结束 {SEPARATOR}\x1b[0m\n")
        ctx.current_stage = title
        print(f"\n\n\x1b[1;35m{STAGE_SEP}\x1b[0m")
        print(f"\x1b[1;35m  {title}\x1b[0m")
        print(f"\x1b[1;35m{STAGE_SEP}\x1b[0m")
        m = re.search(r"第(\d+)天", title)
        if m and int(m.group(1)) != ctx.day_shown:
            ctx.day_shown = int(m.group(1))
            alive = sum(1 for p in ctx.orch.state["players"] if p["alive"])
            print(f"\x1b[1;33m—— 第{ctx.day_shown}天开始 · 存活 {alive} 人 ——\x1b[0m")
            print(day_roster(ctx.orch))
        for key, line in STAGE_FLAVOR.items():
            if key in title:
                print(f"\x1b[3;90m{line}\x1b[0m")
                break

    def on_public(line: str) -> None:
        print(f"\x1b[92m{line}\x1b[0m")

    def on_result(name: str, ok: bool, detail: str) -> None:
        """动作裁决结果（host 侧，与 on_payload 的红色声明行配对）。"""
        print(f"\x1b[90m  {'✓' if ok else '✗'} {detail}\x1b[0m")

    def on_private(name: str, msg: str) -> None:
        """私密回执（host 侧全剧透）：调查/侧写结果、继任通知、记忆植入明细等。"""
        print(f"\x1b[90m  [{name}] {msg}\x1b[0m")

    return {
        "on_call_start": on_call_start,
        "on_token": on_token,
        "on_payload": on_payload,
        "on_call_end": on_call_end,
        "on_stage": on_stage,
        "on_public": on_public,
        "on_result": on_result,
        "on_private": on_private,
        "should_stop": lambda: ctx.halt or ctx.quit_app,
    }


def handle_command(line: str, ctx: Ctx) -> None:
    parts = line.strip().split()
    if not parts:
        return
    cmd, args = parts[0], parts[1:]
    game_dir = ctx.orch.dir
    try:
        if cmd == "help":
            print(HELP)
        elif cmd == "status":
            print(viewer.render_status(game_dir))
        elif cmd == "log":
            print(viewer.render_log(game_dir, int(args[0]) if args else 20))
        elif cmd == "peek":
            print(viewer.render_peek(game_dir, args[0]) if args else "用法：peek <席位>")
        elif cmd == "inner":
            print(viewer.render_inner(game_dir, args[0]) if args else "用法：inner <席位>")
        elif cmd == "host":
            print(viewer.render_host_log(game_dir, int(args[0]) if args else 10))
        elif cmd == "follow":
            ctx.follow = args[0] if args else None
            print(f"已切换 follow：{ctx.follow or '当前调用席'}")
        elif cmd == "pause":
            if ctx.paused:
                print("已处于暂停状态（continue 继续，quit 退出）")
            elif ctx.done.is_set():
                print("跑局已结束，无需暂停")
            else:
                ctx.halt = True
                print("将在当前席位行动结束后暂停并保存（continue 继续）")
        elif cmd in ("continue", "resume"):
            if ctx.paused:
                ctx.continue_ev.set()
            elif ctx.done.is_set():
                print("跑局已结束，没有可继续的进度（开新局或 --resume 另一个存档）")
            else:
                print("跑局进行中（pause 暂停后才需要 continue）")
        elif cmd == "save":
            try:
                ctx.orch.save_state()
                print("已落盘 state.json")
            except RuntimeError:
                print("状态正在变动，落盘失败，请重试")
        elif cmd == "quit":
            ctx.quit_app = True
            ctx.halt = True
            ctx.continue_ev.set()   # 若 worker 正暂停等待 continue，唤醒来走退出分支
            if ctx.paused:
                print("退出中…")
            else:
                print("等待当前席位行动结束后退出…（再按 Ctrl+C 立即强制退出，进度可从 --resume 恢复）")
        else:
            print(f"未知命令 {cmd!r}，输入 help 查看")
    except Exception as e:
        print(f"命令执行失败：{type(e).__name__}: {e}")


def status_bar(ctx: Ctx) -> str:
    ph = ctx.orch.state["phase"]
    parts = [f"第{ph['day']}天·{ph['stage']}"]
    if ctx.paused:
        parts.append("⏸已暂停")
    if ctx.current_seat:
        el = time.monotonic() - ctx.call_start
        sp = SPINNER[int(el * 8) % len(SPINNER)]
        parts.append(f"{ctx.current_seat}·{ctx.current_label} {sp} {el:.0f}s·{ctx.tokens}字")
    elif not ctx.done.is_set():
        parts.append("待命中")
    if ctx.follow:
        parts.append(f"follow:{ctx.follow}")
    return " | ".join(parts)


def ticker(ctx: Ctx) -> None:
    while not ctx.done.is_set():
        ctx.display.set_status(status_bar(ctx))
        time.sleep(0.5)


def worker(ctx: Ctx) -> None:
    """跑局工作线程：pause/Ctrl+C（halt → SoftStop）与 API 失败（AbortRun）统一在此收束为
    "暂停等待"状态——进度已按席位粒度落盘；continue 命令置位 continue_ev 后在进程内续跑，
    被中断/待行动的席位自动重新执行；quit 随时可退出进程。"""
    orch = ctx.orch
    while True:
        try:
            orch.run_game()
            if orch.state["winner"]:
                w = orch.state
                if ctx.current_stage:
                    print(f"\n\x1b[90m── {ctx.current_stage} · 结束 {SEPARATOR}\x1b[0m")
                    ctx.current_stage = ""
                print(f"\n== 对局结束：{w['winner']}获胜——{w.get('win_reason', '')} ==")
                print("（可继续用命令查看，quit 退出）")
                break
            # run_game 未分胜负即返回 = halt 请求在席位断点生效（pause / Ctrl+C / quit）
        except AbortRun as e:
            print(f"\n[跑局中断] API 调用失败：{e}")
        except Exception as e:
            print(f"\n[编排器异常] {type(e).__name__}: {e}（进度已保存）")
        if ctx.quit_app:
            print("\n== 已退出跑局（进度已保存，可用 --resume 续跑） ==")
            break
        ctx.halt = False
        ctx.paused = True
        print("\n== 已暂停，进度已保存（席位行动粒度）==")
        print("continue 继续（被中断/待行动的席位自动重新执行）；quit 退出（可 --resume 续跑）")
        while not ctx.continue_ev.wait(0.2):
            if ctx.quit_app:
                break
        ctx.paused = False
        ctx.continue_ev.clear()
        if ctx.quit_app:
            print("\n== 已退出（进度已保存，可用 --resume 续跑） ==")
            break
        print("== 继续跑局 ==")
    ctx.done.set()


def input_loop(ctx: Ctx) -> None:
    buf = ""
    while not ctx.quit_app:
        if msvcrt is not None and msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                line, buf = buf, ""
                ctx.display.set_input("")
                print(f"> {line}")
                handle_command(line, ctx)
            elif ch == "\x08":
                buf = buf[:-1]
                ctx.display.set_input(buf)
            elif ch == "\x03":
                # Ctrl+C 与 pause 同语义；已暂停/已结束时再按 = 退出
                handle_command("quit" if (ctx.paused or ctx.done.is_set()) else "pause", ctx)
            elif ch in ("\xe0", "\x00"):
                msvcrt.getwch()     # 丢弃功能键第二字节
            elif ch >= " ":
                buf += ch
                ctx.display.set_input(buf)
        elif msvcrt is None:
            line = sys.stdin.readline()     # 兜底：行模式
            if not line:
                break
            handle_command(line, ctx)
        else:
            time.sleep(0.05)


def main() -> None:
    ap = argparse.ArgumentParser(prog="console", description="《暴风夜》交互驾驶舱")
    ap.add_argument("--game", required=True, help="对局目录（deal.py 产物，如 game/012345）")
    ap.add_argument("--resume", action="store_true", help="从 state.json 断点续跑（阶段粒度）")
    args = ap.parse_args()

    game_dir = Path(args.game)
    if not (game_dir / "state.json").exists():
        raise SystemExit(f"找不到 {game_dir / 'state.json'}（先用 python -m src.deal 生成对局）")

    display = Display()
    sys.stdout = display     # 全部 print/流式 token 经 Display
    ctx: Ctx | None = None
    t_work: threading.Thread | None = None
    try:
        orch = Orchestrator(game_dir)
        ctx = Ctx(orch, display)
        orch.hooks = make_hooks(ctx)

        ph = orch.state["phase"]
        print(f"对局 {game_dir}：从 第{ph['day']}天 · {ph['stage']} 开始{'（续跑）' if args.resume else ''}")
        print("输入 help 查看命令。")
        if not args.resume:
            print(f"\n\x1b[3m{OPENING}\x1b[0m")
            for fname in ("roster.md", "announcement.md"):  # 环境描写：角色表 + 主人字条
                f = game_dir / "public" / fname
                if f.exists():
                    print(f"\n\x1b[90m{f.read_text(encoding='utf-8').strip()}\x1b[0m")
            orch.log_header(f"第{ph['day']}天 · 开局")

        ctx.day_shown = ph["day"]
        alive = sum(1 for p in orch.state["players"] if p["alive"])
        print(f"\n\x1b[1;33m—— 第{ph['day']}天开始 · 存活 {alive} 人 ——\x1b[0m")
        print(day_roster(orch))

        t_work = threading.Thread(target=worker, args=(ctx,), daemon=True)
        t_tick = threading.Thread(target=ticker, args=(ctx,), daemon=True)
        t_work.start()
        t_tick.start()

        try:
            while not ctx.quit_app:
                try:
                    input_loop(ctx)
                except KeyboardInterrupt:
                    # Ctrl+C 与 pause 同语义；已暂停/已结束时再按 = 退出
                    if ctx.paused or ctx.done.is_set():
                        ctx.quit_app = True
                        ctx.continue_ev.set()
                    else:
                        ctx.halt = True
                        print("\n[Ctrl+C] 将在当前席位行动结束后暂停并保存"
                              "（continue 继续；暂停后再按 Ctrl+C 或输入 quit 退出）")
        except Exception as e:
            print(f"输入循环异常：{type(e).__name__}: {e}")
            ctx.quit_app = True
            ctx.halt = True
            ctx.continue_ev.set()

        if t_work.is_alive():
            print("等待当前席位行动结束后安全退出…（再按 Ctrl+C 立即强制退出）")
            try:
                while t_work.is_alive():
                    t_work.join(timeout=0.5)
            except KeyboardInterrupt:
                print("\n强制退出。")
        ctx.done.set()
    finally:
        sys.stdout = display.real
        display.close()
    if t_work is not None and t_work.is_alive():
        os._exit(0)     # worker 卡在 API 调用上，守护线程随进程退出


if __name__ == "__main__":
    main()
