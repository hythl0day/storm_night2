"""共享人读格式化：动作 JSON → 人读文本。

console（屏幕卡片）与 orchestrator（script.md 行动回执）共用，避免编排器反向依赖视图层。
"""
from __future__ import annotations

import json


def third_person(name: str, msg: str) -> str:
    """回执第二人称 → 第三人称（script.md 与 console 的 host 侧展示）：
    引擎回执是发给 AI 本人的第二人称文本（"你用螺丝起子打开了监控室"）。
    含对话引用（：）的消息只转换开头，避免污染引文。"""
    if msg.startswith("你"):
        msg = name + msg[1:]
    if "：" not in msg:
        msg = msg.replace("你的", f"{name}的").replace("你", name)
    return msg


def _case_label(a: dict) -> str:
    """案件标签：主口径受害者名字（case/victim 字段），兼容旧式整数 case_id。"""
    name = a.get("case") or a.get("victim")
    return f"{name}案" if name else f"案件 {a.get('case_id', '?')}"


def fmt_action(a: dict) -> str:
    """动作 JSON → 人读文本（展示用；落盘的原始 JSON 不受影响）。"""
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
        return "声明今天停电"
    if t == "murder":
        w = a.get("weapon")
        return f"谋杀 {a.get('target', '?')}" + (f"（凶器：{w}）" if w else "")
    if t == "stage_magic":
        return f"舞台魔术 → {a.get('target', '?')}"
    if t == "falsify":
        return f"{a.get('kind', '伪造')}（{_case_label(a)}）"
    if t == "move_body":
        return f"转移尸体（{_case_label(a)} → {a.get('to_room', '?')}）"
    if t == "investigate":
        return f"现场调查（{_case_label(a)}：{a.get('choice', '?')}）"
    if t == "profile":
        return f"犯罪侧写（{_case_label(a)}：{a.get('aspect', '?')}）"
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
    if t == "take_table":
        return f"取桌上物品：「{a.get('item', '?')}」"
    return json.dumps(a, ensure_ascii=False)
