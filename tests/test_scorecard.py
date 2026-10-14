"""tools/scorecard.py 评分卡测试（mock 对局存档 → 单局/多局报告，不经 API）。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import shutil
import tempfile
import unittest
from pathlib import Path

import src.orchestrator as orchestrator_mod
from src.orchestrator import Orchestrator
from test_engine import default_players
from test_orchestrator_game import make_fake_chat, write_game_dir
from tools.scorecard import analyze, drama, multi_report, rules_version, single_report


def run_mock_game(dest: Path, killer_script: bool) -> None:
    """用编排 mock 集成跑完整一局，存档放到 dest（模拟人工挑选进 selected_games/）。"""
    tmp = dest.parent / "_build"
    if tmp.exists():
        shutil.rmtree(tmp)
    d = write_game_dir(tmp, default_players())
    orch = Orchestrator(d)
    orig = orchestrator_mod.chat
    orchestrator_mod.chat = make_fake_chat(killer_script)
    try:
        orch.run_game()
    finally:
        orchestrator_mod.chat = orig
    shutil.move(str(d), str(dest))
    shutil.rmtree(tmp)


class TestScorecard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sel = Path(self.tmp.name) / "selected_games"
        self.sel.mkdir(parents=True)

    def test_single_report_full_game(self):
        """单局报告：基本台账 + 流程简报 + 戏剧性评分分项 + 主角候选 + 成本。"""
        run_mock_game(self.sel / "1001", killer_script=True)
        text = single_report(self.sel / "1001")
        self.assertIn("===== 单局报告：1001 =====", text)
        self.assertIn("平民 胜", text)
        self.assertIn("—— 席位 ——", text)
        self.assertIn("—— 案件 ——", text)
        self.assertIn("—— 流程简报 ——", text)
        self.assertIn("—— 戏剧性评分：", text)
        self.assertIn("主角弧线候选", text)
        self.assertIn("—— 成本 ——", text)
        # 指标包字段核对：mock 剧本为 A/B 依次作案（E、F 遇害），C 当选侦探
        import json as _json
        from tools.scorecard import load_game
        a = analyze(load_game(self.sel / "1001"))
        self.assertEqual(a["murders"], 2)
        self.assertTrue(a["elected"])
        self.assertEqual(a["detectives"], ["C"])
        self.assertTrue(a["succession"])  # A 出局后 B 继任杀手
        self.assertTrue(a["complete"])
        score, parts = drama(a)
        self.assertGreater(score, 0)
        self.assertTrue(parts)

    def test_multi_report_and_incomplete_game(self):
        """多局报告：版本分组胜率 + 机制利用率 + 多样性；中断局标记且不计入胜负率。"""
        run_mock_game(self.sel / "1001", killer_script=True)   # 平民胜（投票淘汰）
        run_mock_game(self.sel / "1002", killer_script=False)  # 平民胜（无杀倒计时）
        # 中断局：只 deal 不跑（Orchestrator 未实例化，无 script.md，winner 为空）
        d = write_game_dir(self.sel / "_build", default_players())
        shutil.move(str(d), str(self.sel / "1003"))
        text = multi_report(self.sel)
        self.assertIn("共 3 局：完整 2 / 中断 1", text)
        self.assertIn("—— 逐局一览 ——", text)
        self.assertIn("—— 胜率平衡", text)
        self.assertIn("—— 机制利用率", text)
        self.assertIn("竞选流产率：0/2", text)  # mock 均选出 C
        self.assertIn("—— 多样性 ——", text)
        self.assertIn("—— 成本 ——", text)
        # 版本分组：mock 存档无 rules_version/table_items → 推断为旧版
        self.assertIn("≤v3.4", text)
        self.assertIn("平民 100%", text)  # 两局完整局均为平民胜
        # 胜负路径覆盖两种
        self.assertIn("投票淘汰", text)
        self.assertIn("无杀倒计时", text)
        # 消极局戏剧性含"零谋杀消极局"扣分项
        from tools.scorecard import load_game
        a2 = analyze(load_game(self.sel / "1002"))
        _, parts2 = drama(a2)
        self.assertTrue(any("零谋杀消极局" in p for p in parts2))

    def test_empty_dir_and_non_archive(self):
        """空目录与非存档目录的友好提示。"""
        text = multi_report(self.sel)
        self.assertIn("没有对局存档", text)
        (self.sel / "not_a_game").mkdir()
        self.assertIn("不是对局存档", single_report(self.sel / "not_a_game"))

    def test_rules_version_field(self):
        """v3.5 起 deal 写入 rules_version；缺字段旧档按 table_items 推断。"""
        self.assertEqual(rules_version({"rules_version": "v3.5"}), "v3.5")
        self.assertEqual(rules_version({"table_items": []}), "v3.5")
        self.assertEqual(rules_version({}), "≤v3.4")

    def test_rules_group_pooling(self):
        """平衡统计分组：v3.8 起合并为一组（v3.9/v3.10 未改规则），更早版本保持独立。"""
        from tools.scorecard import rules_group
        self.assertEqual(rules_group({"rules_version": "v3.8"}), "v3.8+")
        self.assertEqual(rules_group({"rules_version": "v3.9"}), "v3.8+")
        self.assertEqual(rules_group({"rules_version": "v3.10"}), "v3.8+")
        self.assertEqual(rules_group({"rules_version": "v3.5"}), "v3.5")
        self.assertEqual(rules_group({}), "≤v3.4")

    def test_multi_report_aggression_section(self):
        """多局报告新增「反派主动性」段（出手率/未遂防御/再出手/零谋杀局）；反派功能技能行。"""
        run_mock_game(self.sel / "1001", killer_script=True)
        run_mock_game(self.sel / "1002", killer_script=False)
        text = multi_report(self.sel)
        self.assertIn("—— 反派主动性 ——", text)
        self.assertIn("平均出手率", text)
        self.assertIn("未遂防御分布", text)
        self.assertIn("零谋杀局：1/2", text)  # 1002 全程消极
        self.assertIn("反派功能技能", text)


if __name__ == "__main__":
    unittest.main()
