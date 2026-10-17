"""M5 编排器全流程集成测试：mock providers.chat，不打真实 API。

用脚本化全知策略驱动 run_game() 主循环，验证：
- 倒计时结局（全程消极：无谋杀行为、投票全弃权 → 第 3 天清晨平民胜，v4.0 倒计时从第 1 天起算）
- 主神结局（第 4 天一次未遂重置倒计时后再无行为 → 拖至第 6 天终审投票，v3.15）
- 平民胜结局（杀手 A 第 2 天谋杀 → 第 3 天投票出局 → 另一名杀手 B 第 3 天再杀 → 第 4 天被投出）
- 阶段边界停止 + 重建 Orchestrator 续跑至结局（--resume 语义）
- 归档产物：public/log.md 结局行、private 内心独白、inbox/outbox 存档
- v4.0：反派密谋频道（每天两次）、谋杀窗口顺延与每日封顶、清晨调查队列（平民自动/反派三选一）

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
    """脚本化全知策略。killer_script=False：全程消极（主神结局）；True：A/B 两名杀手依次作案、平民第 3 天起投反派。"""

    def fake_chat(player, messages, *, on_token=None, reasoning_effort=None):
        name = player.name
        briefing = messages[-1]["content"]
        title = briefing.splitlines()[0]  # 只在标题行上分派：正文里的历史公告含"投票结果"等字样，会误判
        m = re.search(r"第(\d+)天", title)
        day = int(m.group(1)) if m else 1
        speech = f"{name} 的第{day}天发言"
        acts: list[dict] = []
        if "密谋" in title:
            speech = f"{name} 的第{day}天密谋"  # v4.0 密谋频道：speech 即密谋发言（不进公开日志）
        elif "开局准备" in title:
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
            # v4.0：平民持有者自动结算（不调 API）；反派持有者私密三选一——mock 一律正常调查
            if '"type": "investigate"' in briefing and '"type": "tamper_investigation"' in briefing:
                t = re.search(r'"case": "([^"]+)"', briefing)
                acts = [{"type": "investigate", "case": t.group(1)}] if t else []
        elif "武装指认" in title:
            pass
        elif "投票" in title:
            if killer_script and day >= 3 and name not in ("A", "B"):
                acts = [{"type": "vote", "target": "A" if day == 3 else "B"}]
            else:
                acts = [{"type": "abstain"}]
        elif "开锁行动轮" in title:
            unlock = re.search(r'\{"type": "unlock", "room": "([^"]+)"\}', briefing)
            acts = [{"type": "unlock", "room": unlock.group(1)}] if unlock else []
        elif "选房" in title:
            room = "会客室"  # v3：白天卧室清场，默认苟大厅
            if killer_script and day == 2 and name in ("A", "E"):
                room = "书房"      # A 第 2 天在书房杀 E（E 无自卫手段）
            if killer_script and day >= 3 and name in ("B", "F"):
                room = "书房"      # B 第 3 天在书房杀 F（v3.2：监控室上锁，改走书房）
            acts = [{"type": "choose_room", "room": room}]
        elif "内交谈" in title:
            pass  # 只发言
        elif "房间行动" in title or "谋杀窗口" in title:
            if killer_script and day >= 2 and '"type": "murder"' in briefing and name in ("A", "B"):
                t = re.search(r'"target": "([^"|]+)"', briefing)
                if t:
                    acts = [{"type": "murder", "target": t.group(1), "weapon": "刀" if name == "A" else "不明药剂"}]
        elif "晚间休息" in title or "毁证机会" in title:
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
        """全程消极（无谋杀行为、投票全弃权）：v4.0 倒计时从第 1 天起算，第 3 天清晨平民胜。"""
        d = write_game_dir(self.root, default_players())
        orch = self.run_game(d, killer_script=False)
        s = orch.state
        self.assertEqual(s["winner"], "平民")
        self.assertIn("无杀倒计时", s["win_reason"])
        self.assertEqual(s["phase"], {"day": 3, "stage": "案件调查"})
        self.assertTrue(all(p["alive"] for p in s["players"]))  # 全程无死无伤
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("游戏结束：平民获胜——无杀倒计时完成", log)
        self.assertIn("投票结果：无人得票", log)
        self.assertIn("无人过半数", log)  # 投票结果附明确裁决（是否有人出局）
        # 归档：内心独白 / 简报与回执 / host_log
        self.assertTrue((d / "private" / "A" / "inner.md").exists())
        self.assertTrue((d / "outbox" / "A" / "day1-竞选声明.md").exists())  # v3.3：第 1 天公共发言改为侦探竞选
        self.assertFalse((d / "inbox" / "F" / "day1-开局准备.md").exists())  # 窃听器改为后续到场放置
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
            if day in (2, 4) and "谋杀窗口" in title and player.name == "A" and '"type": "murder"' in messages[-1]["content"]:
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
        # 职业不重复；B（机械师的【暴力破解】×2）有开锁手段用于潜入他人卧室；
        # 受害者 E/F 均无自卫手段（C 的急救箱、D 的闪避都不在他们身上）。
        return [
            P("A", "杀手", "黑客", "刀", gender="男", physique=4),
            P("B", "杀手", "机械师", "不明药剂", gender="女", physique=2),  # v4.0 双杀手制
            P("C", "平民", "医师", "急救箱", gender="女", physique=3),
            P("D", "平民", "运动员", "手电筒", gender="男", physique=3),
            P("E", "平民", "建筑师", "万能钥匙", gender="男", physique=2),
            P("F", "平民", "心理医生", "窃听器", gender="女", physique=2),
        ]

    def test_killer_voted_out_then_second_killer_voted_out(self):
        players = self.players()
        d = write_game_dir(self.root, players)
        orch = self.run_game(d, killer_script=True)
        s = orch.state
        self.assertEqual(s["winner"], "平民")
        self.assertIn("两名杀手均已出局", s["win_reason"])
        names = {p["name"]: p for p in s["players"]}
        self.assertFalse(names["E"]["alive"])   # 第 2 天被 A 刀杀
        self.assertFalse(names["F"]["alive"])   # 第 3 天被 B 毒杀（v4.0：无继任，B 本就是杀手）
        self.assertFalse(names["A"]["alive"])   # 第 3 天投票出局
        self.assertFalse(names["B"]["alive"])   # 第 4 天投票出局
        self.assertTrue(names["C"]["alive"])
        self.assertEqual(names["B"]["identity"], "杀手")
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("**A 的遗言**", log)          # 遗言先于身份公布
        self.assertIn("遗嘱：「万能钥匙」（A 遗留）→ D", log)
        self.assertIn("游戏结束：平民获胜", log)
        # v4.0 清晨调查队列：平民 C（医师）自动结算，结果全员公告（不调 API）
        self.assertIn("C 调查了 E 的案件", log)
        self.assertIn("杀手线索", log)
        self.assertFalse((d / "inbox" / "C" / "day3-案件调查.md").exists())  # 自动结算不调 API
        # v3.3 侦探竞选：C/D 参选、未参选者投票，C 当选（末置发言 + 1.5 票权）
        self.assertEqual(s["detective"], "C")
        self.assertIn("C 宣告参选侦探", log)
        self.assertIn("侦探竞选：C 当选侦探", log)
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

    def test_lone_bedroom_skips_room_action(self):
        """v3.22：夜间独居自己卧室且无可用行动者，房间行动环节整体跳过——不发简报、不调 AI。
        （自己卧室的门锁项无意义不列入；封闭空间仅自由活动，装置与窃听器限非卧室，夜间卧室不列这些噪音项。）"""
        d = write_game_dir(self.root, self.players())
        self.run_game(d, killer_script=False)
        # C（医师/急救箱）、E（建筑师）、B（机械师帮凶）夜间独居自己卧室：无任何合法行动 → 零调用
        for name in ("C", "E", "B"):
            self.assertFalse((d / "inbox" / name / "day1-晚间休息-房间行动.md").exists(), name)
        # D（手电筒）在黑暗卧室改由"手电筒判定"环节询问（v3.23 前置），房间行动不再提供手电筒项
        self.assertTrue((d / "inbox" / "D" / "day1-晚间休息-手电筒.md").exists())
        self.assertFalse((d / "inbox" / "D" / "day1-晚间休息-房间行动.md").exists())

    def test_flashlight_lights_bedroom_before_talk(self):
        """v3.23：手电筒判定在进入结算后、交谈之前——点亮黑暗卧室后按亮房正常交谈（不再是摸黑低语），
        同房者得知"有人打开了手电筒"；房间行动菜单不再提供手电筒项。"""
        d = write_game_dir(self.root, self.players())
        base = make_fake_chat(False)

        def chat_fl(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "手电筒" in title and name == "D" and day == 1:
                return ChatResult(name, ok=True, content=payload(notes="x", actions=[{"type": "use_flashlight"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "内交谈" in title and "晚间休息" in title:
                return ChatResult(name, ok=True,
                                  content=payload(speech=f"{name} 的卧谈内容", notes="x"),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "晚间休息" in title and day == 1:
                acts = []
                if name == "C":
                    acts = [{"type": "invite", "target": "D"}]
                elif name == "D":
                    acts = [{"type": "choose_room", "room": "C的卧室"}]
                return ChatResult(name, ok=True, content=payload(notes="x", actions=acts),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_fl)
        s = orch.state
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("C的卧室内交谈（C、D）", script)      # 点亮后按亮房正常交谈
        self.assertNotIn("C的卧室内低语", script)
        self.assertIn("D 的卧谈内容", script)
        self.assertTrue(any("你打开手电筒" in m for m in s["orch"]["hist"]["D"]))
        self.assertTrue(any("房间被照亮" in m for m in s["orch"]["hist"]["C"]))  # 同房者得知
        # 房间行动菜单不再提供手电筒（v3.27：D 可能因"当面赠送"项收到简报，但菜单里不得出现手电筒）
        brief = (d / "inbox" / "D" / "day1-晚间休息-房间行动.md").read_text(encoding="utf-8")
        self.assertNotIn("use_flashlight", brief)

    def test_coverup_only_for_scene_villain(self):
        """毁证窗口（v4.0 篡改专用）只通知身处案发现场的反派——场外杀手不收到毁证机会
        （也不再被即时告知"谋杀成功/案发房间/现场干净"）。"""
        d = write_game_dir(self.root, self.players())
        sj = d / "state.json"
        st = json.loads(sj.read_text(encoding="utf-8"))
        next(p for p in st["players"] if p["name"] == "A")["extra_items"] = ["可疑的粉末"]  # 给杀手篡改手段
        sj.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        self.run_game(d, killer_script=True)
        inbox_a = (d / "inbox" / "A" / "day2-毁证机会.md").read_text(encoding="utf-8")
        self.assertIn("tamper_investigation", inbox_a)   # 杀手在案发现场（书房），菜单为篡改
        self.assertNotIn("move_body", inbox_a)           # v4.0：转移尸体已删除
        self.assertFalse((d / "inbox" / "B" / "day2-毁证机会.md").exists())  # B 在会客室，不通知

    def test_coverup_night_hint(self):
        """白天案件当晚归宿选择时，持有篡改手段（调查次数/粉末）的反派收到"今晚可前往案发现场篡改、
        仅限案发当天"的提示（基于公开死亡公告+阵营自知，无额外泄漏）；无手段者不提示。"""
        d = write_game_dir(self.root, self.players())
        sj = d / "state.json"
        st = json.loads(sj.read_text(encoding="utf-8"))
        next(p for p in st["players"] if p["name"] == "A")["extra_items"] = ["可疑的粉末"]  # 给杀手篡改手段
        sj.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        self.run_game(d, killer_script=True)
        brief = (d / "inbox" / "A" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertIn("仅限案发当天", brief)  # A 第 2 天白天在书房杀 E → 当晚收到篡改提示
        self.assertIn("书房", brief)
        b_brief = (d / "inbox" / "B" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertNotIn("仅限案发当天", b_brief)  # B 无调查手段（延时装置不算），不提示

    def test_last_civilian_pre_execution_accusation(self):
        """v3.21 集成（1v2 残局）：投票将导致最后平民出局 → 结算前窗口发动指认 → 指对则该杀手出局
        （遗言/身份公布/遗嘱同投票出局；v4.0 无继任——另一名杀手照常行动）、本人获救、游戏继续。
        指认消耗武器（桌上枪用掉即消失）。"""
        players = [p for p in self.players() if p["name"] in ("A", "B", "D")]  # 1 平民 D + 2 杀手
        d = write_game_dir(self.root, players)
        sj = d / "state.json"
        st = json.loads(sj.read_text(encoding="utf-8"))
        st["phase"] = {"day": 4, "stage": "投票"}
        st["table_items"] = [{"name": "左轮手枪", "uses_left": 1}]  # D 手无武器，桌上枪可临时取用
        sj.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        base = make_fake_chat(False)

        def chat_endgame(player, messages, *, on_token=None, reasoning_effort=None):
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            name = player.name
            if "武装指认" in title:
                return ChatResult(name, ok=True,
                                  content=payload(speech="你们要票我？那先回答我——你就是杀手！",
                                                  actions=[{"type": "armed_accuse", "target": "A"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "谋杀窗口" in title and "晚间休息" in title and name == "B" and '"type": "murder"' in briefing:
                # 不明药剂第 4 夜已消耗（未遂也耗凶器）；第 5 夜改空手（黑暗卧室豁免附加条件）
                act = {"type": "murder", "target": "D"} | ({"weapon": "不明药剂"} if day == 4 else {})
                return ChatResult(name, ok=True, content=payload(actions=[act]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "晚间休息" in title and name == "B" and day >= 4:
                # 另一名杀手夜间开锁进 D 的卧室（B 机械师的【暴力破解】；锁对反派一视同仁）；
                # 第 4 夜被敏捷闪避挡下（未遂不封顶），第 5 夜再试得手
                return ChatResult(name, ok=True, content=payload(actions=[{"type": "choose_room", "room": "D的卧室"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "投票" in title:
                tgt = "D" if name in ("A", "B") else "A"
                return ChatResult(name, ok=True, content=payload(actions=[{"type": "vote", "target": tgt}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_endgame)
        s = orch.state
        names = {p["name"]: p for p in s["players"]}
        self.assertFalse(names["A"]["alive"])              # 杀手被指认出局（第 4 天投票处）
        self.assertTrue(s["orch"]["out_day"].get("A") == 4)
        self.assertEqual(names["B"]["identity"], "杀手")   # v4.0：无继任，B 本就是杀手
        self.assertFalse(names["D"]["alive"])              # 平民获救后当夜遇袭（闪避挡下）、次日被票出（决斗真实发生）
        self.assertEqual(s["winner"], "反派")
        self.assertNotIn("左轮手枪", [e["name"] for e in s["table_items"]])  # v3.22：临时取用的桌上枪已消耗
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("仍可行使武装指认", log)
        self.assertIn("指认正确", log)
        self.assertIn("已消耗", log)                       # 指认消耗武器（公告口径）
        self.assertIn("**A 的遗言**", log)                  # 处决流程与投票出局一致
        self.assertIn("A 出局。身份：杀手", log)
        self.assertTrue((d / "inbox" / "D" / "day4-武装指认.md").exists())

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
        """夜间特性端到端：密道谋杀（v4.0 窗口化）、窃听器到场放置与防追溯、受邀组合低语、清晨状态表。"""
        players = self.players()
        players[1] = P("B", "杀手", "建筑师", "不明药剂", gender="女", physique=2)  # 密道谋杀持有者
        d = write_game_dir(self.root, players)
        base = make_fake_chat(True)

        def chat_v3(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "房间行动" in title and "自由活动" in title and name == "F" and day == 1 \
                    and '"type": "place_bug"' in briefing:
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "place_bug", "room": "会客室"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "房间行动" in title and "晚间休息" in title and name == "F" and day in (1, 2) \
                    and "bug_listen" in briefing:
                return ChatResult(player.name, ok=True, content=payload(notes="x", actions=[{"type": "bug_listen"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "内交谈" in title and "晚间休息" in title:
                return ChatResult(player.name, ok=True,
                                  content=payload(speech=f"{name} 的密谈内容", notes="x"),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "谋杀窗口" in title and "晚间休息" in title and day == 1 and name == "B" \
                    and '"type": "passage_kill"' in briefing:
                # v4.0：密道是谋杀窗口动作——B 经密道现身书房动手（E 夜行独处；空手体格 2≥2）
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "passage_kill", "target": "E"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "晚间休息" in title:
                acts = []
                if name == "C" and day == 1:
                    acts = [{"type": "invite", "target": "D"}]
                elif name == "D" and day == 1:
                    acts = [{"type": "choose_room", "room": "C的卧室"}]  # 应邀 = 与邀请者同目的地
                elif name == "E" and day == 1:
                    acts = [{"type": "choose_room", "room": "书房"}]     # E 第 1 夜单独夜行书房
                return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_v3)
        s = orch.state
        self.assertEqual(s["winner"], "平民")  # 主线剧本不受影响
        self.assertTrue(s["passage"]["used"])  # 密道谋杀第 1 夜成功，密道损毁
        self.assertFalse(next(p for p in s["players"] if p["name"] == "E")["alive"])  # E 第 1 夜遇害
        self.assertNotIn("B", s["monitoring_true"]["第1天晚间休息"].get("书房", []))  # 监控无痕
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("◆ B｜密道谋杀 → E", script)          # 密道谋杀入全记录
        self.assertIn("内低语（黑暗）", script)            # 受邀组合在黑暗卧室摸黑低语
        self.assertIn("C 的密谈内容", script)
        self.assertIn("窃听器录音下发给 F（会客室）", script)
        self.assertIn("状态一览（全剧透）", script)
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("目前存活", log)
        self.assertIn("无杀倒计时：", log)
        self.assertIn("侦探：C", log)
        recordings = [m for m in s["orch"]["hist"]["F"] if "窃听器录音" in m]
        self.assertTrue(any("此前交谈不追溯" in m for m in recordings), recordings)  # 首日放置前的谈话不可听
        self.assertTrue(any("第2天发言" in m for m in recordings), recordings)       # 后续一天的谈话正常记录

    def test_hall_night_talk(self):
        """v3.22：夜宿会客室者（≥2 人）睡前围炉夜谈=亮房正常房间交谈——内容仅当夜夜宿者知晓。"""
        d = write_game_dir(self.root, self.players())
        base = make_fake_chat(False)

        def chat_hall(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "内交谈" in title and "晚间休息" in title:
                return ChatResult(player.name, ok=True,
                                  content=payload(speech=f"{name} 的夜谈内容", notes="x"),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "选房" in title and day == 1 and name in ("A", "C"):
                # 第 1 天一次未遂（C 的急救箱挡下）重置倒计时——否则 v4.0 下第 3 天清晨游戏就结束了
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "choose_room", "room": "书房"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "谋杀窗口" in title and day == 1 and name == "A" and '"type": "murder"' in briefing:
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "murder", "target": "C", "weapon": "刀"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "晚间休息" in title:
                # 第 2 夜 E（万能钥匙开锁）与 F（随入）夜宿会客室
                acts = [{"type": "choose_room", "room": "会客室"}] if name in ("E", "F") and day == 2 else []
                return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_hall)
        s = orch.state
        self.assertEqual(s["winner"], "平民")  # 消极局照常倒计时收尾
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("会客室内交谈（E、F）", script)
        self.assertIn("E 的夜谈内容", script)
        self.assertIn("F 的夜谈内容", script)
        # 仅当夜夜宿者可见：E/F 私密历史有夜谈内容，C（卧室过夜）听不到
        self.assertTrue(any("夜谈内容" in m for m in s["orch"]["hist"]["E"]))
        self.assertTrue(any("夜谈内容" in m for m in s["orch"]["hist"]["F"]))
        self.assertFalse(any("夜谈内容" in m for m in s["orch"]["hist"].get("C", [])))
        # 夜宿名单清晨照常公开（夜谈不影响公开口径）
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("夜宿会客室公开：E、F", log)

    def test_night_dark_whisper_with_lurker(self):
        """v3.22：停电夜的黑暗书房中，同入邀请成立的组合（C、D）摸黑低语——未受邀的潜伏反派 A 旁听全部内容，
        低语者不知情；黑暗中谋杀条件放宽（非反派 ≤2），A 可袭击二人之一（C 的急救箱自动防下）。"""
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
            if "谋杀窗口" in title and "晚间休息" in title and day == 2 and name == "A" and '"type": "murder"' in briefing:
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "murder", "target": "C", "weapon": "刀"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "内交谈" in title and "晚间休息" in title:
                return ChatResult(player.name, ok=True,
                                  content=payload(speech=f"{name} 的密谈内容", notes="x"),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "晚间休息" in title and day == 2:
                acts = []
                if name == "C":
                    acts = [{"type": "choose_room", "room": "书房"}, {"type": "invite", "target": "D"}]
                elif name == "D":
                    acts = [{"type": "choose_room", "room": "书房"}]
                elif name == "A":
                    acts = [{"type": "choose_room", "room": "书房"}]  # 反派不受平民慎入限制
                return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
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
        self.assertIn("书房内低语（黑暗）（C、D）【旁听：A】", script)
        self.assertIn("C 的密谈内容", script)
        # 低语组合正常收到；潜伏者收到全部内容；低语者的记录不泄漏"被旁听"
        self.assertTrue(any("内低语" in m and "密谈内容" in m for m in s["orch"]["hist"]["C"]))
        self.assertTrue(any("低声交谈" in m for m in s["orch"]["hist"]["A"]))
        self.assertFalse(any("旁听" in m or "听到" in m for m in s["orch"]["hist"]["C"]))
        # 邀请成立对邀请者私密可见（白天同入 + 夜间同入）
        self.assertTrue(any("接受了你的同入邀请" in m for m in s["orch"]["hist"]["C"]))   # 白天选房邀请
        self.assertTrue(any("接受了你的邀请" in m for m in s["orch"]["hist"]["C"]))       # 夜间邀请
        # 黑暗中（非反派 ≤2）杀手可袭击二人之一：C 的急救箱自动防下（未遂）
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

        # 第 1 天调用序：竞选声明 A..F(1-6) → 退选 C,D(7-8) → 竞选投票 A,B,E,F(9-12)
        # → 自由活动-选房 A(13)：第 13 次后停 = 选房中途；窃听器不再占用开局准备调用
        orch = Orchestrator(d, hooks={"should_stop": lambda: calls["n"] >= 13})
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

    def test_armed_accusation_hint_dual_targets(self):
        """v4.0：武装指认速查写明双目标口径（两名杀手都是正确目标，全部出局平民才获胜）。"""
        hints = self.orch._stage_hints("第2天 · 武装指认")
        self.assertTrue(any("两名杀手" in h and "全部出局" in h for h in hints), hints)
        self.assertFalse(any("继任" in h for h in hints), hints)

        self.orch.state["is_lite_rule"] = True
        hints = self.orch._stage_hints("第2天 · 投票")
        self.assertTrue(any("两名杀手全部出局平民才获胜" in h for h in hints), hints)
        self.assertFalse(any("继任" in h or "平民立即获胜" in h for h in hints), hints)

    def test_night_hint(self):
        hints = self.orch._stage_hints("第2天 · 晚间休息")
        self.assertTrue(any("互不可见" in h for h in hints), hints)
        self.assertTrue(any("卧室夜间黑暗" in h for h in hints), hints)
        self.assertTrue(any("不敢" in h for h in hints), hints)  # 平民慎入

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

    def test_villains_know_dark_roommates(self):
        s = self.orch.state
        s["phase"] = {"day": 2, "stage": "晚间休息"}
        s["positions"] = {n: "会客室" for n in s["order"]}
        s["positions"].update({"A": "地下室", "B": "地下室", "C": "地下室"})
        for name, expected in (("A", "B、C"), ("B", "A、C")):
            text = self.orch.build_briefing(self.orch.player(name), "第2天 · 晚间休息（房间行动）", [])
            current = text.split("## 当前局面\n", 1)[1].splitlines()[0]
            self.assertIn(f"你仍知道在场者：{expected}", current)
            self.assertIn("也无法交谈", current)  # 只增加人员信息，不解除黑暗限制
        text = self.orch.build_briefing(self.orch.player("C"), "第2天 · 晚间休息（房间行动）", [])
        current = text.split("## 当前局面\n", 1)[1].splitlines()[0]
        self.assertIn("你看不见这里有谁", current)
        self.assertNotIn("你仍知道在场者", current)

    def test_room_action_rejects_multiple_actions(self):
        s = self.orch.state
        s["phase"] = {"day": 2, "stage": "自由活动"}
        s["power_out"] = True
        s["positions"] = {n: "会客室" for n in s["order"]}
        s["positions"].update({"A": "地下室", "C": "地下室"})
        calls = []

        def chat_multi(player, messages, *, on_token=None, reasoning_effort=None):
            acts = []
            if player.name == "A":
                calls.append(player.name)
                if len(calls) == 1:
                    acts = [{"type": "fix_generator"}, {"type": "murder", "target": "C", "weapon": "刀"}]
                else:
                    acts = [{"type": "fix_generator"}]
            return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
                              latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")

        orig = orchestrator.chat
        orchestrator.chat = chat_multi
        try:
            self.orch.room_actions(2)
        finally:
            orchestrator.chat = orig
        self.assertEqual(len(calls), 2)  # 初次多报 + 一次驳回重报，没有修后额外行动
        self.assertFalse(s["power_out"])  # 重报只保留修电后正常执行
        self.assertTrue(self.orch.player("C")["alive"])  # 首次多报中的谋杀没有部分执行
        self.assertEqual(self.orch.player("A")["items"][0]["name"], "刀")
        self.assertEqual(self.orch.player("A")["items"][0]["uses_left"], 1)
        script = (self.orch.dir / "script.md").read_text(encoding="utf-8")
        self.assertIn("一次声明 2 个房间动作 → 驳回", script)

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
        """夜间房间行动误报已消耗凶器 → 驳回后当场私下重报一次 → 改报空手落杀（重报独立存档/日志/host 全记录）。"""
        d = write_game_dir(self.root / "g2", default_players())
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            acts = None
            if "房间行动" in title and "晚间休息" in title and name == "A" and day == 3:
                if "刚才的动作声明被驳回" in briefing:  # 驳回重报的第二次调用：改报空手
                    acts = [{"type": "murder", "target": "F"}]
                else:                   # 第一次：误报已在第 2 天消耗掉的刀
                    acts = [{"type": "murder", "target": "F", "weapon": "刀"}]
            elif "晚间休息" in title and name == "A" and day == 3:
                acts = [{"type": "choose_room", "room": "F的卧室"}]  # 万能钥匙开锁进入
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
        self.assertTrue(any(c["victim"] == "F" and c["room"] == "F的卧室" for c in s["cases"]))  # 重报后落杀成功
        host_log = (d / "host_log.jsonl").read_text(encoding="utf-8")
        self.assertIn("晚间休息-房间行动#驳回重报", host_log)    # host_log 独立条目
        self.assertTrue((d / "outbox" / "A" / "day3-晚间休息-房间行动#驳回重报.md").exists())  # 重报独立存档
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

    def test_night_menu_unified_choices(self):
        """v3.22：夜间菜单统一为 choose_room（含他人卧室与四房间），不再有独立夜袭/夜宿菜单项。"""
        d1 = write_game_dir(self.root / "g4", default_players())
        self.run_game(d1, killer_script=False)
        night2 = (d1 / "inbox" / "A" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertIn("choose_room", night2)
        self.assertIn("某人的卧室", night2)
        self.assertNotIn("night_raid", night2)
        self.assertNotIn("night_hall", night2)

    def test_invited_killer_murders_host_at_night(self):
        """v3.22：平民亲手邀请杀手进卧室（与虎谋皮）——杀手应邀进入（免开锁），黑暗卧室中谋杀。"""
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="男", physique=4)  # 无开锁手段——受邀免锁
        d = write_game_dir(self.root / "g3", players)
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            acts = None
            if "房间行动" in title and "晚间休息" in title and name == "A" and day == 2:
                acts = [{"type": "murder", "target": "F", "weapon": "刀"}]
            elif "晚间休息" in title and day == 2:
                if name == "F":
                    acts = [{"type": "invite", "target": "A"}]   # F 亲手邀请杀手守夜
                elif name == "A":
                    acts = [{"type": "choose_room", "room": "F的卧室"}]
                else:
                    acts = []
            if acts is None:
                return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)
            return ChatResult(player.name, ok=True, content=payload(notes="x", actions=acts),
                              latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")

        orch = self.run_game_with(d, chat_fn)
        s = orch.state
        self.assertTrue(any(c["victim"] == "F" and c["room"] == "F的卧室" for c in s["cases"]))
        self.assertFalse(next(p for p in s["players"] if p["name"] == "F")["alive"])
        self.assertTrue(any("接受了你的邀请" in m for m in s["orch"]["hist"]["F"]))  # 邀请成立对房主可见


class TestSpeechRouting(MockGameTestBase):
    """v3.8 附言公开：交换/受赠回应/竞选退选/竞选投票/武装指认/死者字条；私密环节简报明示不发言。"""

    def test_election_remarks_published(self):
        """密封退选与竞选投票的附言随结果/开票一齐公开。"""
        d = write_game_dir(self.root, default_players())
        self.run_game(d, killer_script=False)  # C/D 参选并留任，A/B/E/F 投 C
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("（退选揭晓）", log)
        self.assertIn("（竞选投票）", log)

    def test_gift_in_room_actions(self):
        """v3.27：赠送挪入房间行动——同房当面赠送、每天限一次；不再有全局公开公告，
        明亮房间仅同房者可见（道具名不公开），黑暗房间无人知晓。"""
        d = write_game_dir(self.root / "g7", default_players())
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            title = messages[-1]["content"].splitlines()[0]
            if "房间行动" in title and "第1天" in title and name == "C":
                return ChatResult(player.name, ok=True,
                                  content=payload(speech="", notes="x",
                                                  actions=[{"type": "gift", "to": "D", "item": "急救箱"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "赠送回应" in title and name == "D":
                return ChatResult(player.name, ok=True,
                                  content=payload(speech="", notes="x", actions=[{"type": "accept"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        self.run_game_with(d, chat_fn)
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertNotIn("把一件道具交给了", log)  # v3.27：取消全局公开公告
        self.assertNotIn("（交换环节）", log)  # 白天会议后的固定交换环节已移除
        state = json.loads((d / "state.json").read_text(encoding="utf-8"))
        dv = next(p for p in state["players"] if p["name"] == "D")
        self.assertIn("急救箱", [e["name"] for e in dv["items"]])

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


class TestSecondKillerSkills(MockGameTestBase):
    """v4.0 双杀手：第二名杀手（原帮凶位）的谋杀窗口菜单同时含普通谋杀与反派技能杀，声明后真正击杀。"""

    def test_second_killer_magician_stage_magic(self):
        players = default_players()
        players[1] = P("B", "杀手", "魔术师", "不明药剂", gender="女", physique=2)
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
            if day == 2 and "谋杀窗口" in title and name == "B" and '"type": "stage_magic"' in briefing:
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "stage_magic", "target": "E"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_fn)
        s = orch.state
        # 菜单证据：第二名杀手的谋杀窗口简报同时含普通谋杀与舞台魔术项（v4.0 两人都是完整杀手）
        inbox = (d / "inbox" / "B" / "day2-自由活动-谋杀窗口.md").read_text(encoding="utf-8")
        self.assertIn('"type": "stage_magic"', inbox)
        self.assertIn('"type": "murder"', inbox)
        self.assertIn("可被急救箱", inbox)  # v4.0 削弱：舞台魔术可防御（菜单口径同步）
        # 击杀落账：E 死亡，案件方法=舞台魔术，凶手=B 本人（独处现场，E 的万能钥匙被 B 缴获）
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
        self.assertIn("无杀倒计时：", b)
        self.assertIn("当前进度 0/2", b)  # v3.20：倒计时直白口径（进度 + 触发条件）
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
        self.assertIn("当前进度 0/2", b2)  # 第 2 天有谋杀行为，倒计时清零
        self.assertIn("C（女·健壮·中国·第2天出局：平民/医师）", b2)
        seg_d = next(x for x in b2.split("、") if x.startswith("D（"))
        self.assertIn("第2天死亡（书房）", seg_d)
        self.assertIn("职业已公开：运动员", seg_d)
        self.assertNotIn("平民", seg_d)  # 被谋杀者身份不公开


class TestAnnouncementScope(MockGameTestBase):
    """game/45 复盘：简报「游戏状态」段按清晨公告口径——当天谋杀在当天简报里仍显示遇害者存活、
    倒计时保持清晨公告值（§7.1 死亡次日清晨才公告，场外反派也无从得知）；房间行动在 winner
    产生后整环节收束（终局后不再调用 AI）。"""

    def _orch_at(self, name, day, positions, cases=None, dead=(), last_murder_day=None, out_day=None):
        d = write_game_dir(self.root / name, default_players())
        orch = Orchestrator(d)
        s = orch.state
        s["phase"] = {"day": day, "stage": "自由活动"}
        s["rooms_entered"] = f"第{day}天自由活动"
        s["positions"] = dict(positions)
        s["cases"] = list(cases or [])
        s["last_murder_day"] = last_murder_day
        for n in dead:
            next(q for q in s["players"] if q["name"] == n)["alive"] = False
        if out_day:
            orch.orch["out_day"] = dict(out_day)
        return orch

    def _drive_room_actions(self, orch, day, kill_target):
        calls: dict[str, str] = {}

        def cap(player, messages, *, on_token=None, reasoning_effort=None):
            brief = messages[-1]["content"]
            calls[player.name] = brief
            acts = [{"type": "murder", "target": kill_target, "weapon": "刀"}] if player.name == "A" else []
            return ChatResult(player.name, ok=True, content=payload(actions=acts),
                              latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")

        orig = orchestrator.chat
        orchestrator.chat = cap
        try:
            orch.room_actions(day, "自由活动")
        finally:
            orchestrator.chat = orig
        return calls

    def test_same_day_death_hidden_in_briefing(self):
        """A 第 2 天书房刀杀 E → 同阶段后排房间（监控室 B）的简报不得泄漏：E 仍列存活、存活数不变。"""
        orch = self._orch_at("g45a", 2, {"A": "书房", "E": "书房", "B": "监控室",
                                         "C": "会客室", "D": "会客室", "F": "会客室"})
        calls = self._drive_room_actions(orch, 2, "E")
        s = orch.state
        self.assertTrue(any(c["victim"] == "E" and c["day"] == 2 for c in s["cases"]))
        self.assertIsNone(s["winner"])  # 仍有其他平民，游戏继续
        self.assertIn("B", calls)  # B 在监控室有菜单（查监控/延时装置），谋杀后被调用
        b = calls["B"]
        self.assertIn("E（男·普通·中国·存活）", b)      # 遇害者按公告口径仍显示存活
        self.assertIn("存活 6 人", b)
        self.assertNotIn("第2天死亡", b)

    def test_countdown_keeps_morning_value_on_murder_day(self):
        """第 2 天已有谋杀、第 3 天空白 → 第 4 天清晨公告 1/2；当天再次谋杀后，同天简报仍显示 1/2
        （倒计时重置次日清晨才公告）。"""
        case_d2 = {"id": 1, "victim": "E", "room": "书房", "crime_room": "书房", "day": 2,
                   "stage": "自由活动", "period": "第2天自由活动", "method": "刀", "wound": "利器",
                   "culprit": "A", "body_moved": False, "falsified": None, "looted": [],
                   "evidence_vulnerable": True, "sealed": True}
        orch = self._orch_at("g45b", 4, {"A": "书房", "F": "书房", "B": "监控室", "C": "会客室", "D": "会客室"},
                             cases=[case_d2], dead=("E",), last_murder_day=2)
        calls = self._drive_room_actions(orch, 4, "F")
        self.assertTrue(any(c["victim"] == "F" and c["day"] == 4 for c in orch.state["cases"]))
        b = calls["B"]
        self.assertIn("已连续 1 个完整天无谋杀行为（1/2）", b)  # 保持清晨公告值
        self.assertNotIn("当前进度 0/2", b)
        self.assertIn("F（女·普通·中国·存活）", b)  # 当天遇害者同样按公告口径

    def test_next_morning_roster_shows_death(self):
        """公告口径不误伤次日：隔夜案件在角色表中正常显示死亡与案发房间（职业未公开则不显示）。"""
        orch = self._orch_at("g45c", 3, {}, cases=[{"id": 1, "victim": "E", "room": "书房", "day": 2}],
                             dead=("E",), last_murder_day=2)
        text = "\n".join(orch._game_state_lines())
        self.assertIn("存活 5 人", text)
        seg_e = next(x for x in text.split("、") if x.startswith("E（"))
        self.assertIn("第2天死亡（书房）", seg_e)
        self.assertNotIn("黑客", seg_e)  # 身份不公开（§9.4）；职业未经清晨公开前也不显示

    def test_room_actions_stop_after_winner(self):
        """谋杀导致反派胜利（平民全灭）后，本环节剩余席位不再被调用（终局无合法行动 + 收窄泄漏窗口）。"""
        orch = self._orch_at("g45d", 4, {"A": "会客室", "F": "会客室", "B": "监控室"},
                             dead=("C", "D", "E"), out_day={"C": 2, "D": 3, "E": 3})
        calls = self._drive_room_actions(orch, 4, "F")
        self.assertEqual(orch.state["winner"], "反派")
        self.assertIn("A", calls)      # 杀手本人被调用并完成谋杀
        self.assertNotIn("B", calls)   # winner 产生后，后排房间的帮凶不再被调用

    def test_room_actions_skip_seat_killed_earlier_in_loop(self):
        """同房谋杀后，遇害者在本环节剩余行动被跳过：A 在地下室刀杀 E（E 无自卫手段、
        顺序在 A 之后）→ E 不再收到房间行动调用（无简报/回执），同房幸存的 F 照常行动。"""
        orch = self._orch_at("g45f", 2, {"A": "地下室", "E": "地下室", "F": "地下室",
                                         "B": "会客室", "C": "会客室", "D": "会客室"})
        calls = self._drive_room_actions(orch, 2, "E")
        s = orch.state
        self.assertTrue(any(c["victim"] == "E" and c["day"] == 2 for c in s["cases"]))
        self.assertFalse(next(p for p in s["players"] if p["name"] == "E")["alive"])
        self.assertIn("A", calls)      # 杀手本人完成谋杀
        self.assertNotIn("E", calls)   # 已死亡：不再被调用（修复前 occ 快照会让 E 照常行动）
        self.assertIn("F", calls)      # 同房幸存者的行动不受影响
        d = orch.dir
        self.assertFalse((d / "inbox" / "E" / "day2-自由活动-房间行动.md").exists())
        self.assertFalse((d / "outbox" / "E" / "day2-自由活动-房间行动.md").exists())

    def test_talks_and_actions_skip_when_game_over(self):
        """进入结算的装置击杀终局后，房间交谈/房间行动均整环节跳过（门卫检查）。"""
        orch = self._orch_at("g45e", 2, {"C": "会客室", "D": "会客室"})
        orch.state["winner"] = "反派"
        calls = []

        def cap(player, messages, *, on_token=None, reasoning_effort=None):
            calls.append(player.name)
            return ChatResult(player.name, ok=True, content=payload(),
                              latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")

        orig = orchestrator.chat
        orchestrator.chat = cap
        try:
            orch.room_talks(2, "自由活动")
            orch.room_actions(2, "自由活动")
        finally:
            orchestrator.chat = orig
        self.assertEqual(calls, [])


class TestCaseIdRobustness(MockGameTestBase):
    """案件指认主口径=受害者名字（"case" 字段）：容忍「」引号与"案"后缀、兼容旧式整数 case_id、
    单案件自动归一；未知名驳回并附案件名单，不得炸穿编排器（game/18 复盘）。无案件清晨调查整体跳过。"""

    @staticmethod
    def _mk_case(cid, victim, room="书房"):
        return {"id": cid, "victim": victim, "room": room, "crime_room": room, "day": 2,
                "stage": "自由活动", "period": "第2天自由活动", "method": "刀", "wound": "利器",
                "culprit": "A", "body_moved": False, "clue_trait": None, "falsified": None,
                "investigated": False, "looted": [], "evidence_vulnerable": True, "sealed": True,
                "announced": True, "discovered_by": [], "reported_by": []}

    def _orch_with_cases(self, name, cases):
        d = write_game_dir(self.root / name, default_players())
        orch = Orchestrator(d)
        orch.state["cases"] = cases
        orch.state["phase"] = {"day": 3, "stage": "案件调查"}
        return orch

    def test_case_resolved_by_victim_name(self):
        orch = self._orch_with_cases("g13a", [self._mk_case(1, "E"), self._mk_case(2, "F", "地下室")])
        p = next(q for q in orch.state["players"] if q["name"] == "C")  # 医师：现场调查×2
        ev = orch.dispatch(p, {"type": "investigate", "case": "F"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["private"]["C"]["case"], "F")      # 回执回显受害者与房间（选错可自查）
        self.assertEqual(ev["private"]["C"]["room"], "地下室")
        self.assertEqual(set(ev["private"]["C"]["results"]), {"死亡时间", "尸体转移", "武器伤痕", "杀手线索"})  # v4.0 四项全给
        ev = orch.dispatch(p, {"type": "investigate", "case": "「E案」"})
        self.assertTrue(ev["ok"], ev)  # 容忍「」引号与"案"后缀
        self.assertEqual(ev["private"]["C"]["case"], "E")
        ev = orch.dispatch(p, {"type": "investigate", "case": "格罗克"})
        self.assertFalse(ev["ok"])     # 未知名：解析层驳回（先于引擎，不耗次数）
        self.assertIn("受害者名字", ev["error"])
        self.assertIn("E", ev["error"])

    def test_case_legacy_case_id_still_accepted(self):
        orch = self._orch_with_cases("g13b", [self._mk_case(1, "E"), self._mk_case(2, "F", "地下室")])
        p = next(q for q in orch.state["players"] if q["name"] == "C")
        ev = orch.dispatch(p, {"type": "investigate", "case_id": 2})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["private"]["C"]["case"], "F")
        ev = orch.dispatch(p, {"type": "investigate", "case_id": "案件1"})
        self.assertTrue(ev["ok"], ev)  # 可提取数字的写法兼容
        self.assertEqual(ev["private"]["C"]["case"], "E")

    def test_single_case_auto_default(self):
        orch = self._orch_with_cases("g13c", [self._mk_case(1, "E")])
        p = next(q for q in orch.state["players"] if q["name"] == "C")
        ev = orch.dispatch(p, {"type": "investigate"})  # 缺省 → 唯一案件
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["private"]["C"]["case"], "E")

    def test_unknown_case_rejected_not_raised(self):
        orch = self._orch_with_cases("g13d", [])
        p = next(q for q in orch.state["players"] if q["name"] == "C")
        ev = orch.dispatch(p, {"type": "investigate", "case_id": "格罗克遇袭"})
        self.assertFalse(ev["ok"])
        self.assertIn("受害者名字", ev["error"])
        ev2 = orch.dispatch(p, {"type": "tamper_investigation", "case_id": None})
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


class TestEntryUnlockRound(MockGameTestBase):
    def test_day_declarations_then_unlock_and_opener_priority(self):
        d = write_game_dir(self.root, default_players())
        base = make_fake_chat(False)

        def chat_rooms(player, messages, *, on_token=None, reasoning_effort=None):
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "选房" in title and day == 2 and player.name in ("D", "E", "F"):
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "choose_room", "room": "监控室"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_rooms)
        records = orch.state["monitoring_true"]["第2天自由活动"]
        self.assertEqual(records["监控室"], ["E", "D"])  # E 开锁优先；先前声明的 D 不再被提前劝退
        self.assertIn("F", records["会客室"])          # 容量仍在进入轮判定
        self.assertTrue((d / "outbox" / "E" / "day2-自由活动-开锁.md").exists())
        self.assertFalse((d / "outbox" / "D" / "day2-自由活动-开锁.md").exists())
        unlock_wip = orch.state["orch"]["wip"]["2:自由活动:选房"]
        self.assertEqual(unlock_wip["unlockers"], {"监控室": "E"})
        self.assertIn("E→监控室", unlock_wip["unlock_done"])
        brief = (d / "inbox" / "F" / "day2-自由活动-选房.md").read_text(encoding="utf-8")
        self.assertIn("截至目前去向声明", brief)
        self.assertIn("D→监控室", brief)
        self.assertIn("E→监控室", brief)
        self.assertNotIn("各房间进入情况", brief)
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("◆ E｜开锁：监控室 → 成功", script)


class TestBlackoutDiscovery(MockGameTestBase):
    """停电逐人发现：玩家在抵达所选房间时收到回执；选房简报只给去向声明，
    不提前展示门锁或进入结果，后续声明者无法据此推断停电。"""

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
        # 选房简报只展示去向声明，不提前展示门锁或进入结果
        self.assertTrue(any("截至目前去向声明" in b for _, b in briefings))
        for name, b in briefings:
            self.assertNotIn("门仍上锁", b, name)
        # v3.23 修复：拉闸后、进入前，选房简报不得提前泄漏停电（游戏状态段供电行在公布前隐藏）
        for name, b in briefings:
            if name == "B":
                continue  # 拉闸者本人的私密回执含"停电开始"，属合法知情
            self.assertNotIn("停电", b, name)
        # 选房收尾公告后恢复显示：晚间简报「游戏状态」含供电行（本局无人修复）
        night_brief = (d / "inbox" / "A" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertIn("供电：停电中", night_brief)
        # 停电不动物理锁台账：第 2 天选房结束（进入晚间休息）时监控室仍记为上锁，供电恢复后回到原状态
        night2 = next(v for t, v in locks_snap.items() if "第2天" in t and "晚间休息" in t)
        self.assertTrue(night2["监控室"])

    def test_power_restored_notifications(self):
        """v3.20：白天修复发电机后——晚间休息公告供电已恢复、当夜监控恢复记录、
        清晨缺口精确到"仅自由活动时段"（不再与夜间记录矛盾）、简报「游戏状态」显示供电已修复。
        （game/838823：杀手白天拉闸、平民当天修复，杀手仍误以为当夜"监控空白"）"""
        d = write_game_dir(self.root, default_players())
        base = make_fake_chat(False)

        def chat_repair(player, messages, *, on_token=None, reasoning_effort=None):
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
                    acts = [{"type": "choose_room", "room": "地下室"}]
                return ChatResult(name, ok=True, content=payload(notes="x", actions=acts),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if day == 2 and "房间行动" in title and name == "C":
                return ChatResult(name, ok=True, content=payload(notes="x", actions=[{"type": "fix_generator"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_repair)
        s = orch.state
        self.assertFalse(s["power_out"])
        self.assertEqual(s["power_restored"], "第2天自由活动")
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("供电已恢复（发电机已于今日修复）：今晚门锁与监控正常工作", log)
        self.assertIn("第2天晚间休息", s["monitoring_true"])  # 当夜归宿进入恢复记录（空白不回溯，修复后正常）
        self.assertEqual(s["blackout_periods"], ["第2天自由活动"])
        night_brief = (d / "inbox" / "A" / "day2-晚间休息.md").read_text(encoding="utf-8")
        self.assertIn("供电：今日停电已被修复", night_brief)

    def test_cross_day_power_restored_notifications(self):
        """第 2 天拉闸、第 3 天修复：恢复公告和简报按实际修复日出现，缺失时段跨日连续记录。"""
        d = write_game_dir(self.root, default_players())
        base = make_fake_chat(False)

        def chat_repair(player, messages, *, on_token=None, reasoning_effort=None):
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            name = player.name
            if day == 2 and "停电声明" in title and name == "B":
                return ChatResult(name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "declare_blackout"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if day in (2, 3) and "选房" in title:
                room = {"C": "地下室"}.get(name, "会客室")
                if day == 2 and name in ("A", "D"):
                    room = "书房"  # 第 2 天一次未遂（D 的敏捷闪避挡下）重置倒计时，游戏才能拖到第 3 天修复
                acts = [{"type": "choose_room", "room": room}]
                return ChatResult(name, ok=True, content=payload(notes="x", actions=acts),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if day == 2 and "谋杀窗口" in title and name == "A" and '"type": "murder"' in briefing:
                return ChatResult(name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "murder", "target": "D", "weapon": "刀"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if day == 3 and "房间行动" in title and name == "C":
                return ChatResult(name, ok=True, content=payload(notes="x", actions=[{"type": "fix_generator"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_repair)
        s = orch.state
        self.assertFalse(s["power_out"])
        self.assertEqual(s["power_restored"], "第3天自由活动")
        self.assertEqual(s["blackout_days"], [2])
        self.assertEqual(s["blackout_periods"],
                         ["第2天自由活动", "第2天晚间休息", "第3天自由活动"])
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("供电已恢复（发电机已于今日修复）：今晚门锁与监控正常工作", log)
        self.assertIn("地下室与夜间卧室仍黑暗", log)
        night_brief = (d / "inbox" / "A" / "day3-晚间休息.md").read_text(encoding="utf-8")
        self.assertIn("供电：今日停电已被修复", night_brief)
        self.assertIn("停电造成的黑暗已解除", night_brief)

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
            if "选房" in title and day == 1 and player.name in ("A", "C"):
                # 第 1 天一次未遂（C 的急救箱挡下）重置倒计时——否则 v4.0 下第 3 天清晨游戏就结束了
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "choose_room", "room": "书房"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "谋杀窗口" in title and day == 1 and player.name == "A" and '"type": "murder"' in briefing:
                return ChatResult(player.name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "murder", "target": "C", "weapon": "刀"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
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
        self.assertTrue(s["orch"]["talks"].get("2:自由活动:书房"))


class TestFirstDayMurderAndFeedback(MockGameTestBase):
    def test_day1_murder_menu_in_both_action_stages(self):
        orch = Orchestrator(write_game_dir(self.root / "day1-menu", default_players()))
        for stage in ("自由活动", "晚间休息"):
            orch.state["phase"] = {"day": 1, "stage": stage}
            orch.state["stage_murder_used"] = False
            orch.state["positions"] = {"A": "书房", "E": "书房", "B": "监控室",
                                       "C": "会客室", "D": "会客室", "F": "会客室"}
            menu = orch.murder_menu(orch.player("A"), 1)
            self.assertTrue(any('"type": "murder"' in item and "E" in item for item in menu), stage)

    def test_empty_murder_window_feedback_once_without_api(self):
        orch = Orchestrator(write_game_dir(self.root / "empty-feedback", default_players()))
        orch.state["phase"] = {"day": 1, "stage": "晚间休息"}
        orch.state["positions"] = {p["name"]: f"{p['name']}的卧室" for p in orch.state["players"]}
        orig = orchestrator.chat

        def unexpected(*args, **kwargs):
            raise AssertionError("空目标不应调用 API")

        orchestrator.chat = unexpected
        try:
            orch.murder_window(1, "晚间休息")
            orch.murder_window(1, "晚间休息")  # WIP 已完成，不得重复发回执
        finally:
            orchestrator.chat = orig
        messages = [m for m in orch.state["orch"]["hist"]["A"] if "没有可攻击目标" in m]
        self.assertEqual(len(messages), 1)
        self.assertIn("未发生袭击", messages[0])
        self.assertIn("不消耗本窗口谋杀尝试", messages[0])
        self.assertIn("A", orch.state["orch"]["wip"]["1:晚间休息:谋杀"]["done"])
        saved = json.loads((orch.dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(len([m for m in saved["orch"]["hist"]["A"] if "没有可攻击目标" in m]), 1)

    def test_lite_night_selection_requires_direct_destination(self):
        orch = Orchestrator(write_game_dir(self.root / "lite-night", default_players()))
        orch.state["phase"] = {"day": 1, "stage": "晚间休息"}
        orch.state["is_lite_rule"] = True
        briefings = []
        orig = orchestrator.chat

        def fake_chat(player, messages, *, on_token=None, reasoning_effort=None):
            briefings.append(messages[-1]["content"])
            return ChatResult(player.name, ok=True, content=payload(notes="x"), latency=0.01,
                              usage={"total_tokens": 5}, finish_reason="stop")

        orchestrator.chat = fake_chat
        try:
            orch.night_select(1, "第1天晚间休息")
        finally:
            orchestrator.chat = orig
        self.assertEqual(len(briefings), 6)
        for briefing in briefings:
            self.assertIn("不能先回房待命", briefing)
            self.assertIn("想去某人的卧室就现在直接选择该卧室", briefing)

    def test_murder_rejection_and_failure_wording(self):
        orch = Orchestrator(write_game_dir(self.root / "reject-feedback", default_players()))
        p = orch.player("A")
        orch.reject_note(p, {"type": "murder", "error": "未持有可用凶器"})
        msg = orch.state["orch"]["hist"]["A"][-1]
        self.assertIn("未发生袭击", msg)
        self.assertIn("不消耗本窗口谋杀尝试", msg)
        hints = orch._stage_hints("第1天 · 自由活动（谋杀窗口）")
        self.assertTrue(any("动作被驳回" in h and "谋杀失败" in h for h in hints), hints)

    def test_placement_menu_any_action_stage_but_not_bedroom(self):
        orch = Orchestrator(write_game_dir(self.root / "placement-menu", default_players()))
        for stage in ("自由活动", "晚间休息"):
            orch.state["phase"] = {"day": 1, "stage": stage}
            orch.state["positions"] = {"B": "监控室", "F": "会客室"}
            self.assertTrue(any('"type": "set_device"' in item for item in orch.action_menu(orch.player("B"), 1)), stage)
            self.assertTrue(any('"type": "place_bug"' in item for item in orch.action_menu(orch.player("F"), 1)), stage)
            orch.state["positions"] = {"B": "B的卧室", "F": "F的卧室"}
            self.assertFalse(any('"type": "set_device"' in item for item in orch.action_menu(orch.player("B"), 1)), stage)
            self.assertFalse(any('"type": "place_bug"' in item for item in orch.action_menu(orch.player("F"), 1)), stage)

    def test_bug_recording_not_retroactive(self):
        orch = Orchestrator(write_game_dir(self.root / "bug-no-history", default_players()))
        orch.state["phase"] = {"day": 1, "stage": "自由活动"}
        orch.state["positions"] = {"F": "会客室"}
        orch.orch["talks"]["1:自由活动:会客室"] = ["放置前的旧谈话"]
        ev = orch.dispatch(orch.player("F"), {"type": "place_bug", "room": "会客室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(orch.state["bug_placed"], {"day": 1, "stage": "自由活动"})

        orch.state["phase"] = {"day": 1, "stage": "晚间休息"}
        orch.state["positions"] = {"F": "F的卧室"}
        ev = orch.dispatch(orch.player("F"), {"type": "bug_listen"})
        self.assertTrue(ev["ok"], ev)
        orch.orch["talks"]["2:自由活动:会客室"] = ["放置后的新谈话"]
        orch.state["phase"] = {"day": 2, "stage": "晚间休息"}
        ev = orch.dispatch(orch.player("F"), {"type": "bug_listen"})
        self.assertTrue(ev["ok"], ev)
        recordings = [m for m in orch.state["orch"]["hist"]["F"] if "窃听器录音" in m]
        self.assertEqual(len(recordings), 2)
        self.assertIn("此前交谈不追溯", recordings[0])
        self.assertNotIn("放置前的旧谈话", recordings[0])
        self.assertIn("放置后的新谈话", recordings[1])
        self.assertNotIn("放置前的旧谈话", recordings[1])

    def test_day1_single_case_daily_cap_flow_to_vote(self):
        """v4.0 每日封顶端到端：白天 A 杀 E 后，晚间窗口整体跳过（零调用）；次日清晨公告 +
        监控宣读 + 平民自动调查（不调 API、结果公开）。"""
        d = write_game_dir(self.root / "day1-two-cases", default_players())
        base = make_fake_chat(False)

        def chat_first_day(player, messages, *, on_token=None, reasoning_effort=None):
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            name = player.name

            def respond(actions):
                return ChatResult(name, ok=True, content=payload(notes="x", actions=actions), latency=0.01,
                                  usage={"total_tokens": 5}, finish_reason="stop")

            if "第1天" in title and "选房" in title:
                room = {"A": "书房", "E": "书房", "B": "监控室"}.get(name, "会客室")
                return respond([{"type": "choose_room", "room": room}])
            if title == "# 第1天 · 晚间休息":
                actions = []
                if name == "A":
                    actions = [{"type": "choose_room", "room": "C的卧室"}]
                elif name == "F":
                    actions = [{"type": "choose_room", "room": "监控室"}]
                return respond(actions)
            if "第1天" in title and "自由活动" in title and "房间行动" in title and name == "B":
                return respond([{"type": "set_device", "room": "监控室"}])
            if "第1天" in title and "谋杀窗口" in title and name == "A":
                if "自由活动" in title:
                    return respond([{"type": "murder", "target": "E", "weapon": "刀"}])
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = Orchestrator(d)
        orch.hooks = {"should_stop": lambda: orch.state["phase"] == {"day": 2, "stage": "投票"}}
        orig = orchestrator.chat
        orchestrator.chat = chat_first_day
        try:
            orch.run_game()
        finally:
            orchestrator.chat = orig

        self.assertEqual(orch.state["phase"], {"day": 2, "stage": "投票"})
        self.assertEqual([(c["victim"], c["period"]) for c in orch.state["cases"]],
                         [("E", "第1天自由活动")])
        self.assertEqual(orch.state["attempts"], [])
        # v4.0 每日封顶：白天已有人成功 → 晚间谋杀窗口整体跳过（A/B 均未被调用）
        self.assertFalse((d / "inbox" / "A" / "day1-晚间休息-谋杀窗口.md").exists())
        self.assertFalse((d / "inbox" / "B" / "day1-晚间休息-谋杀窗口.md").exists())
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("E 死亡", log)
        self.assertIn("监控公开宣读", log)
        # v4.0 清晨调查队列：平民 C（医师）自动结算、结果全员公告，不调 API
        self.assertFalse((d / "inbox" / "C" / "day2-案件调查.md").exists())
        self.assertIn("C 调查了 E 的案件", log)
        self.assertIn("死亡时间 = 第1天自由活动", log)
        self.assertIn("杀手线索", log)


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
        self.assertIn("两名杀手全部出局平民才获胜", hints)  # 武装指认内联口径（v4.0 双杀手）
        self.assertNotIn("指对平民立即获胜", hints)
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
            hooks["on_public"]("竞选票型：C→D、E→D")          # 连续结果行共享一个分割线块
            hooks["on_public"]("侦探竞选：D 当选侦探")
            hooks["on_public"]("夜宿会客室公开：A、B")          # 普通公告：关闭块，不带分割线
            hooks["on_private"]("B", "你用万能钥匙拉开了电闸")  # 席位调用外 → 行动回执块
        out = buf.getvalue()
        self.assertEqual(out.count("─" * 40), 3)              # 结果块前后 2 条 + 回执块头 1 条（尾部无后续输出则不补）
        self.assertLess(out.index("竞选票型"), out.index("侦探竞选"))
        self.assertIn("行动回执", out)
        self.assertIn("B用万能钥匙拉开了电闸", out)              # 私密回执转第三人称

    def test_console_summary_and_receipt_blocks(self):
        import contextlib
        import io
        from src.console import Ctx, make_hooks
        ctx = Ctx(None, None)
        hooks = make_hooks(ctx)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hooks["on_summary"]("晚间归宿", ["晚间归宿：A→A的卧室、B→B的卧室", "守夜成立：C 进入 D 的卧室"])
            hooks["on_private"]("B", "你无法进入（不知原因）")
            hooks["on_private"]("C", "你进入了书房")
            hooks["on_call_start"]("A", "晚间休息")
        out = buf.getvalue()
        self.assertIn("晚间归宿：A→A的卧室", out)      # 总结段上屏（此前只写 script.md）
        self.assertIn("守夜成立：C 进入 D 的卧室", out)
        self.assertEqual(out.count("行动回执"), 1)          # 连续回执归拢为一块，不窜行
        self.assertLess(out.index("行动回执"), out.index("B无法进入"))


class TestVillainChannel(MockGameTestBase):
    """v4.0 反派密谋频道：每天两次（自由活动开始前先于停电声明轮、晚间休息选房前）；
    两名杀手按行动顺序各发一段，互相可见、对他人完全不可见；一方出局后频道消失。"""

    def test_channel_twice_daily_and_private(self):
        d = write_game_dir(self.root / "vc1", default_players())
        orch = Orchestrator(d)
        orch.hooks = {"should_stop": lambda: orch.state["phase"] == {"day": 2, "stage": "投票"}}
        orig = orchestrator.chat
        orchestrator.chat = make_fake_chat(False)
        try:
            orch.run_game()
        finally:
            orchestrator.chat = orig
        # 第 1 天两次密谋：自由活动前 + 晚间休息选房前（两名杀手各被调用一次）
        for name in ("A", "B"):
            self.assertTrue((d / "inbox" / name / "day1-自由活动-密谋.md").exists(), name)
            self.assertTrue((d / "inbox" / name / "day1-晚间休息-密谋.md").exists(), name)
        for name in ("C", "D", "E", "F"):  # 平民完全不可见
            self.assertFalse((d / "inbox" / name / "day1-自由活动-密谋.md").exists(), name)
        # 按行动顺序各发一段：B 的简报能看到 A 刚发的密谋（互相可见）
        brief_b = (d / "inbox" / "B" / "day1-自由活动-密谋.md").read_text(encoding="utf-8")
        self.assertIn("A：A 的第1天密谋", brief_b)
        # 存档分线：双方私密 log 都有两段；script.md 剧透线有；公开日志没有
        log_a = (d / "private" / "A" / "log.md").read_text(encoding="utf-8")
        self.assertIn("A：A 的第1天密谋", log_a)
        self.assertIn("B：B 的第1天密谋", log_a)
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("密谋频道（自由活动前）｜A：A 的第1天密谋", script)
        self.assertIn("密谋频道（晚间休息前）｜B：B 的第1天密谋", script)
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertNotIn("密谋", log)

    def test_channel_disappears_when_partner_out(self):
        """A 第 3 天被票出后，频道消失（B 第 4 天起不再收到密谋调用）。"""
        d = write_game_dir(self.root / "vc2", TestCivilianWinEnding.players(self))
        self.run_game(d, killer_script=True)
        self.assertTrue((d / "inbox" / "A" / "day2-自由活动-密谋.md").exists())   # 第 2 天两名杀手都在
        self.assertTrue((d / "inbox" / "B" / "day2-晚间休息-密谋.md").exists())
        # A 第 3 天白天被票出：密谋频道在自由活动前（投票后）——第 3 天起只剩 B 一人，频道已消失
        self.assertFalse((d / "inbox" / "B" / "day3-自由活动-密谋.md").exists())
        self.assertFalse((d / "inbox" / "B" / "day4-晚间休息-密谋.md").exists())

    def test_decline_defers_to_partner(self):
        """窗口顺延口径：前排杀手放弃 → 顺延给同伙（其简报明写"本窗口你的同伙尚未动手"）；
        同伙一旦发动，本窗口结束。"""
        d = write_game_dir(self.root / "mw1", default_players())
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "第1天" in title and "选房" in title and name in ("A", "B", "E"):
                return ChatResult(name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "choose_room", "room": "书房"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "谋杀窗口" in title and day == 1 and name == "A":
                return ChatResult(name, ok=True, content=payload(notes="x", actions=[]),  # A 放弃
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "谋杀窗口" in title and day == 1 and name == "B" and '"type": "murder"' in briefing:
                return ChatResult(name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "murder", "target": "E", "weapon": "不明药剂"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_fn)
        s = orch.state
        self.assertTrue((d / "inbox" / "A" / "day1-自由活动-谋杀窗口.md").exists())  # A 先被询问
        brief_b = (d / "inbox" / "B" / "day1-自由活动-谋杀窗口.md").read_text(encoding="utf-8")
        self.assertIn("本窗口你的同伙尚未动手", brief_b)  # 顺延给 B 时简报明写
        self.assertTrue(any(c["victim"] == "E" and c["culprit"] == "B" for c in s["cases"]))  # B 发动
        self.assertEqual(s["cases"][0]["day"], 1)

    def test_daily_cap_skips_night_window(self):
        """每日封顶：白天已有人成功 → 晚间谋杀窗口整体跳过（两名杀手零调用）。"""
        d = write_game_dir(self.root / "mw2", default_players())
        base = make_fake_chat(False)

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "第1天" in title and "选房" in title and name in ("A", "E"):
                return ChatResult(name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "choose_room", "room": "书房"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "谋杀窗口" in title and day == 1 and name == "A" and '"type": "murder"' in briefing:
                return ChatResult(name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "murder", "target": "E", "weapon": "刀"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_fn)
        self.assertTrue(any(c["victim"] == "E" and c["day"] == 1 for c in orch.state["cases"]))  # 白天已得手
        # 晚间窗口整体跳过：无谋杀窗口简报（每日封顶）
        self.assertFalse((d / "inbox" / "A" / "day1-晚间休息-谋杀窗口.md").exists())
        self.assertFalse((d / "inbox" / "B" / "day1-晚间休息-谋杀窗口.md").exists())


class TestInvestigationQueueV4(MockGameTestBase):
    """v4.0 清晨调查队列：平民自动结算（不调 API）；反派私密菜单（篡改/调查/跳过）；
    篡改不封闭调查——篡改者可亲自把假结论公告出去。"""

    def test_villain_tamper_then_publicize(self):
        players = default_players()
        players[0] = P("A", "杀手", "医师", "刀", gender="男", physique=4)   # 反派医师持调查手段（×2）
        players[2] = P("C", "平民", "运动员", "急救箱", gender="女", physique=3)  # C 换无调查手段职业
        players[3] = P("D", "平民", "心理医生", "手电筒", gender="男", physique=3)  # 保持职业不重复
        d = write_game_dir(self.root / "iq1", players)
        base = make_fake_chat(False)
        calls = {"n": 0}

        def chat_fn(player, messages, *, on_token=None, reasoning_effort=None):
            name = player.name
            briefing = messages[-1]["content"]
            title = briefing.splitlines()[0]
            m = re.search(r"第(\d+)天", title)
            day = int(m.group(1)) if m else 1
            if "第2天" in title and "选房" in title and name in ("A", "E"):
                return ChatResult(name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "choose_room", "room": "书房"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "谋杀窗口" in title and day == 2 and name == "A" and '"type": "murder"' in briefing:
                return ChatResult(name, ok=True,
                                  content=payload(notes="x", actions=[{"type": "murder", "target": "E", "weapon": "刀"}]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            if "案件调查" in title and name == "A":
                calls["n"] += 1
                # 第一次：篡改（菜单含三选一）；第二次：正常调查（本案已被篡改，菜单只剩调查/跳过）
                act = {"type": "tamper_investigation", "case": "E"} if calls["n"] == 1 \
                    else {"type": "investigate", "case": "E"}
                if calls["n"] == 1:
                    self.assertIn("tamper_investigation", briefing)
                else:
                    self.assertNotIn("tamper_investigation", briefing)  # 一次定型：篡改项消失
                return ChatResult(name, ok=True, content=payload(notes="x", actions=[act]),
                                  latency=0.01, usage={"total_tokens": 5}, finish_reason="stop")
            return base(player, messages, on_token=on_token, reasoning_effort=reasoning_effort)

        orch = self.run_game_with(d, chat_fn)
        s = orch.state
        case = s["cases"][0]
        self.assertEqual(calls["n"], 2)                  # 篡改后仍被再次询问
        self.assertEqual(len(case["falsified"]), 2)      # 随机篡改两项
        self.assertTrue(case["investigated"])            # 随后调查定型
        self.assertTrue(any("改动两项" in m for m in s["orch"]["hist"]["A"]))  # 私下告知明细
        log = (d / "public" / "log.md").read_text(encoding="utf-8")
        self.assertIn("A 调查了 E 的案件", log)          # 假结论由篡改者亲自公告
        script = (d / "script.md").read_text(encoding="utf-8")
        self.assertIn("篡改案件调查（E案）", script)


if __name__ == "__main__":
    unittest.main()
