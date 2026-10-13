"""M5 编排器全流程集成测试：mock providers.chat，不打真实 API。

用脚本化全知策略驱动 run_game() 主循环，验证：
- 倒计时结局（全程消极：无谋杀行为、投票全弃权 → 第 4 天清晨平民胜，倒计时自第 2 天起算）
- 主神结局（一次未遂重置倒计时后再无行为 → 拖至第 5 天晚间）
- 平民胜结局（杀手第 2 天谋杀 → 第 3 天投票出局 → 帮凶继任再杀 → 第 4 天被投出）
- 阶段边界停止 + 重建 Orchestrator 续跑至结局（--resume 语义）
- 归档产物：public/log.md 结局行、private 内心独白、inbox/outbox 存档

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import json
import re
import tempfile
import unittest
from pathlib import Path

from src import orchestrator
from src.orchestrator import Orchestrator
from src.providers import ChatResult
from test_engine import P, default_players, mk_state

RULES_SNAPSHOT = "（测试用规则书快照）"


def write_game_dir(root: Path, players) -> Path:
    """在临时目录构造 deal 产物的最小等价物（state/rules/players.toml/私密卡）。"""
    d = root / "game"
    (d / "public").mkdir(parents=True)
    (d / "rules.md").write_text(RULES_SNAPSHOT, encoding="utf-8")
    toml = ""
    for p in players:
        toml += f'[[player]]\nname = "{p["name"]}"\nmodel = "mock"\nbase_url = "http://localhost"\napi_key_env = "MOCK_KEY"\n\n'
    (d / "players.toml").write_text(toml, encoding="utf-8")
    for p in players:
        seat = d / "private" / p["name"]
        seat.mkdir(parents=True)
        (seat / "card.md").write_text(f"你是 {p['name']}（{p['identity']}/{p['profession']}）", encoding="utf-8")
    state = mk_state(players=players)
    (d / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return d


def payload(speech="", inner="……", notes="", actions=None) -> str:
    return "```json\n" + json.dumps(
        {"speech": speech, "inner": inner, "notes": notes, "actions": actions or []},
        ensure_ascii=False,
    ) + "\n```"


def make_fake_chat(killer_script: bool):
    """脚本化全知策略。killer_script=False：全程消极（主神结局）；True：A/B 依次作案、平民第 3 天起投反派。"""

    def fake_chat(player, messages, *, on_token=None, reasoning_effort=None):
        name = player.name
        briefing = messages[-1]["content"]
        title = briefing.splitlines()[0]  # 只在标题行上分派：正文里的历史公告含"投票结果"等字样，会误判
        m = re.search(r"第(\d+)天", title)
        day = int(m.group(1)) if m else 1
        speech = f"{name} 的第{day}天发言"
        acts: list[dict] = []
        if "开局准备" in title:
            pass
        elif "侦探竞选声明" in title:
            acts = [{"type": "declare", "run": name in ("C", "D")}]  # v3.3：C/D 参选，竞选票 C 当选
        elif "侦探竞选退选" in title:
            acts = [{"type": "elect_stay"}]
        elif "侦探竞选投票" in title:
            acts = [{"type": "elect", "target": "C"}]
        elif "遗言" in title:
            speech = "我无话可说。"
            acts = [{"type": "bequeath", "mapping": {"万能钥匙": "D"}}]
        elif "死者字条" in title:
            acts = [{"type": "bequeath", "mapping": {"万能钥匙": "D"}}]
        elif "遗嘱回应" in title:
            acts = [{"type": "bequest_reply", "decisions": {"万能钥匙": "接受"}}]
        elif "案件调查" in title:
            if '"type": "profile"' in briefing:
                acts = [{"type": "profile", "case_id": 1, "aspect": "性别"}]
        elif "武装指认" in title:
            pass
        elif "投票" in title:
            if killer_script and day >= 3 and name not in ("A", "B"):
                acts = [{"type": "vote", "target": "A" if day == 3 else "B"}]
            else:
                acts = [{"type": "abstain"}]
        elif "选房" in title:
            room = "会客室"  # v3：白天卧室清场，默认苟大厅
            if killer_script and day == 2 and name in ("A", "E"):
                room = "书房"      # A 第 2 天在书房杀 E（E 无自卫手段）
            if killer_script and day >= 3 and name in ("B", "F"):
                room = "书房"      # B 继任后第 3 天在书房杀 F（v3.2：监控室上锁，改走书房）
            acts = [{"type": "choose_room", "room": room}]
        elif "内交谈" in title:
            pass  # 只发言
        elif "房间行动" in title or "夜袭" in title:
            if killer_script and '"type": "murder"' in briefing and name in ("A", "B"):
                t = re.search(r'"target": "([^"|]+)"', briefing)
                acts = [{"type": "murder", "target": t.group(1), "weapon": "刀" if name == "A" else "不明药剂"}]
        elif "晚间休息" in title or "毁证机会" in title or "交换环节" in title:
            pass
        return ChatResult(
            player.name,
            ok=True,
            content=payload(speech=speech, notes=f"## 第{day}天\n- 占位", actions=acts),
            latency=0.01,
            usage={"total_tokens": 5},
            finish_reason="stop",
        )

    return fake_chat


class MockGameTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_game(self, game_dir: Path, killer_script: bool, hooks: dict | None = None):
        return self.run_game_with(game_dir, make_fake_chat(killer_script), hooks=hooks)

    def run_game_with(self, game_dir: Path, chat_fn, hooks: dict | None = None):
        orch = Orchestrator(game_dir, hooks=hooks)
        orig = orchestrator.chat
        orchestrator.chat = chat_fn
        try:
            orch.run_game()
        finally:
            orchestrator.chat = orig
        return orch


class TestMainGodEnding(MockGameTestBase):
    def test_passive_game_countdown_civilian_win(self):
        """全程消极（无谋杀行为、投票全弃权）：倒计时自第 2 天起算，第 4 天清晨平民胜。"""
        d = write_game_dir(self.root, default_players())
        orch = self.run_game(d, killer_script=False)
        s = orch.state
        self.assertEqual(s["winner"], "平民")
        self.assertIn("无杀倒计时", s["win_reason"])
        self.assertEqual(s["phase"], {"day": 4, "stage": "案件调查"})
        self.assertTrue(all(p["alive"] for p in s["players"]))  # 全程无死无伤
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("游戏结束：平民获胜——无杀倒计时完成", log)
        self.assertIn("投票结果：无人得票", log)
        self.assertIn("无人过半数", log)  # 投票结果附明确裁决（是否有人出局）
        # 归档：内心独白 / 简报与回执 / host_log
        self.assertTrue((d / "private" / "A" / "inner.md").exists())
        self.assertTrue((d / "outbox" / "A" / "day1-竞选声明.md").exists())  # v3.3：第 1 天公共发言改为侦探竞选
        self.assertTrue((d / "inbox" / "F" / "day1-开局准备.md").exists())
        self.assertTrue((d / "host_log.jsonl").read_text(encoding="utf-8").strip())
        # state.json 落盘即终局
        saved = json.loads((d / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["winner"], "平民")
        # script.md 全记录：背景 / 阵容分配（全剧透）/ 流程（选房/归宿/投票明细）/ 结局
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("# 《暴风夜》对局全记录", script)
        self.assertIn("## 背景", script)
        self.assertIn("## 阵容与分配（全剧透）", script)
        self.assertIn("| A | 男 | 魁梧 | 中国 | 杀手 | 魔术师 | 刀×1 |", script)
        self.assertIn("书房保险箱：左轮手枪（开局公告已公开）", script)
        self.assertIn("- 选房结果：", script)
        self.assertIn("- 晚间归宿：", script)
        self.assertIn("- 投票明细：", script)
        self.assertIn("游戏结束：平民获胜", script)
        self.assertNotIn("……", script)  # mock 的 inner 固定为 "……"：内心独白不得进全记录

    def test_maingod_after_failed_attempt(self):
        """未遂重置倒计时：第 3 天一次未遂后再无行为，游戏拖至第 5 天晚间主神胜。"""
        d = write_game_dir(self.root, default_players())
        base = make_fake_chat(False)

        def chat_attempt(player, messages, *, on_token=None, reasoning_effort=None):
            title = messages[-1]["content"].splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if day == 3 and "选房" in title and player.name in ("A", "D"):
                return ChatResult(player.name, ok=True,
                                  content=payload(actions=[{"type": "choose_room", "room": "书房"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if day == 3 and "房间行动" in title and player.name == "A" and '"type": "murder"' in messages[-1]["content"]:
                return ChatResult(player.name, ok=True,
                                  content=payload(actions=[{"type": "murder", "target": "D", "weapon": "刀"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_attempt)
        s = orch.state
        self.assertEqual(s["winner"], "主神")
        self.assertEqual(s["phase"], {"day": 5, "stage": "晚间休息"})
        self.assertEqual(len(s["attempts"]), 1)   # D 运动员【敏捷闪避】防下唯一一次未遂
        self.assertTrue(all(p["alive"] for p in s["players"]))

    def test_stop_and_resume(self):
        d = write_game_dir(self.root, default_players())
        calls = {"n": 0}

        def on_stage(_title):
            calls["n"] += 1

        orch = self.run_game(d, killer_script=False,
                             hooks={"on_stage": on_stage, "should_stop": lambda: calls["n"] >= 2})
        self.assertIsNone(orch.state["winner"])  # 停在中途
        saved = json.loads((d / "state.json").read_text(encoding="utf-8"))
        self.assertIsNone(saved["winner"])
        orch2 = self.run_game(d, killer_script=False)  # 重建实例续跑至结局
        self.assertEqual(orch2.state["winner"], "平民")
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertEqual(script.count("# 《暴风夜》对局全记录"), 1)  # 续跑不重写开头


class TestCivilianWinEnding(MockGameTestBase):
    def players(self):
        # 职业不重复；A/B 都有开锁手段（黑客/机械师的【暴力破解】）用于潜入他人卧室；
        # 受害者 E/F 均无自卫手段（C 的急救箱、D 的侧写都挡不了谋杀，故不选他们做目标）。
        return [
            P("A", "杀手", "黑客", "刀", gender="男", physique=4),
            P("B", "帮凶", "机械师", "不明药剂", gender="女", physique=2),
            P("C", "平民", "医师", "急救箱", gender="女", physique=3),
            P("D", "平民", "犯罪侧写师", "手电筒", gender="男", physique=3),
            P("E", "平民", "建筑师", "万能钥匙", gender="男", physique=2),
            P("F", "平民", "心理医生", "窃听器", gender="女", physique=2),
        ]

    def test_killer_voted_out_then_successor_voted_out(self):
        players = self.players()
        d = write_game_dir(self.root, players)
        orch = self.run_game(d, killer_script=True)
        s = orch.state
        self.assertEqual(s["winner"], "平民")
        self.assertIn("杀手与帮凶均已出局", s["win_reason"])
        names = {p["name"]: p for p in s["players"]}
        self.assertFalse(names["E"]["alive"])   # 第 2 天被 A 刀杀
        self.assertFalse(names["F"]["alive"])   # 第 3 天被继任的 B 毒杀
        self.assertFalse(names["A"]["alive"])   # 第 3 天投票出局
        self.assertFalse(names["B"]["alive"])   # 第 4 天投票出局
        self.assertTrue(names["C"]["alive"])
        self.assertEqual(names["B"]["identity"], "杀手")  # 帮凶已继任
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("**A 的遗言**", log)          # 遗言先于身份公布
        self.assertIn("遗嘱：「万能钥匙」（A 遗留）→ D", log)
        self.assertIn("游戏结束：平民获胜", log)
        # v3.3 侦探竞选：C/D 参选、未参选者投票，C 当选（末置发言 + 1.5 票权）
        self.assertEqual(s["detective"], "C")
        self.assertIn("C 宣告参选侦探", log)
        self.assertIn("侦探竞选：C 当选侦探", log)
        # 侧写的调用确实发生（简报已存档）
        self.assertTrue((d / "inbox" / "D" / "day3-案件调查.md").exists())
        self.assertTrue((d / "inbox" / "A" / "day1-竞选声明.md").exists())
        # 案件台账：两起案件，凶手分别为 A 与 B
        self.assertEqual([(c["victim"], c["culprit"]) for c in s["cases"]], [("E", "A"), ("F", "B")])
        # script.md 全记录：秘密谋杀行动、房间内交谈、投票明细、道具分配结果均可见
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn('- A 声明 {"type": "murder", "target": "E", "weapon": "刀"} → 成功', script)
        self.assertIn('- B 声明 {"type": "murder", "target": "F", "weapon": "不明药剂"} → 成功', script)
        self.assertIn("书房内交谈", script)  # v3：案发前的同房交谈（书房）
        self.assertIn("- 投票明细：", script)
        self.assertIn("遗嘱：「万能钥匙」（A 遗留）→ D", script)
        self.assertIn("游戏结束：平民获胜", script)

    def test_briefing_key_records(self):
        """简报含"截至目前的关键记录"：公共事实流水 + 该席私密历史；不含发言与内心独白。"""
        d = write_game_dir(self.root, self.players())
        self.run_game(d, killer_script=True)
        b = (d / "inbox" / "C" / "day3-投票.md").read_text(encoding="utf-8")
        self.assertIn("## 截至目前的关键记录", b)
        self.assertIn("### 公共记录", b)
        self.assertIn("### 与你相关的私密记录", b)
        self.assertIn("投票结果", b)              # 公共记录含第 2 天投票结果
        self.assertIn("第2天投票记录：你弃权", b)  # 私密记录含本人历史投票
        section = b.split("## 截至目前的关键记录", 1)[1].split("## 你的私人笔记", 1)[0]
        self.assertNotIn("**", section)   # 发言行（**名字**：…）不进关键记录
        self.assertNotIn("……", section)   # mock 的 inner 固定为 "……"：内心独白不进简报

    def test_night_features_v3(self):
        """v3 夜间特性端到端：密道（建筑师帮凶夜用）、窃听器内容真实下发、守夜密谈、清晨状态行/状态表。"""
        players = self.players()
        players[1] = P("B", "帮凶", "建筑师", "不明药剂", gender="女", physique=2)  # 密道持有者
        d = write_game_dir(self.root, players)
        base = make_fake_chat(True)

        def chat_v3(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "开局准备" in title:
                acts = []
                if name == "B" and '"type": "set_passage"' in briefing:
                    acts = [{"type": "set_passage"}]
                if name == "F" and '"type": "place_bug"' in briefing:
                    acts = [{"type": "place_bug", "room": "书房"}]
                return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "晚间休息" in title and "夜袭" not in title and "密谈" not in title:
                acts = []
                if name == "C" and day == 1:
                    acts = [{"type": "invite", "target": "D"}]
                elif name == "D" and day == 1:
                    acts = [{"type": "accept_invite"}]
                elif name == "F" and day == 1:
                    acts = [{"type": "bug_listen"}]
                elif name == "B" and day == 2 and '"type": "use_passage"' in briefing:
                    acts = [{"type": "use_passage", "room": "书房"}]
                return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "密谈" in title:
                return ChatResult(player.name, ok=True,
                                  content=payload(speech=f"{name} 的密谈内容", notes="x"),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_v3)
        s = orch.state
        self.assertEqual(s["winner"], "平民")  # 主线剧本不受影响
        self.assertTrue(s["passage"]["used"])  # 密道第 2 夜成功使用
        self.assertNotIn("B", s["monitoring_true"]["第2天晚间休息"].get("书房", []))  # 监控无痕
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("密谈", script)
        self.assertIn("C 的密谈内容", script)
        self.assertIn("窃听器录音下发给 F（书房）", script)  # 内容真实下发（不再是空头支票）
        self.assertIn("状态一览（全剧透）", script)          # 每日清晨状态表
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("目前存活", log)                       # 清晨状态行
        self.assertIn("无杀倒计时：", log)
        self.assertIn("侦探：C", log)                        # v3.3：竞选产生的侦探列入清晨公告
        self.assertTrue(any("窃听器录音" in m for m in s["orch"]["hist"]["F"]))  # 私密历史留痕

    def test_hall_night_talk(self):
        """v3.5：夜宿会客室者（≥2 人）睡前围炉夜谈——内容仅当夜夜宿者知晓，其他席位收不到。"""
        d = write_game_dir(self.root, self.players())
        base = make_fake_chat(False)

        def chat_hall(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "晚间休息" in title and "夜谈" not in title and "夜袭" not in title:
                # 第 2 夜 E（万能钥匙开锁）与 F（随入）夜宿会客室
                acts = [{"type": "night_hall"}] if name in ("E", "F") and day == 2 else []
                return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "夜谈" in title:
                return ChatResult(player.name, ok=True,
                                  content=payload(speech=f"{name} 的夜谈内容", notes="x"),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_hall)
        s = orch.state
        self.assertEqual(s["winner"], "平民")  # 消极局照常倒计时收尾
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("- 会客室夜谈（E、F）：", script)
        self.assertIn("E 的夜谈内容", script)
        self.assertIn("F 的夜谈内容", script)
        # 仅当夜夜宿者可见：E/F 私密历史有夜谈记录，C（卧室过夜）没有
        self.assertTrue(any("会客室夜谈" in m for m in s["orch"]["hist"]["E"]))
        self.assertTrue(any("会客室夜谈" in m for m in s["orch"]["hist"]["F"]))
        self.assertFalse(any("夜谈" in m for m in s["orch"]["hist"].get("C", [])))
        # 夜宿名单清晨照常公开（夜谈不影响公开口径）
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("夜宿会客室公开：E、F", log)

    def test_blackout_bedroom_talk_with_eavesdropper(self):
        """v3.5 修订：停电黑暗中守夜密谈照常（摸黑也能谈）；杀手潜入满员卧室时旁听全部内容，
        密谈者不知情（密谈不取消，避免"取消"本身泄漏潜入）。"""
        d = write_game_dir(self.root, self.players())
        base = make_fake_chat(False)

        def chat_blackout(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "选房" in title and day == 2 and name == "B":
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "choose_room", "room": "会客室"},
                                                                      {"type": "declare_blackout"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "晚间休息" in title and "密谈" not in title and "夜袭" not in title:
                acts = []
                if name == "C" and day == 2:
                    acts = [{"type": "invite", "target": "D"}]
                elif name == "D" and day == 2:
                    acts = [{"type": "accept_invite"}]
                elif name == "A" and day == 2:
                    acts = [{"type": "night_raid", "room": "C的卧室"}]
                return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "密谈" in title:
                return ChatResult(player.name, ok=True,
                                  content=payload(speech=f"{name} 的密谈内容", notes="x"),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "夜袭" in title and name == "A" and '"type": "murder"' in briefing:
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "murder", "target": "C", "weapon": "刀"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_blackout)
        s = orch.state
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("C的卧室密谈（C、D）：【旁听：A】", script)
        self.assertIn("C 的密谈内容", script)
        # 密谈双方正常收到；杀手作为旁听者收到；密谈者的记录不泄漏"被旁听"
        self.assertTrue(any("C的卧室密谈" in m and "密谈内容" in m for m in s["orch"]["hist"]["C"]))
        self.assertTrue(any("听到了他们的密谈" in m for m in s["orch"]["hist"]["A"]))
        self.assertFalse(any("旁听" in m or "听到" in m for m in s["orch"]["hist"]["C"]))
        # 黑暗中守夜对（2 名非反派）仍被杀手袭击：C 的急救箱自动防下（未遂）
        self.assertTrue(s["attempts"])


class TestInterruptAndResume(MockGameTestBase):
    """席位粒度断点：API 失败 / Ctrl+C / 席位间隙停止都会落盘，续跑跳过已完成席位、重问被中断席位。

    默认阵容 order=A..F；第 1 天公共发言时段为侦探竞选（C/D 参选、C 当选），声明轮顺序即 A..F。
    """

    @staticmethod
    def _flaky_once(pred, fail):
        """包装健康策略：第一次命中 pred(name, title) 的调用按 fail 方式失败一次。"""
        base = make_fake_chat(False)
        state = {"fired": False}

        def wrapper(player, messages, *, on_token=None, reasoning_effort=None):
            title = messages[-1]["content"].splitlines()[0]
            if not state["fired"] and pred(player.name, title):
                state["fired"] = True
                return fail(player)
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        return wrapper

    def _run_expecting(self, d, chat_fn, exc_type):
        orch = Orchestrator(d)
        orig = orchestrator.chat
        orchestrator.chat = chat_fn
        try:
            with self.assertRaises(exc_type):
                orch.run_game()
        finally:
            orchestrator.chat = orig

    def test_api_failure_aborts_and_resume_recovers(self):
        d = write_game_dir(self.root, default_players())
        chat = self._flaky_once(
            lambda name, title: name == "C" and title.startswith("# 第1天 · 侦探竞选声明"),
            lambda player: ChatResult(player.name, ok=False, error="模拟 API 故障", latency=0.01),
        )
        self._run_expecting(d, chat, orchestrator.AbortRun)
        saved = json.loads((d / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["phase"], {"day": 1, "stage": "公共发言"})
        self.assertEqual(saved["orch"]["wip"]["1:公共发言:竞选"]["declared"], ["A", "B"])  # C 被中断时 A/B 已完成
        self.assertNotIn("C", saved["orch"]["cursor"])         # 被中断席位的 feed 游标未提交
        self.assertIn("模拟 API 故障", (d / "host_log.jsonl").read_text(encoding="utf-8"))
        orch2 = self.run_game(d, killer_script=False)  # 健康续跑直达结局
        self.assertEqual(orch2.state["winner"], "平民")
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertEqual(log.count("**A**：A 的第1天发言"), 1)  # 已完成席位不重问
        self.assertEqual(log.count("**C**：C 的第1天发言"), 1)  # 被中断席位重问且只成功一次

    def test_keyboard_interrupt_saves_and_resume(self):
        d = write_game_dir(self.root, default_players())

        def ki(player):
            raise KeyboardInterrupt

        chat = self._flaky_once(
            lambda name, title: name == "D" and title.startswith("# 第1天 · 侦探竞选声明"), ki)
        self._run_expecting(d, chat, KeyboardInterrupt)
        saved = json.loads((d / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["phase"], {"day": 1, "stage": "公共发言"})
        self.assertEqual(saved["orch"]["wip"]["1:公共发言:竞选"]["declared"], ["A", "B", "C"])  # D 被中断
        orch2 = self.run_game(d, killer_script=False)
        self.assertEqual(orch2.state["winner"], "平民")
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        for n in "ACD":
            self.assertEqual(log.count(f"**{n}**：{n} 的第1天发言"), 1)

    def test_soft_stop_mid_stage(self):
        """console pause/Ctrl+C 语义：当前席位行动完成后停下；continue = 同一实例再跑 run_game。"""
        d = write_game_dir(self.root, default_players())
        calls = {"n": 0}
        base = make_fake_chat(False)

        def counting(player, messages, *, on_token=None, reasoning_effort=None):
            calls["n"] += 1
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        # 第 1 天调用序：F 开局准备(1) → 竞选声明 A..F(2-7) → 退选 C,D(8-9) → 竞选投票 A,B,E,F(10-13)
        # → 交换 A(14)：第 14 次后停 = 交换环节中途
        orch = Orchestrator(d, hooks={"should_stop": lambda: calls["n"] >= 14})
        orig = orchestrator.chat
        orchestrator.chat = counting
        try:
            orch.run_game()
            self.assertIsNone(orch.state["winner"])
            saved = json.loads((d / "state.json").read_text(encoding="utf-8"))
            self.assertIsNone(saved["winner"])
            self.assertEqual(saved["phase"]["day"], 1)  # 第 1 天尚未跑完
            orch.hooks = {}   # console continue：清掉停止标记，同一实例直接再跑（进程内续跑）
            orch.run_game()
            self.assertEqual(orch.state["winner"], "平民")
        finally:
            orchestrator.chat = orig
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        for n in "ABCDEF":
            self.assertEqual(log.count(f"**{n}**：{n} 的第1天发言"), 1)  # 断点续跑无重复发言


if __name__ == "__main__":
    unittest.main()
