"""M5 编排器全流程集成测试：mock providers.chat，不打真实 API。

用脚本化全知策略驱动 run_game() 主循环，验证：
- 倒计时结局（全程消极：无谋杀行为、投票全弃权 → 第 4 天清晨平民胜，倒计时自第 2 天起算）
- 主神结局（第 4 天一次未遂重置倒计时后再无行为 → 拖至第 6 天终审投票，v3.15）
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
        """v3.15 主神死线=第 6 天终审投票后。倒计时联动：第 2 天未遂 + 第 4 天既遂维持倒计时不归零
        （若只在第 4 天出手，第 4 天清晨就已倒计时平民胜；只在第 3 天出手则第 6 天清晨平民胜）。"""
        d = write_game_dir(self.root, default_players())
        base = make_fake_chat(False)

        def chat_attempt(player, messages, *, on_token=None, reasoning_effort=None):
            title = messages[-1]["content"].splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if day in (2, 4) and "选房" in title and player.name in ("A", "D"):
                return ChatResult(player.name, ok=True,
                                  content=payload(actions=[{"type": "choose_room", "room": "书房"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if day in (2, 4) and "房间行动" in title and player.name == "A" and '"type": "murder"' in messages[-1]["content"]:
                # 第 2 天用刀（被敏捷闪避防下，未遂）；第 4 天刀已耗，徒手杀 D（A 魁梧 ≥ D 健壮，既遂）
                act = {"type": "murder", "target": "D"} | ({"weapon": "刀"} if day == 2 else {})
                return ChatResult(player.name, ok=True,
                                  content=payload(actions=[act]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_attempt)
        s = orch.state
        self.assertEqual(s["winner"], "主神")
        self.assertEqual(s["phase"], {"day": 6, "stage": "投票"})
        self.assertEqual(len(s["attempts"]), 1)   # 第 2 天未遂（敏捷闪避）
        self.assertEqual([c["victim"] for c in s["cases"]], ["D"])  # 第 4 天既遂
        self.assertFalse(next(p for p in s["players"] if p["name"] == "D")["alive"])
        self.assertTrue(all(p["alive"] for p in s["players"] if p["name"] != "D"))

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
        self.assertIn("── 行动回执", script)  # 行动回执块分隔头（v3.5 二跑复盘：行动记录独立成块）
        self.assertIn("◆ A｜谋杀 E（凶器：刀） → 成功", script)
        self.assertIn("◆ B｜谋杀 F（凶器：不明药剂） → 成功", script)
        self.assertIn("书房内交谈", script)  # v3：案发前的同房交谈（书房）
        self.assertIn("- 投票明细：", script)
        self.assertIn("遗嘱：「万能钥匙」（A 遗留）→ D", script)
        self.assertIn("游戏结束：平民获胜", script)

    def test_coverup_only_for_scene_villain(self):
        """v3.18：毁证窗口只通知身处案发现场的反派——场外帮凶不再收到毁证机会
        （也不再被即时告知"谋杀成功/案发房间/现场干净"）。"""
        d = write_game_dir(self.root, self.players())
        sj = d / "state.json"
        st = json.loads(sj.read_text(encoding="utf-8"))
        next(p for p in st["players"] if p["name"] == "A")["extra_items"] = ["裹尸袋"]  # 给杀手毁证手段
        sj.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        self.run_game(d, killer_script=True)
        self.assertTrue((d / "inbox" / "A" / "day2-毁证机会.md").exists())   # 杀手在案发现场（书房）
        self.assertFalse((d / "inbox" / "B" / "day2-毁证机会.md").exists())  # 帮凶在会客室，不通知

    def test_coverup_night_hint(self):
        """v3.19：白天案件当晚归宿选择时，持有毁证手段的反派收到"今晚可前往案发现场毁证、
        仅限案发当天"的提示（基于公开死亡公告+阵营自知，无额外泄漏）；无毁证手段者不提示。"""
        d = write_game_dir(self.root, self.players())
        sj = d / "state.json"
        st = json.loads(sj.read_text(encoding="utf-8"))
        next(p for p in st["players"] if p["name"] == "A")["extra_items"] = ["裹尸袋"]  # 给杀手毁证手段
        sj.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        self.run_game(d, killer_script=True)
        brief = (d / "inbox" / "A" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertIn("仅限案发当天", brief)  # A 第 2 天白天在书房杀 E → 当晚收到毁证提示
        self.assertIn("书房", brief)
        b_brief = (d / "inbox" / "B" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertNotIn("仅限案发当天", b_brief)  # B 无毁证手段（延时装置不算），不提示

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
            if "停电声明" in title and day == 2 and name == "B":
                # 停电声明轮：选房开始前逐一秘密询问反派（rules-6 §7.5 口径修正，game/2 复盘）
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "declare_blackout"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "选房" in title and day == 2 and name == "B":
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "choose_room", "room": "会客室"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "选房" in title and day == 2 and name == "C":
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "choose_room", "room": "会客室"},
                                                                      {"type": "invite", "target": "D"}]),
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
        self.assertIn("◆ B｜声明今天停电 → 成功", script)  # v3.5 复盘修复：停电声明记入全记录（行动回执格式）
        # game/2 复盘修复：声明轮先于选房（script 中声明回执位于"选房结果"之前）；停电公告在收尾后公开
        self.assertLess(script.index("声明今天停电"), script.index("- 选房结果：B→会客室"))
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("停电了——", log)                     # 进入房间后向众人公布
        self.assertNotIn("第2天自由活动", json.dumps(s["monitoring"], ensure_ascii=False))  # 停电整段监控零记录
        self.assertIn("C的卧室密谈（C、D）：【旁听：A】", script)
        self.assertIn("C 的密谈内容", script)
        # 密谈双方正常收到；杀手作为旁听者收到；密谈者的记录不泄漏"被旁听"
        self.assertTrue(any("C的卧室密谈" in m and "密谈内容" in m for m in s["orch"]["hist"]["C"]))
        self.assertTrue(any("听到了他们的密谈" in m for m in s["orch"]["hist"]["A"]))
        self.assertFalse(any("旁听" in m or "听到" in m for m in s["orch"]["hist"]["C"]))
        # v3.5 复盘修复：同入/守夜邀请成立对邀请者私密可见（索尔不知桑娜已到而放弃谋杀的教训）
        self.assertTrue(any("接受了你的同入邀请" in m for m in s["orch"]["hist"]["C"]))   # 白天选房邀请
        self.assertTrue(any("接受了你的守夜邀请" in m for m in s["orch"]["hist"]["C"]))   # 夜间守夜邀请
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


class StageHintsTest(MockGameTestBase):
    """简报增强：本环节规则速查块（明确数字/易误读规则）+ host_log 全量 usage。"""

    def setUp(self):
        super().setUp()
        self.orch = Orchestrator(write_game_dir(self.root, default_players()))

    def test_vote_hint_explicit_threshold(self):
        hints = self.orch._stage_hints("第2天 · 投票")  # 6 人存活
        self.assertTrue(any("至少 3.5 票" in h for h in hints), hints)
        self.assertTrue(any("3 票不够" in h for h in hints), hints)

    def test_vote_hint_threshold_scales_down(self):
        for p in self.orch.state["players"][4:]:
            p["alive"] = False  # 剩 4 人：2.5 即出局（game/1 豆包/杰米娜误算为需 3 票的事故）
        hints = self.orch._stage_hints("第3天 · 投票")
        self.assertTrue(any("至少 2.5 票" in h for h in hints), hints)
        self.assertTrue(any("2 票不够" in h for h in hints), hints)

    def test_election_hint_not_swallowed_by_vote(self):
        hints = self.orch._stage_hints("第1天 · 侦探竞选投票")
        self.assertTrue(any("未参选者" in h for h in hints), hints)

    def test_night_raid_hint(self):
        hints = self.orch._stage_hints("第2天 · 晚间休息（夜袭）")
        self.assertTrue(any("非反派" in h for h in hints), hints)
        self.assertTrue(any("不重置无杀倒计时" in h for h in hints), hints)

    def test_briefing_hints_before_menu(self):
        p = self.orch.player(self.orch.state["order"][0])
        text = self.orch.build_briefing(p, "第2天 · 投票", ['- {"type": "vote"}'])
        self.assertIn("## 本环节规则速查", text)
        self.assertLess(text.index("## 本环节规则速查"), text.index("## 本环节合法动作"))

    def test_briefing_no_hints_for_plain_stage(self):
        p = self.orch.player(self.orch.state["order"][0])
        text = self.orch.build_briefing(p, "第2天 · 自由活动（会客室内交谈）", [])
        self.assertIn("## 本环节规则速查", text)  # 内交谈有速查
        text2 = self.orch.build_briefing(p, "第2天 · 自由活动（非标准环节）", [])
        self.assertNotIn("## 本环节规则速查", text2)

    def test_host_log_full_usage(self):
        self.orch.host_log("A", "测试", True, 1.0, {"total_tokens": 100, "prompt_cache_hit_tokens": 80})
        line = json.loads((self.orch.dir / "host_log.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual(line["total_tokens"], 100)
        self.assertEqual(line["usage"]["prompt_cache_hit_tokens"], 80)


class TestBriefingAndRetryFixes(MockGameTestBase):
    """game/2 复盘修复：简报「你的状态」台账段 / 关键记录压缩 / 驳回即时重报（含三出口口径）。"""

    def setUp(self):
        super().setUp()
        self.orch = Orchestrator(write_game_dir(self.root, default_players()))

    def test_briefing_status_section_live_ledger(self):
        p = self.orch.player("A")  # 杀手/魔术师/刀
        text = self.orch.build_briefing(p, "第1天 · 公共发言", [])
        self.assertIn("## 你的状态", text)
        self.assertIn("身份：杀手｜职业：魔术师", text)
        self.assertIn("刀×1", text)
        self.assertIn("看破伪装×2", text)
        p["items"].clear()  # 消耗后立即反映（不依赖 AI 自己的笔记）
        text = self.orch.build_briefing(p, "第1天 · 公共发言", [])
        self.assertIn("道具：无", text)

    def test_compact_public_election_and_status_lines(self):
        lines = ["A 宣告参选侦探", "B 宣告不参选", "退选结果：无人退选；留任候选人：A",
                 "- 侦探竞选：A 当选侦探（略）；侦探佩枪：桌上凶器「左轮手枪」归 A 持有",
                 "- 第 2/6 天开始。今日行动顺序：B → A", "- 目前存活 6 人：A、B",
                 "- 无杀倒计时：已连续 0 个完整天无谋杀行为", "- 投票结果：无人得票",
                 "- 第 3/6 天开始。今日行动顺序：A → B", "- 目前存活 5 人：A、B",
                 "- 无杀倒计时：已连续 1 个完整天无谋杀行为"]
        out = Orchestrator._compact_public(lines)
        self.assertTrue(any("侦探竞选：" in x for x in out))                 # 结果行保留
        self.assertFalse(any("宣告参选" in x or "宣告不参选" in x or "退选结果" in x for x in out))  # 仪式行折叠
        self.assertEqual([x for x in out if "/6 天开始" in x], ["- 第 3/6 天开始。今日行动顺序：A → B"])
        self.assertEqual([x for x in out if "目前存活" in x], ["- 目前存活 5 人：A、B"])
        self.assertEqual([x for x in out if "无杀倒计时：已连续" in x], ["- 无杀倒计时：已连续 1 个完整天无谋杀行为"])
        self.assertTrue(any("投票结果" in x for x in out))                   # 事件行全保留

    def test_compact_private_keeps_latest_monitoring_dump(self):
        lines = ["你用暴力破解打开了监控室",
                 "监控记录（公开口径，今日新增置顶）：今日新增：第1天自由活动[监控室←A]",
                 "你收到了不明药剂（剩余 1 次）",
                 "监控记录（公开口径，今日新增置顶）：今日新增：第2天自由活动[书房←B]；历史：第1天自由活动[监控室←A]"]
        out = Orchestrator._compact_private(lines)
        dumps = [x for x in out if x.startswith("监控记录（公开口径")]
        self.assertEqual(len(dumps), 1)
        self.assertIn("第2天", dumps[0])          # 留最新一条（格式本身累计历史）
        self.assertTrue(any("暴力破解" in x for x in out))   # 其余事件全保留
        self.assertTrue(any("不明药剂" in x for x in out))

    def test_night_murder_rejection_then_retry(self):
        """夜袭误报已消耗凶器 → 驳回后当场私下重报一次 → 改报空手落杀（重报独立存档/日志/host 全记录）。"""
        d = write_game_dir(self.root / "g2", default_players())
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            acts = None
            if "夜袭" in title and name == "A" and day == 3:
                if "被驳回" in briefing:  # 驳回重报的第二次调用：改报空手
                    acts = [{"type": "murder", "target": "F"}]
                else:                   # 第一次：误报已在第 2 天消耗掉的刀
                    acts = [{"type": "murder", "target": "F", "weapon": "刀"}]
            elif "晚间休息" in title and "密谈" not in title and name == "A" and day == 3:
                acts = [{"type": "night_raid", "room": "F的卧室"}]
            elif "选房" in title and day == 2 and name == "A":
                acts = [{"type": "choose_room", "room": "书房"}]
            elif "选房" in title and day == 2 and name == "E":
                acts = [{"type": "choose_room", "room": "书房"}]
            elif "房间行动" in title and day == 2 and name == "A":
                acts = [{"type": "murder", "target": "E", "weapon": "刀"}]  # 刀在此消耗
            if acts is None:
                return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)
            return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
                              latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")

        orch = self.run_game_with(d, chat_fn)
        s = orch.state
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("驳回（未持有可用凶器）", script)          # 驳回入 host 全记录
        self.assertTrue(any(c["victim"] == "F" for c in s["cases"]))  # 重报后落杀成功
        host_log = (d / "host_log.jsonl").read_text(encoding="utf-8")
        self.assertIn("晚间休息-夜袭#驳回重报", host_log)        # host_log 独立条目
        self.assertTrue((d / "outbox" / "A" / "day3-晚间休息-夜袭#驳回重报.md").exists())  # 重报独立存档
        self.assertTrue(any("你的动作被驳回：未持有可用凶器" in m for m in s["orch"]["hist"]["A"]))
        # 简报台账与压缩：A 第 3 天晚间简报——你的状态段实时道具（刀没了、缴获的万能钥匙在）；
        # 公共记录折叠竞选仪式行、保留结果行
        briefing = (d / "inbox" / "A" / "day3-晚间休息.md").read_text(encoding="utf-8")
        self.assertIn("## 你的状态", briefing)
        self.assertIn("万能钥匙×2", briefing)
        self.assertNotIn("刀×1", briefing)
        pub = briefing.split("### 公共记录")[1].split("### 与你相关的私密记录")[0]
        self.assertIn("侦探竞选：", pub)
        self.assertNotIn("宣告参选", pub)
        self.assertEqual(pub.count("/6 天开始"), 1)

    def test_night_raid_menu_requires_means_or_blackout(self):
        """夜袭菜单门槛：无开锁手段且非停电时不提供该项（菜单只列合法动作——game/2 复盘同源的菜单准确性）。"""
        # A=魔术师（无任何开锁手段）：整局夜间菜单无夜袭项
        d1 = write_game_dir(self.root / "g4", default_players())
        self.run_game(d1, killer_script=False)
        night2 = (d1 / "inbox" / "A" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertNotIn("night_raid", night2)

        # A=黑客（暴力破解）：第 2 天起有夜袭项；第 1 天（全天禁杀）没有
        players = default_players()
        players[0] = P("A", "杀手", "黑客", "刀", gender="男", physique=4)
        d2 = write_game_dir(self.root / "g5", players)
        self.run_game(d2, killer_script=False)
        night1 = (d2 / "inbox" / "A" / "day1-晚间休息.md").read_text(encoding="utf-8")
        self.assertNotIn("night_raid", night1)
        night2 = (d2 / "inbox" / "A" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertIn("night_raid", night2)

        # 停电中（B 第 2 天声明）：门锁失效，无手段的杀手也显示夜袭项
        d3 = write_game_dir(self.root / "g6", default_players())
        base = make_fake_chat(False)

        def chat_bo(player, messages, *, on_token=None, reasoning_effort=None):
            title = messages[-1]["content"].splitlines()[0]
            if "停电声明" in title and player.name == "B" and "第2天" in title:
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "declare_blackout"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        self.run_game_with(d3, chat_bo)
        night2 = (d3 / "inbox" / "A" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertIn("night_raid", night2)

    def test_lit_breakin_interrupts_sleep_talk(self):
        """v3.7 内应口径：帮凶邀平民守夜 + 杀手夜袭破门——亮房闯入被看见，守夜密谈中断，深夜照常判定。"""
        players = default_players()
        players[0] = P("A", "杀手", "黑客", "刀", gender="男", physique=4)
        d = write_game_dir(self.root / "g3", players)
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            acts = None
            if "夜袭" in title and name == "A" and day == 2:
                acts = [{"type": "murder", "target": "F", "weapon": "刀"}]
            elif "晚间休息" in title and "密谈" not in title and day == 2:
                if name == "B":
                    acts = [{"type": "invite", "target": "F"}]   # 帮凶邀平民守夜（请君入瓮）
                elif name == "F":
                    acts = [{"type": "accept_invite"}]
                elif name == "A":
                    acts = [{"type": "night_raid", "room": "B的卧室"}]
                else:
                    acts = []
            if acts is None:
                return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)
            return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
                              latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")

        orch = self.run_game_with(d, chat_fn)
        s = orch.state
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("密谈中断", script)                    # 亮房闯入打断密谈
        self.assertNotIn("B的卧室密谈（B、F）：", script)     # 密谈没有发生
        self.assertTrue(any("闯入了你们的卧室" in m for m in s["orch"]["hist"]["F"]))  # 守夜两人看清来人
        self.assertTrue(any("都看见了你" in m for m in s["orch"]["hist"]["A"]))
        self.assertTrue(any(c["victim"] == "F" and c["room"] == "B的卧室" for c in s["cases"]))
        self.assertFalse(next(p for p in s["players"] if p["name"] == "F")["alive"])


class TestSpeechRouting(MockGameTestBase):
    """v3.8 附言公开：交换/受赠回应/竞选退选/竞选投票/武装指认/死者字条；私密环节简报明示不发言。"""

    def test_election_remarks_published(self):
        """密封退选与竞选投票的附言随结果/开票一齐公开。"""
        d = write_game_dir(self.root, default_players())
        self.run_game(d, killer_script=False)  # C/D 参选并留任，A/B/E/F 投 C
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("（退选揭晓）", log)
        self.assertIn("（竞选投票）", log)

    def test_exchange_speech_public(self):
        """交换附言与受赠回应公开可见（此前 speech 被静默丢弃）。"""
        d = write_game_dir(self.root / "g7", default_players())
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            title = messages[-1]["content"].splitlines()[0]
            if "交换环节" in title and "赠送要约" not in title and "第1天" in title and name == "C":
                return ChatResult(player.name, ok=True,
                                  content=payload(speech="D，这个急救箱给你防身。", notes="x",
                                                  actions=[{"type": "gift", "to": "D", "item": "急救箱"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "赠送要约" in title and name == "D":
                return ChatResult(player.name, ok=True,
                                  content=payload(speech="谢了，我会记住的。", notes="x",
                                                  actions=[{"type": "accept"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        self.run_game_with(d, chat_fn)
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("**C**（交换环节）：D，这个急救箱给你防身。", log)
        self.assertIn("**D**（回应 C）：谢了，我会记住的。", log)
        self.assertIn("把一件道具交给了 D", log)  # 公开口径仍不含道具名

    def test_armed_accusation_declaration_public(self):
        """武装指认宣言公开，且先于指认结果上屏。"""
        players = default_players()
        players[3] = P("D", "平民", "运动员", "棒球棍", gender="男", physique=3)  # 持武器平民
        d = write_game_dir(self.root / "g8", players)
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            title = messages[-1]["content"].splitlines()[0]
            if "竞选声明" in title:  # 全员不参选 → 本局不设侦探（无垄断，持械平民可指认）
                return ChatResult(player.name, ok=True,
                                  content=payload(speech="我不参选。", notes="x",
                                                  actions=[{"type": "declare", "run": False}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "武装指认" in title and name == "D":
                return ChatResult(player.name, ok=True,
                                  content=payload(speech="我看穿你了，A——你就是杀手。", notes="x",
                                                  actions=[{"type": "armed_accuse", "target": "A"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_fn)
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("**D**（武装指认宣言）：我看穿你了，A——你就是杀手。", log)
        self.assertEqual(orch.state["winner"], "平民")  # 指认正确（A 是杀手）→ 平民立即获胜
        self.assertLess(log.index("武装指认宣言"), log.index("指认正确"))  # 宣言先于裁决结果公开

    def test_bequest_note_speech_public(self):
        """死者字条附言公开（被谋杀者的留言不再是黑洞）。"""
        d = write_game_dir(self.root / "g9", default_players())
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            title = messages[-1]["content"].splitlines()[0]
            acts = None
            speech = None
            if "选房" in title and "第2天" in title and name in ("A", "E", "F"):
                acts = [{"type": "choose_room", "room": "地下室"}]  # 黑暗房 ≤2 非反派可杀；F 目击→不缴获→封存
            elif "房间行动" in title and "第2天" in title and name == "A":
                acts = [{"type": "murder", "target": "E", "weapon": "刀"}]
            elif "死者字条" in title and name == "E":
                speech = "黑暗里我听见电线滋滋作响……万能钥匙给 D，替我查下去。"
                acts = [{"type": "bequeath", "mapping": {"万能钥匙": "D"}}]
            if acts is None and speech is None:
                return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)
            return ChatResult(player.name, ok=True,
                              content=payload(speech=speech or "", notes="x", actions=acts or []),
                              latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")

        self.run_game_with(d, chat_fn)
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("**E 的死者字条**：黑暗里我听见电线滋滋作响", log)
        self.assertIn("万能钥匙", log)  # 遗嘱归属公开

    def test_secret_phases_told_not_to_speak(self):
        """纯私密环节简报明示不发言（投票/归宿），避免 AI 对着虚空白写。"""
        d = write_game_dir(self.root / "g10", default_players())
        self.run_game(d, killer_script=False)
        vote_briefing = (d / "inbox" / "A" / "day2-投票.md").read_text(encoding="utf-8")
        self.assertIn("本环节不发言", vote_briefing)
        night_briefing = (d / "inbox" / "A" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertIn("本环节不发言", night_briefing)


class TestAccompliceStageMagic(MockGameTestBase):
    """v3.9 修复：舞台魔术是反派技能——帮凶魔术师的自由活动菜单必须包含舞台魔术，且声明后真正击杀。"""

    def test_accomplice_magician_stage_magic(self):
        players = default_players()
        players[1] = P("B", "帮凶", "魔术师", "不明药剂", gender="女", physique=2)
        d = write_game_dir(self.root / "g11", players)
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if day == 2 and "选房" in title and name in ("B", "E"):
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "choose_room", "room": "书房"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if day == 2 and "房间行动" in title and name == "B" and '"type": "stage_magic"' in briefing:
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "stage_magic", "target": "E"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_fn)
        s = orch.state
        # 菜单证据：帮凶魔术师的房间行动简报含舞台魔术项，且不含杀手专属的谋杀项
        inbox = (d / "inbox" / "B" / "day2-自由活动-房间行动.md").read_text(encoding="utf-8")
        self.assertIn('"type": "stage_magic"', inbox)
        self.assertNotIn('"type": "murder"', inbox)
        # 击杀落账：E 死亡，案件方法=舞台魔术，凶手=帮凶 B 本人（独处现场，E 的万能钥匙被 B 缴获）
        self.assertTrue(any(c["victim"] == "E" and c["method"] == "舞台魔术" and c["culprit"] == "B"
                            for c in s["cases"]))
        self.assertFalse(next(p for p in s["players"] if p["name"] == "E")["alive"])
        bp = next(p for p in s["players"] if p["name"] == "B")
        self.assertTrue(any(e["name"] == "万能钥匙" for e in bp["items"]))


class TestGameStateSection(MockGameTestBase):
    """v3.10：简报新增「游戏状态」段——公开事实速查（角色表/存活/侦探/倒计时），不含声称信息。"""

    def test_briefing_game_state(self):
        d = write_game_dir(self.root / "g12", default_players())
        orch = Orchestrator(d)
        p = next(q for q in orch.state["players"] if q["name"] == "C")
        b = orch.build_briefing(p, "第1天 · 公共发言", [], "")
        self.assertIn("## 游戏状态", b)
        self.assertIn("第 1/6 天", b)
        self.assertIn("存活 6 人", b)
        self.assertIn("侦探：无", b)
        self.assertIn("无杀倒计时：已连续 0 个完整天", b)
        self.assertIn("A（男·魁梧·中国·存活）", b)  # 体格转文字，不含数值（硬性约定）

        # 出局者身份职业公开（§7.3）；被谋杀者只公开死亡信息与已公开职业，身份不公开（§9.4）
        s = orch.state
        s["phase"] = {"day": 3, "stage": "公共发言"}
        s["players"][2]["alive"] = False  # C 第 2 天被投票出局
        s["orch"]["out_day"] = {"C": 2}
        s["profession_public"] = ["C", "D"]
        s["players"][3]["alive"] = False  # D 第 2 天被谋杀
        s["cases"] = [{"id": 1, "victim": "D", "day": 2, "room": "书房"}]
        s["last_murder_day"] = 2
        s["detective"] = "E"
        b2 = orch.build_briefing(p, "第3天 · 公共发言", [], "")
        self.assertIn("第 3/6 天", b2)
        self.assertIn("存活 4 人", b2)
        self.assertIn("侦探：E", b2)
        self.assertIn("已连续 0 个完整天", b2)  # 第 2 天有谋杀行为，倒计时清零
        self.assertIn("C（女·健壮·中国·第2天出局：平民/医师）", b2)
        seg_d = next(x for x in b2.split("、") if x.startswith("D（"))
        self.assertIn("第2天死亡（书房）", seg_d)
        self.assertIn("职业已公开：运动员", seg_d)
        self.assertNotIn("平民", seg_d)  # 被谋杀者身份不公开


class TestCaseIdRobustness(MockGameTestBase):
    """game/18 复盘：AI 自造 case_id（如 "格罗克遇袭"）不得炸穿编排器；无案件清晨调查阶段整体跳过。"""

    def test_non_integer_case_id_rejected_not_raised(self):
        d = write_game_dir(self.root / "g13", default_players())
        orch = Orchestrator(d)
        p = next(q for q in orch.state["players"] if q["name"] == "C")
        ev = orch.dispatch(p, {"type": "investigate", "case_id": "格罗克遇袭", "choice": "武器伤痕"})
        self.assertFalse(ev["ok"])
        self.assertIn("案件编号", ev["error"])
        ev2 = orch.dispatch(p, {"type": "profile", "case_id": None})
        self.assertFalse(ev2["ok"])

    def test_investigation_skipped_without_cases(self):
        """只有未遂、没有案件的清晨：无可调查对象，调查阶段不调用任何 AI。"""
        d = write_game_dir(self.root / "g14", default_players())
        orch = Orchestrator(d)
        s = orch.state
        s["phase"] = {"day": 3, "stage": "案件调查"}
        s["attempts"].append({"target": "E", "day": 2, "stage": "晚间休息", "room": "会客室", "period": "第2天晚间休息"})
        calls = []
        orig = orchestrator.chat
        orchestrator.chat = lambda *a, **k: calls.append((a, k))
        try:
            orch.run_investigation()
        finally:
            orchestrator.chat = orig
        self.assertEqual(calls, [])


class TestBlackoutDiscovery(MockGameTestBase):
    """v3.11：停电逐人发现——每位玩家在抵达所选房间时各自收到发现回执；选房进入情况
    不再标注锁状态（正常天与停电天的选房简报逐字一致，后续选房者无法提前推断停电）。"""

    def test_blackout_per_person_discovery(self):
        d = write_game_dir(self.root, default_players())
        base = make_fake_chat(False)
        briefings = []
        locks_snap = {}
        holder = {}

        def on_stage(title):
            if "orch" in holder:
                locks_snap[title] = dict(holder["orch"].state["locks"])

        def chat_blackout(player, messages, *, on_token=None, reasoning_effort=None):
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "选房" in title and day == 2:
                briefings.append((player.name, briefing))
            if "停电声明" in title and day == 2 and player.name == "B":
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "declare_blackout"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = Orchestrator(d, hooks={"on_stage": on_stage})
        holder["orch"] = orch
        orig = orchestrator.chat
        orchestrator.chat = chat_blackout
        try:
            orch.run_game()
        finally:
            orchestrator.chat = orig
        s = orch.state
        # 每位存活玩家在第 2 天选房时收到逐人发现回执（含留在会客室者）
        for n in ("A", "B", "C", "D", "E", "F"):
            self.assertTrue(any("停电了" in msg and "门锁失效" in msg for msg in s["orch"]["hist"][n]), n)
        # 选房简报不再出现锁状态标注（进入情况只报占用）
        self.assertTrue(any("各房间进入情况" in b for _, b in briefings))
        for name, b in briefings:
            self.assertNotIn("门仍上锁", b, name)
        # 停电不动物理锁台账：第 2 天选房结束（进入晚间休息）时监控室仍记为上锁，供电恢复后回到原状态
        night2 = next(v for t, v in locks_snap.items() if "第2天" in t and "晚间休息" in t)
        self.assertTrue(night2["监控室"])

    def test_permanently_opened_doors_in_game_state(self):
        """E（万能钥匙）第 2 天首开监控室后，「游戏状态」段列出「已永久开启」——跨天锁台账
        不再依赖 AI 自己记笔记（替代被删除的选房锁标注）。"""
        d = write_game_dir(self.root, default_players())
        base = make_fake_chat(False)
        briefings = []

        def chat_open(player, messages, *, on_token=None, reasoning_effort=None):
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "选房" in title:
                briefings.append((day, briefing))
                if day == 2 and player.name == "E":
                    return ChatResult(player.name, ok=True,
                                      content=payload(notes="x", actions=[{"type": "choose_room", "room": "监控室"}]),
                                      latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_open)
        s = orch.state
        self.assertFalse(s["locks"]["监控室"])  # 首开即永久开启（rules-6 §3.1）
        day1 = [b for day, b in briefings if day == 1]
        later = [b for day, b in briefings if day >= 3]
        self.assertTrue(day1 and later)
        self.assertTrue(all("已永久开启：（暂无" in b for b in day1))
        self.assertTrue(all("已永久开启：监控室的门" in b for b in later))


class TestDarkWhisper(MockGameTestBase):
    """v3.17：黑暗房间中"同入邀请成立的组合"（互知在场）可摸黑低语（rules-6 §7.5.6/§9.5）——
    低语全体在场者都能听到（含组合看不见的潜伏者），低语者不会知道是否被旁听。"""

    def test_dark_whisper_invited_pair_with_lurker(self):
        d = write_game_dir(self.root, default_players())
        base = make_fake_chat(False)

        def chat_whisper(player, messages, *, on_token=None, reasoning_effort=None):
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            name = player.name
            if day == 2 and "停电声明" in title and name == "B":
                return ChatResult(name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "declare_blackout"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if day == 2 and "选房" in title:
                acts = [{"type": "choose_room", "room": "会客室"}]
                if name == "C":
                    acts = [{"type": "choose_room", "room": "书房"}, {"type": "invite", "target": "D"}]
                elif name in ("D", "E"):
                    acts = [{"type": "choose_room", "room": "书房"}]
                return ChatResult(name, ok=True, content=payload(notes="x", actions=acts),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        seen = {}

        def on_stage(title):
            if "第2天" in title and "晚间休息" in title:
                seen["invites"] = dict(orch.state["orch"].get("free_invites") or {})

        orch = Orchestrator(d, hooks={"on_stage": on_stage})
        orig = orchestrator.chat
        orchestrator.chat = chat_whisper
        try:
            orch.run_game()
        finally:
            orchestrator.chat = orig
        s = orch.state
        # 第 2 天 B 拉闸停电：C 邀 D 同入书房成立（互知在场）→ 二人摸黑低语；E 未受邀独立进入 → 黑暗中旁听
        self.assertEqual(seen["invites"], {"D": "C"})
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("### 书房内低语（黑暗）（C、D）【旁听：E】", script)
        c_log = (d / "private" / "C" / "log.md").read_text(encoding="utf-8")
        d_log = (d / "private" / "D" / "log.md").read_text(encoding="utf-8")
        e_log = (d / "private" / "E" / "log.md").read_text(encoding="utf-8")
        self.assertIn("C 的第2天发言", c_log)
        self.assertIn("D 的第2天发言", c_log)
        self.assertNotIn("E 的第2天发言", c_log)  # E 不是低语组合成员，没有发言机会
        self.assertIn("书房内低语（黑暗）", d_log)
        self.assertIn("听到有人低语", e_log)        # 潜伏者的私密存档
        self.assertIn("C 的第2天发言", e_log)
        self.assertTrue(any("听到有人低声交谈" in m for m in s["orch"]["hist"]["E"]))  # 潜伏者回执得知全部内容
        self.assertNotIn("旁听", c_log)              # 低语者不会被告知是否有人旁听
        # E 不是发言者：没有收到低语环节简报；C 收到了，且简报明示摸黑低语口径
        self.assertFalse((d / "inbox" / "E" / "day2-自由活动-交谈-书房.md").exists())
        c_brief = (d / "inbox" / "C" / "day2-自由活动-交谈-书房.md").read_text(encoding="utf-8")
        self.assertIn("摸黑低声交谈", c_brief)
        # 黑暗中的低语同样进入窃听器录音源（麦克风不受黑暗影响）
        self.assertTrue(s["orch"]["talks"].get("2:书房"))


class TestLiteJustInTimeRules(MockGameTestBase):
    """lite 规则集：AI 的规则书没有机制章节——凡 full 局指向"规则书 §X"的引用，
    lite 局改为把对应规则临场内联发给 AI（just-in-time），不产生悬空引用。"""

    def test_lite_inline_rules(self):
        d = write_game_dir(self.root, default_players())
        orch = Orchestrator(d)
        # full：保持对规则书的引用
        self.assertTrue(any("见规则书 §7.3" in h for h in orch._stage_hints("第2天 · 投票")))
        self.assertIn("见规则 §7.6", orch._weapon_hint(orch.player("C"), None))  # C 无凶器 → 空手提示
        # lite：临场内联，无章节引用
        orch.state["is_lite_rule"] = True
        hints = "、".join(orch._stage_hints("第2天 · 投票"))
        self.assertNotIn("§", hints)
        self.assertIn("指对平民立即获胜", hints)  # 武装指认内联口径
        w = orch._weapon_hint(orch.player("C"), None)
        self.assertNotIn("§", w)
        self.assertIn("你体格不弱于目标", w)  # 空手条件内联

    def test_lite_game_no_dangling_refs(self):
        """lite 全流程 mock 局：全部 inbox 简报不得出现任何规则书章节引用（§）。"""
        d = write_game_dir(self.root, default_players())
        sj = d / "state.json"
        st = json.loads(sj.read_text(encoding="utf-8"))
        st["is_lite_rule"] = True
        sj.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        self.run_game(d, killer_script=True)
        inboxes = list(d.glob("inbox/*/*.md"))
        self.assertTrue(inboxes)
        for f in inboxes:
            self.assertNotIn("§", f.read_text(encoding="utf-8"), f.name)


class TestScriptFmt(MockGameTestBase):
    """script.md / console 显示修复：开局公告不重复；自由活动按房间分段；
    回执（发给 AI 的第二人称文本）在 host 侧转第三人称；console 重大结果行带分割线。"""

    def test_no_duplicate_announcement(self):
        d = write_game_dir(self.root, default_players())
        (d / "public" / "announcement.md").write_text(
            "# 开局公告\n\n欢迎来到暴风夜。\n规则书就放在大厅的桌上，自己读。\n—— 主人\n", encoding="utf-8")
        self.run_game(d, killer_script=False)
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertEqual(script.count("规则书就放在大厅的桌上"), 1)  # 只在开头「开局公告」节出现一次
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("规则书就放在大厅的桌上", log)  # 公开口径（feed/public）不变

    def test_room_sections_and_third_person(self):
        d = write_game_dir(self.root, default_players())
        self.run_game(d, killer_script=True)
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("### 选房", script)                      # 自由活动的环节子段
        self.assertIn("### 书房内交谈（", script)              # 房内交谈按房间分段
        self.assertIn("### 书房 · 房间行动（", script)         # 房间行动按房间分段
        self.assertNotIn("｜你用", script)                     # 回执不再沿用第二人称
        self.assertNotIn("结果：你", script)

    def test_third_person_unit(self):
        from src.fmt import third_person
        self.assertEqual(third_person("克劳德", "你用螺丝起子打开了监控室"),
                         "克劳德用螺丝起子打开了监控室")
        self.assertEqual(third_person("狄西", "杰普特 接受了你的同入邀请，你也知道 TA 来了"),
                         "杰普特 接受了狄西的同入邀请，狄西也知道 TA 来了")
        # 含对话引用（：）只转换开头，引文内的"你"不动
        self.assertEqual(third_person("A", "你在黑暗中潜入，听到密谈：C：你还好吗"),
                         "A在黑暗中潜入，听到密谈：C：你还好吗")
        self.assertEqual(third_person("A", "监控记录（公开口径）：无"), "监控记录（公开口径）：无")

    def test_console_result_dividers(self):
        import contextlib
        import io
        from src.console import Ctx, make_hooks
        hooks = make_hooks(Ctx(None, None))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks["on_public"]("投票结果：A 3票——A 过半数出局")
            hooks["on_public"]("夜宿会客室公开：A、B")  # 普通公告不带分割线
            hooks["on_private"]("B", "你用万能钥匙拉开了电闸")
        out = buf.getvalue()
        self.assertEqual(out.count("─" * 40), 2)          # 只有投票结果带前后分割线
        self.assertIn("B用万能钥匙拉开了电闸", out)          # 私密回执转第三人称


if __name__ == "__main__":
    unittest.main()
