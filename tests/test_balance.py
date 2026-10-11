"""M5 平衡场景回归：平民扎堆（3 人恒同室）与卧室蹲守，及四条反制路径（停电/舞台魔术/延时装置/开锁夜袭）。

对照 rules/dev-plan.md M5 验收：两种"龟缩"打法都必须存在可触发的反制。
引擎级确定性测试，不经 API；机制单点用例见 test_engine.py，本文件做场景化串联。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import unittest

from src.engine import Engine
from test_engine import P, default_players, goto, mk_state, player


def stack_day2(eng, rooms):
    goto(eng, "自由活动", day=2)
    ev = eng.enter_rooms(rooms)
    assert ev["ok"], ev
    return ev


class TestCivilianStacking(unittest.TestCase):
    """场景 A：平民扎堆——多名平民每个自由活动都挤同一间房。"""

    def test_a1_full_room_excludes_killer(self):
        """3 平民占满书房（上限 3）后，杀手连门都进不去——扎堆的第一层保护来自容量。"""
        eng = Engine(mk_state())
        stack_day2(eng, {"A": "A的卧室", "B": "B的卧室", "C": "书房", "D": "书房", "E": "书房", "F": "F的卧室"})
        ev = eng.enter_rooms({"A": "书房", "B": "B的卧室", "C": "书房", "D": "书房", "E": "书房", "F": "F的卧室"})
        self.assertFalse(ev["ok"])
        self.assertIn("人数超过上限", ev["error"])

    def test_a2_vanilla_murder_rejected_in_lit_room(self):
        """亮房里 2 名平民在场，普通谋杀被驳回（在场非反派 >1）。"""
        eng = Engine(mk_state())
        stack_day2(eng, {"A": "书房", "B": "B的卧室", "C": "书房", "D": "D的卧室", "E": "书房", "F": "F的卧室"})
        ev = eng.murder("A", "E", weapon="刀")
        self.assertFalse(ev["ok"])
        self.assertIn("非反派", ev["error"])

    def test_a3_counter_blackout(self):
        """反制·停电：帮凶声明停电后书房变黑暗，谋杀人数限制放宽到 ≤2，两人扎堆被撕开。"""
        players = default_players()
        players[1] = P("B", "帮凶", "运动员", "毒药", gender="女", physique=2)  # 运动员反派技能含【暴力破解】
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=2)
        ev = eng.declare_blackout("B")
        self.assertTrue(ev["ok"], ev)
        stack_day2(eng, {"A": "书房", "B": "B的卧室", "C": "书房", "D": "D的卧室", "E": "书房", "F": "F的卧室"})
        self.assertTrue(eng.state["power_out"])
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")
        self.assertEqual(ev["case"]["victim"], "E")
        self.assertIn("有人被谋杀", ev["private"]["C"])  # 同房平民只知有人遇害，不知凶手

    def test_a4_counter_stage_magic(self):
        """反制·舞台魔术：亮房、多名非反派在场也能强杀，且不可防御。"""
        eng = Engine(mk_state())
        stack_day2(eng, {"A": "书房", "B": "B的卧室", "C": "书房", "D": "D的卧室", "E": "书房", "F": "F的卧室"})
        self.assertFalse(eng.murder("A", "E", weapon="刀")["ok"])  # 普通谋杀先被扎堆挡下
        ev = eng.stage_magic("A", "E")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(player(eng, "E")["alive"])
        self.assertIn("凶手就在当时在场的人之中", ev["private"]["C"])

    def test_a5_stack_cannot_survive_the_night(self):
        """扎堆只能维持白天：卧室上限 2，夜里必然有人落单 → 反制·开锁夜袭收尾。"""
        eng = Engine(mk_state())
        player(eng, "A")["items"].append({"name": "万能钥匙", "uses_left": 2, "available_day": 1})
        stack_day2(eng, {"A": "A的卧室", "B": "B的卧室", "C": "书房", "D": "书房", "E": "书房", "F": "F的卧室"})
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"A": "E的卧室"})  # C/D/E 无法继续同室（卧室上限 2），E 落单
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "E的卧室")
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["room"], "E的卧室")


class TestBedroomCamping(unittest.TestCase):
    """场景 B：卧室蹲守——平民白天夜里都不出自己卧室。"""

    def test_b1_locked_bedroom_blocks_killer_without_means(self):
        """没有开锁手段，杀手连蹲守者的门都进不去。"""
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=2)
        ev = eng.enter_rooms({"A": "C的卧室", "B": "B的卧室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        self.assertFalse(ev["ok"])
        self.assertIn("开锁", ev["error"])

    def test_b2_counter_unlock_night_raid(self):
        """反制·开锁夜袭：杀手用【破门而入】夜闯独居卧室，完成击杀。"""
        players = default_players()
        players[0] = P("A", "杀手", "黑客", "刀", gender="男", physique=4)  # 黑客通用技能【破门而入】
        eng = Engine(mk_state(players=players))
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"A": "E的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "E的卧室")
        self.assertEqual(eng.state["skill_uses"]["A"]["破门而入"], 0)
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["victim"], "E")

    def test_b3_counter_delayed_device(self):
        """反制·延时装置：机械师白天破门潜入蹲守者卧室设装置，夜里目标独自回房即触发。"""
        eng = Engine(mk_state())  # B = 帮凶 机械师（通用技能【破门而入】，反派技能【延时装置】）
        goto(eng, "自由活动", day=2)
        ev = eng.enter_rooms({"A": "书房", "B": "C的卧室", "C": "书房", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(eng.state["skill_uses"]["B"]["破门而入"], 0)  # 开锁费由有手段的进入者支付
        ev = eng.set_device("B", "C的卧室")
        self.assertTrue(ev["ok"], ev)
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({})  # 全员默认回自己卧室 → C 独自回房触发装置
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev.get("device_triggers"), ["C"])
        case = eng.state["cases"][0]
        self.assertEqual(case["wound"], "机关")
        self.assertEqual(case["culprit"], "B")
        self.assertFalse(player(eng, "C")["alive"])

    def test_b4_paired_bedroom_beats_night_raid(self):
        """反制的边界：两人合住一间卧室，夜袭无法进入（记谋杀失败一次），无人遇害。"""
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"D": "C的卧室", "A": "C的卧室"}, invited={"D": "C"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "A的卧室")
        self.assertTrue(eng.state["stage_murder_used"])
        self.assertTrue(any(a["target"] is None for a in eng.state["attempts"]))
        self.assertFalse(eng.state["cases"])


if __name__ == "__main__":
    unittest.main()
