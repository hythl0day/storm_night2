Looking at the situation: Day 1 free activity, no murder permitted today. I'm in the reception room alone. The detective election is over — 狄西 won with the revolver. My notes flag 格罗克 as my primary suspicion (medium confidence) because they ran for detective but never disclosed their profession or items.

Since today is safe, I should use this window to gather intelligence. My hacker identity gives me 暴力破解 — the surveillance room is locked, and opening it permanently would let everyone check entry logs. But I also want to probe 格罗克 in private without the audience of the full reception room.

Going to the 监控室 alone would let me review records privately, but inviting 格罗克 creates a one-on-one conversation channel where I can press them about their undisclosed profession. No murder today means zero physical risk. If they decline the invite, I just go alone anyway.

```json
{
  "speech": "格罗克，要不要跟我去监控室看看？反正今天不会出什么事，我想先看看记录熟悉一下情况——你要是愿意来，咱们路上聊聊？",
  "inner": "主动邀请格罗克，是想在私下场合试探他。他竞选侦探时死活不肯公开职业和道具，这很可疑。今天不能杀人，正好是安全窗口。我用暴力破解打开监控室的门，顺便让他看到我的能力——如果他是反派，这个信息对他有用，我能观察他的反应。如果他是平民，合作开门也对大家有利。",
  "notes": "## 第1天\n- 【身份】平民，黑客，持测谎仪×1，暴力破解×1\n- 【事实】狄西当选侦探，持左轮手枪；候选人奇米、格罗克落选；非参选：我、索尔、桑娜\n- 【事实】格罗克竞选时未公开职业/道具，动机可疑\n- 【怀疑】格罗克（中：竞选时刻意隐瞒信息）、狄西（低：待验证）、奇米（低：待验证螺丝起子/建筑师声明）\n- 【计划】自由活动前往监控室，邀请格罗克同入试探；用暴力破解开门；观察监控记录；保留测谎仪不公开",
  "actions": [
    {"type": "choose_room", "room": "监控室"},
    {"type": "invite", "target": "格罗克"}
  ]
}
```