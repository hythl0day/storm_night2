"""补拍演出器（src/scene.py）单测：参演名单（冻结时点在场）、多轮对话循环与归档。

不打真实 API：run_scene 注入假 chat_fn。
"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import tempfile
import unittest
from pathlib import Path

from src.providers import ChatResult, Player
from src.scene import participants, run_scene


def _state():
    return {
        "order": ["A", "B", "C"],
        "players": [
            {"name": "A", "alive": True},
            {"name": "B", "alive": False},
            {"name": "C", "alive": True},
        ],
        "cases": [{"victim": "B", "day": 2, "stage": "晚间休息"}],  # B 第2天晚间被杀
        "orch": {"out_day": {}},
    }


class TestParticipants(unittest.TestCase):
    def test_alive_at_freeze(self):
        """冻结在第2天晚间休息之前：B 还在场；冻结在那之后或赛后：B 不在。"""
        s = _state()
        self.assertEqual(participants(s, (1, "开局准备")), ["A", "B", "C"])
        self.assertEqual(participants(s, (2, "自由活动")), ["A", "B", "C"])
        self.assertEqual(participants(s, (3, "案件调查")), ["A", "C"])
        self.assertEqual(participants(s, None), ["A", "C"])  # 赛后按终局存活


class TestRunScene(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _cast(self, name):
        p = Player(name=name, model="mock", base_url="http://localhost", api_key_env="X")
        return {"player": p, "system": "sys", "memory": f"{name}的记忆"}

    def test_multi_round_dialogue_and_archive(self):
        """两轮 × 两席：后发言者能看到本场此前台词；归档含全部台词。"""
        seen_users = []

        def fake_chat(p, messages, *, on_token=None, **kw):
            seen_users.append((p.name, messages[-1]["content"]))
            return ChatResult(p.name, ok=True, content=f"{p.name}的台词{len(seen_users)}")

        archive = Path(self.tmp.name) / "scene.md"
        archive.write_text("# 补拍素材\n", encoding="utf-8")
        n = run_scene([self._cast("A"), self._cast("B")], 2, archive, chat_fn=fake_chat)
        self.assertEqual(n, 4)
        # A 是第一个开口的；B 的第一轮能看到 A 的台词
        self.assertIn("你是第一个", seen_users[0][1])
        self.assertIn("**A**：A的台词1", seen_users[1][1])
        # 第二轮 A 能看到第一轮两句
        self.assertIn("**B**：B的台词2", seen_users[2][1])
        text = archive.read_text(encoding="utf-8")
        self.assertIn("**A**：A的台词1", text)
        self.assertIn("**B**：B的台词4", text)
        self.assertIn("## 第2轮", text)

    def test_failed_call_skipped(self):
        """单席调用失败：跳过该席本轮并落"补拍失败"，不中断场景。"""
        def flaky(p, messages, *, on_token=None, **kw):
            if p.name == "A":
                return ChatResult(p.name, ok=False, error="boom")
            return ChatResult(p.name, ok=True, content="B开口了")

        archive = Path(self.tmp.name) / "scene.md"
        archive.write_text("# 补拍素材\n", encoding="utf-8")
        n = run_scene([self._cast("A"), self._cast("B")], 1, archive, chat_fn=flaky)
        self.assertEqual(n, 1)
        text = archive.read_text(encoding="utf-8")
        self.assertIn("（补拍失败：boom）", text)
        self.assertIn("**B**：B开口了", text)


class TestPresets(unittest.TestCase):
    def test_loop_opening_preset(self):
        """轮回开场预设：冻结在开局准备（同开场惊魂），desc 含册子措辞变化与「主神」，
        note 写明纪律豁免边界（可谈既视感与称谓，不编造往局细节）。"""
        from src.scene import SCENES
        p = SCENES["轮回开场"]
        self.assertEqual(p["at"], (1, "开局准备"))
        self.assertIn("主神", p["desc"])
        self.assertIn("主持人", p["desc"])  # 措辞变化的对照
        self.assertIn("不要编造具体的往局细节", p["note"])


if __name__ == "__main__":
    unittest.main()
