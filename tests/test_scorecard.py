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
        # 消极局戏剧性含"零出手消极局"扣分项；有未遂的活跃对局不扣这项
        from tools.scorecard import load_game
        a2 = analyze(load_game(self.sel / "1002"))
        _, parts2 = drama(a2)
        self.assertTrue(any("零出手消极局" in p for p in parts2))
        a2["attempts"] = 1
        _, active_parts = drama(a2)
        self.assertFalse(any("零出手消极局" in p for p in active_parts))

    def test_flow_briefing_skips_multiline_speech(self):
        from tools.scorecard import flow_briefing
        log = """## 第3天 · 公共发言
**狄西**：第一段发言

第二段不应进入简报
第三段也不应进入简报
〔第3天·公共发言〕奇米 把一件道具交给了 狄西

## 第3天 · 投票
- 〔第3天·投票〕投票结果：奇米 出局
"""
        text = "\n".join(flow_briefing(log))
        self.assertIn("奇米 把一件道具交给了 狄西", text)
        self.assertIn("投票结果：奇米 出局", text)
        self.assertNotIn("第一段发言", text)
        self.assertNotIn("第二段不应进入简报", text)
        self.assertNotIn("第三段也不应进入简报", text)

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
        """平衡统计分组：完全按版本分组、不做任何跨版本合并（迭代期每次更新几乎都改规则）；
        lite 规则集单独成组（总体一池、不细分版本）、永不并入高级规则池；旧档缺 is_lite_rule 字段视为高级规则。"""
        from tools.scorecard import is_lite, rules_group
        self.assertEqual(rules_group({"rules_version": "v3.8"}), "v3.8")
        self.assertEqual(rules_group({"rules_version": "v3.9"}), "v3.9")
        self.assertEqual(rules_group({"rules_version": "v3.10"}), "v3.10")
        self.assertEqual(rules_group({"rules_version": "v3.30"}), "v3.30")
        self.assertEqual(rules_group({"rules_version": "v3.5"}), "v3.5")
        self.assertEqual(rules_group({}), "≤v3.4")
        # lite 规则集：不与 full 同池；lite 总体一池、不按版本细分
        self.assertEqual(rules_group({"rules_version": "v3.12", "is_lite_rule": True}), "lite（失忆版）")
        self.assertEqual(rules_group({"rules_version": "v3.12", "is_lite_rule": True}),
                         rules_group({"rules_version": "v3.13", "is_lite_rule": True}))
        self.assertNotEqual(rules_group({"rules_version": "v3.12", "is_lite_rule": True}),
                            rules_group({"rules_version": "v3.12"}))
        # is_lite_rule 缺字段/None → False（旧档默认高级规则）
        self.assertFalse(is_lite({}))
        self.assertFalse(is_lite({"is_lite_rule": None}))
        self.assertTrue(is_lite({"is_lite_rule": True}))

    def test_min_version_filter(self):
        """--min-version：多局报告只统计指定版本及以后的存档；无法解析版本的旧档被排除。"""
        import json as _json
        run_mock_game(self.sel / "1001", killer_script=True)
        run_mock_game(self.sel / "1002", killer_script=False)
        # 1001 手工打上 v3.9 标记；1002 无版本字段（推断 ≤v3.4）
        sj = self.sel / "1001" / "state.json"
        st = _json.loads(sj.read_text(encoding="utf-8"))
        st["rules_version"] = "v3.9"
        sj.write_text(_json.dumps(st, ensure_ascii=False), encoding="utf-8")
        text = multi_report(self.sel, min_version="v3.8")
        self.assertIn("过滤掉 1 局", text)
        self.assertIn("共 1 局", text)
        self.assertIn("1001", text)
        self.assertNotIn("1002", text)
        # 全部不满足时的提示
        text = multi_report(self.sel, min_version="v9.9")
        self.assertIn("没有对局存档满足", text)
        # 版本参数格式错误
        self.assertIn("无法识别", multi_report(self.sel, min_version="3.8"))

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

    def test_aggression_windows_include_first_day_from_v328(self):
        def aggression(version):
            state = {
                "seed": 1,
                "rules_version": version,
                "phase": {"day": 3, "stage": "投票"},
                "players": [],
                "cases": [{"victim": "E", "day": 1, "period": "第1天自由活动"}],
                "attempts": [],
                "orch": {},
            }
            game = {"dir": Path("mock"), "state": state, "feed": [], "script": "", "host": []}
            return analyze(game)["aggression"]

        self.assertEqual(aggression("v3.27"), 0.5)   # 旧版：第 2 天两个阶段
        self.assertEqual(aggression("v3.28"), 0.25)  # 新版：首日两个阶段也计入


if __name__ == "__main__":
    unittest.main()
