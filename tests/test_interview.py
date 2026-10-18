"""采访录制器（src/interview.py）单测：记忆档案组装、死亡截断/--at 冻结、采访循环归档。

不打真实 API：采访循环注入假 chat_fn；存档 fixture 为手工构造的最小对局目录。
"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import json
import tempfile
import unittest
from pathlib import Path

from src.interview import _label_stage, _parse_timeline, build_memory, run_interview
from src.providers import ChatResult, Player


def make_game(root: Path) -> Path:
    """最小对局存档：A（平民，存活）/ B（杀手，第1天晚间被投出……改为：B 第2天投票出局）。"""
    d = root / "game"
    (d / "public").mkdir(parents=True)
    (d / "rules.md").write_text("（规则书快照）", encoding="utf-8")
    state = {
        "seed": 1,
        "players": [
            {"name": "A", "identity": "平民", "profession": "医师", "alive": True},
            {"name": "B", "identity": "杀手", "profession": "化学家", "alive": False},
        ],
        "cases": [],
        "orch": {
            "feed": [
                "〔第1天·开局准备〕欢迎来到暴风夜。",
                "**A**：我先说两句。",            # 发言行不打戳 → 继承开局准备
                "〔第1天·自由活动〕选房结果：A→书房",
                "- 第 2/6 天开始。今日行动顺序：B → A",   # 清晨状态行不打戳 → 归第2天案件调查
                "〔第2天·投票〕投票结果：B 2票——B 出局",
                "**B**：这是我最后的遗言。",
                "〔第3天·案件调查〕无新案件。",
            ],
            "hist": {
                "A": ["〔第1天·自由活动〕书房内交谈：A：你好；B：别过来"],
                "B": ["〔第1天·自由活动〕书房内交谈：A：你好；B：别过来",
                      "〔第2天·投票〕你被投票出局了"],
            },
            "out_day": {"B": 2},
        },
    }
    (d / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    # B 的密谋存档（密谋只进 private/<席位>/log.md）
    b_priv = d / "private" / "B"
    b_priv.mkdir(parents=True)
    (b_priv / "log.md").write_text(
        "\n## 第1天 · 自由活动（反派密谋）\n\nB：今晚你动手。\n", encoding="utf-8")
    (b_priv / "notes.md").write_text("## 第1天\n- 【身份】杀手\n", encoding="utf-8")
    # outbox payloads
    for seat, day, label, speech, inner in [
        ("B", 1, "竞选声明", "我参选。", "当侦探方便带节奏"),
        ("B", 1, "自由活动-密谋", "今晚你动手。", "别让A看见"),
        ("B", 2, "投票", "", "完了，票型不对"),
        ("B", 2, "遗言", "这是我最后的遗言。", "认了"),
        ("A", 1, "公共发言", "我先说两句。", "先观察"),
    ]:
        ob = d / "outbox" / seat
        ob.mkdir(parents=True, exist_ok=True)
        (ob / f"day{day}-{label}.md").write_text("```json\n" + json.dumps(
            {"speech": speech, "inner": inner, "notes": "", "actions": []},
            ensure_ascii=False) + "\n```", encoding="utf-8")
    return d


class TestTimelineParse(unittest.TestCase):
    def test_inherit_and_morning(self):
        entries = _parse_timeline([
            "〔第1天·投票〕投票结果：X 出局",
            "**X**：我的遗言。",                 # 无戳 → 继承（1, 投票）
            "- 第 2/6 天开始。今日行动顺序：…",   # 清晨状态行 → (2, 案件调查)
        ])
        self.assertEqual(entries[0][:2], (1, "投票"))
        self.assertEqual(entries[1][:2], (1, "投票"))
        self.assertEqual(entries[2][:2], (2, "案件调查"))

    def test_label_stage(self):
        self.assertEqual(_label_stage("竞选声明"), "公共发言")
        self.assertEqual(_label_stage("自由活动-交谈-书房"), "自由活动")
        self.assertEqual(_label_stage("武装指认"), "投票")
        self.assertIsNone(_label_stage("毁证机会"))       # 标签不含阶段
        self.assertIsNone(_label_stage("赠送回应-A"))


class TestBuildMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = make_game(Path(self.tmp.name))
        self.state = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_channels_complete(self):
        """B（杀手）：含公开发言、私密事件、密谋（log.md 补入）、自己的 speech/inner 全文与最终笔记。"""
        mem, cut = build_memory(self.dir, self.state, "B", "B")
        self.assertIn("**A**：我先说两句。", mem)               # 公开发言
        self.assertIn("书房内交谈：A：你好；B：别过来", mem)      # 他听到的交谈（hist）
        self.assertIn("（反派密谋）B：今晚你动手。", mem)          # 密谋（private log.md）
        self.assertIn("你说：「我参选。」", mem)                  # 自己的发言
        self.assertIn("你的内心：当侦探方便带节奏", mem)          # 自己的内心
        self.assertIn("【身份】杀手", mem)                       # 最终笔记
        self.assertEqual(cut, (2, "投票"))                       # 死亡/出局截断点

    def test_death_cut(self):
        """出局后的公开事件不进入记忆（"他没有经历过"）。"""
        mem, _ = build_memory(self.dir, self.state, "B", "B")
        self.assertIn("你被投票出局了", mem)
        self.assertNotIn("无新案件", mem)                        # 第3天公告被截掉
        self.assertIn("已于第2天 · 投票出局", mem)
        # --full 则不截断
        mem_full, cut_full = build_memory(self.dir, self.state, "B", "B", full=True)
        self.assertIn("无新案件", mem_full)
        self.assertIsNone(cut_full)

    def test_survivor_no_cut(self):
        mem, cut = build_memory(self.dir, self.state, "A", "A")
        self.assertIsNone(cut)
        self.assertIn("无新案件", mem)

    def test_at_freeze(self):
        """--at 冻结：两编都只保留冻结点之前的内容。"""
        mem, cut = build_memory(self.dir, self.state, "B", "B", at=(1, "公共发言"))
        self.assertEqual(cut, (1, "公共发言"))
        self.assertIn("我参选", mem)
        self.assertNotIn("你被投票出局了", mem)                  # 第一编截断
        self.assertNotIn("完了，票型不对", mem)                  # 第二编截断
        self.assertNotIn("笔记本", mem)                          # 冻结模式不含最终笔记

    def test_no_inner(self):
        mem, _ = build_memory(self.dir, self.state, "B", "B", with_inner=False)
        self.assertNotIn("你的内心", mem)
        self.assertIn("你说：「我参选。」", mem)


class TestRunInterview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_multi_turn_and_archive(self):
        player = Player(name="B", model="mock", base_url="http://localhost", api_key_env="X")
        seen = []

        def fake_chat(p, messages, *, on_token=None, **kw):
            seen.append([m["role"] for m in messages])
            return ChatResult(p.name, ok=True, content=f"回答{len(seen)}")

        archive = Path(self.tmp.name) / "interview.md"
        archive.write_text("# 采访记录\n", encoding="utf-8")
        qs = iter(["第一个问题", "第二个问题", "exit"])
        rounds = run_interview(player, "system", "记忆档案", archive,
                               chat_fn=fake_chat, input_fn=lambda _p: next(qs))
        self.assertEqual(rounds, 2)
        # 消息结构：system + 记忆 + 逐轮交替
        self.assertEqual(seen[0], ["system", "user", "user"])
        self.assertEqual(seen[1], ["system", "user", "user", "assistant", "user"])
        text = archive.read_text(encoding="utf-8")
        self.assertIn("**制作人**：第一个问题", text)
        self.assertIn("**B**：回答2", text)

    def test_failed_call_not_recorded(self):
        player = Player(name="B", model="mock", base_url="http://localhost", api_key_env="X")

        def bad_chat(p, messages, *, on_token=None, **kw):
            return ChatResult(p.name, ok=False, error="boom")

        archive = Path(self.tmp.name) / "interview.md"
        qs = iter(["问题", "exit"])
        rounds = run_interview(player, "system", "记忆", archive,
                               chat_fn=bad_chat, input_fn=lambda _p: next(qs))
        self.assertEqual(rounds, 0)


if __name__ == "__main__":
    unittest.main()
