"""M5 平衡场景回归（v3 版）：平民扎堆（恒同室）与会客室蹲守，及反制路径（停电/舞台魔术/延时装置/开锁夜袭/密道）。

对照 docs/dev-plan.md M5 验收与 docs/rules-6.md v3：两种"龟缩"打法都必须存在可触发的反制。
v3 起白天卧室清场，蹲守的唯一去处是会客室（不限容、不设监控、永不黑暗）。
引擎级确定性测试，不经 API；机制单点用例见 test_engine.py，本文件做场景化串联。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import unittest

from src.deal import ROOT, VALVE_PROFESSIONS, deal, load_personas, validate
from src.engine import Engine
from src.gamedata import VILLAIN_ITEMS
from src.providers import load_config
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
        stack_day2(eng, {"A": "会客室", "B": "会客室", "C": "书房", "D": "书房", "E": "书房", "F": "会客室"})
        ev = eng.enter_rooms({"A": "书房", "B": "会客室", "C": "书房", "D": "书房", "E": "书房", "F": "会客室"})
        self.assertFalse(ev["ok"])
        self.assertIn("人数超过上限", ev["error"])

    def test_a2_vanilla_murder_rejected_in_lit_room(self):
        """亮房里 2 名平民在场，普通谋杀被驳回（在场非反派 >1）。"""
        eng = Engine(mk_state())
        stack_day2(eng, {"A": "书房", "B": "会客室", "C": "书房", "D": "会客室", "E": "书房", "F": "会客室"})
        ev = eng.murder("A", "E", weapon="刀")
        self.assertFalse(ev["ok"])
        self.assertIn("非反派", ev["error"])

    def test_a3_counter_blackout(self):
        """反制·停电：帮凶声明停电后书房变黑暗，谋杀人数限制放宽到 ≤2，两人扎堆被撕开。"""
        players = default_players()
        players[1] = P("B", "帮凶", "运动员", "不明药剂", gender="女", physique=2)  # 运动员反派技能含【暴力破解】
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=2)
        ev = eng.declare_blackout("B")
        self.assertTrue(ev["ok"], ev)
        stack_day2(eng, {"A": "书房", "B": "会客室", "C": "书房", "D": "会客室", "E": "书房", "F": "会客室"})
        self.assertTrue(eng.state["power_out"])
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")
        self.assertEqual(ev["case"]["victim"], "E")
        self.assertIn("有人被谋杀", ev["private"]["C"])  # 同房平民只知有人遇害，不知凶手

    def test_a4_counter_stage_magic(self):
        """反制·舞台魔术：亮房、多名非反派在场也能强杀，且不可防御。"""
        eng = Engine(mk_state())
        stack_day2(eng, {"A": "书房", "B": "会客室", "C": "书房", "D": "会客室", "E": "书房", "F": "会客室"})
        self.assertFalse(eng.murder("A", "E", weapon="刀")["ok"])  # 普通谋杀先被扎堆挡下
        ev = eng.stage_magic("A", "E")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(player(eng, "E")["alive"])
        self.assertIn("凶手就在当时在场的人之中", ev["private"]["C"])

    def test_a5_stack_cannot_survive_the_night(self):
        """扎堆只能维持白天：卧室上限 2，夜里必然有人落单 → 反制·开锁夜袭收尾。"""
        eng = Engine(mk_state())
        player(eng, "A")["items"].append({"name": "万能钥匙", "uses_left": 2, "available_day": 1})
        stack_day2(eng, {"A": "会客室", "B": "会客室", "C": "书房", "D": "书房", "E": "书房", "F": "会客室"})
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"A": "E的卧室"})  # C/D/E 无法继续同室（卧室上限 2），E 落单
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "E的卧室")
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["room"], "E的卧室")


class TestHallCamping(unittest.TestCase):
    """场景 B（v3）：会客室蹲守——白天卧室已清场，消极玩家的唯一去处是不限容的大厅。"""

    def test_b1_solo_hall_camping_is_exposed(self):
        """大厅无门无锁不限容：独自蹲大厅时杀手直接走入即可动手（亮房 1 平民满足条件）。"""
        eng = Engine(mk_state())
        stack_day2(eng, {"A": "会客室", "E": "会客室", "B": "书房", "C": "书房", "D": "监控室", "F": "监控室"})
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["room"], "会客室")

    def test_b2_counter_unlock_night_raid(self):
        """反制·开锁夜袭：杀手用【破门而入】夜闯独居卧室，完成击杀（夜间机制不变）。"""
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

    def test_b3_counter_delayed_device_in_hall(self):
        """反制·延时装置：装置设在会客室，蹲守者独自滞留大厅即触发（大厅不限容，装置只看"单独"）。"""
        eng = Engine(mk_state())  # B = 帮凶 机械师（反派技能【延时装置】）
        stack_day2(eng, {"A": "书房", "B": "会客室", "C": "书房", "D": "监控室", "E": "监控室", "F": "会客室"})
        ev = eng.set_device("B", "会客室")
        self.assertTrue(ev["ok"], ev)
        goto(eng, "自由活动", day=3)
        ev = eng.enter_rooms({"A": "书房", "B": "书房", "C": "会客室", "D": "监控室", "E": "监控室", "F": "书房"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev.get("device_triggers"), ["C"])  # C 独自滞留大厅触发
        case = eng.state["cases"][0]
        self.assertEqual(case["wound"], "机关")
        self.assertEqual(case["room"], "会客室")
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


class TestDealVillainGuarantees(unittest.TestCase):
    """deal 反派平衡保证：凶手固定持有「万能钥匙」（公开规则）；反派队必有宽域职业（含黑客，保密）。"""

    @classmethod
    def setUpClass(cls):
        cls.players, _ = load_config(str(ROOT / "players.toml"))
        cls.personas = load_personas(ROOT / "personas.toml")

    def test_villain_guarantees_across_seeds(self):
        for seed in range(1, 41):
            st = deal(seed, self.players, self.personas, None)
            self.assertEqual(validate(st), [], f"seed={seed} 分配校验失败")
            killer = next(p for p in st["players"] if p["identity"] == "杀手")
            accom = next(p for p in st["players"] if p["identity"] == "帮凶")
            self.assertIn("万能钥匙", killer.get("extra_items", []), f"seed={seed} 杀手未固定持有万能钥匙")
            self.assertEqual(len(accom.get("extra_items", [])), 1, f"seed={seed} 帮凶反派道具数错误")
            self.assertIn(accom["extra_items"][0], VILLAIN_ITEMS, f"seed={seed} 帮凶反派道具非法")
            for p in st["players"]:
                if p["identity"] == "平民":
                    self.assertNotIn(p["item"], ["万能钥匙"] + VILLAIN_ITEMS,
                                     f"seed={seed} 平民 {p['name']} 抽到 {p['item']}（禁入平民池）")
            profs = {p["profession"] for p in st["players"] if p["identity"] in ("杀手", "帮凶")}
            self.assertTrue(profs & set(VALVE_PROFESSIONS), f"seed={seed} 反派队无宽域职业")

    def test_deal_still_deterministic(self):
        self.assertEqual(deal(42, self.players, self.personas, None),
                         deal(42, self.players, self.personas, None))


if __name__ == "__main__":
    unittest.main()
