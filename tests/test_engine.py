"""M3 规则引擎单元测试（对照 rules/rules-6.md §7/§8/§9 逐分支）。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import copy
import unittest
from pathlib import Path

from src.engine import Engine, load_state
from src.gamedata import ITEM_USES

ROOT = Path(__file__).resolve().parent.parent


def P(name, identity, profession, item, gender="男", physique=2, nationality="中国"):
    return {
        "id": name,
        "name": name,
        "gender": gender,
        "physique": physique,
        "nationality": nationality,
        "identity": identity,
        "profession": profession,
        "item": item,
        "item_uses_left": ITEM_USES[item],
        "alive": True,
    }


def default_players():
    return [
        P("A", "杀手", "魔术师", "刀", gender="男", physique=4),
        P("B", "帮凶", "机械师", "毒药", gender="女", physique=2),
        P("C", "平民", "医师", "急救箱", gender="女", physique=3),
        P("D", "平民", "运动员", "手电筒", gender="男", physique=3),
        P("E", "平民", "黑客", "万能钥匙", gender="男", physique=2),
        P("F", "平民", "建筑师", "窃听器", gender="女", physique=2),
    ]


def mk_state(players=None, order=None, day=1, stage="开局准备", safe_item="消音手枪"):
    players = players if players is not None else default_players()
    return {
        "seed": 1,
        "ruleset": "rules-6.md",
        "mode": "6人局",
        "phase": {"day": day, "stage": stage},
        "order": order or [p["name"] for p in players],
        "safe_item": safe_item,
        "unused_professions": [],
        "unused_items": [],
        "players": players,
    }


def goto(eng, stage, day=None, limit=60):
    for _ in range(limit):
        ph = eng.state["phase"]
        if ph["stage"] == stage and (day is None or ph["day"] == day):
            return ph
        ev = eng.advance()
        if not ev["ok"]:
            raise AssertionError(f"advance 被驳回: {ev}")
    raise AssertionError(f"未到达 stage={stage} day={day}，当前 {eng.state['phase']}")


def free2(eng, rooms):
    goto(eng, "自由活动", day=2)
    ev = eng.enter_rooms(rooms)
    assert ev["ok"], ev
    return ev


def items_of(eng, name):
    p = next(p for p in eng.state["players"] if p["name"] == name)
    return {e["name"]: e for e in p["items"]}


def player(eng, name):
    return next(p for p in eng.state["players"] if p["name"] == name)


class TestSampleState(unittest.TestCase):
    def test_load_sample_and_extend(self):
        st = load_state(ROOT / "game" / "000042" / "state.json")
        eng = Engine(st)
        self.assertEqual(st["phase"], {"day": 1, "stage": "开局准备"})
        self.assertTrue(st["locks"]["地下室"])
        self.assertFalse(st["locks"]["书房"])
        self.assertTrue(st["locks"]["桑娜·克劳德的卧室"])
        self.assertEqual(st["skill_uses"]["索尔·杰普特"], {"看破伪装": 2, "舞台魔术": 1})
        self.assertEqual(st["skill_uses"]["奇米"], {"破门而入": 1, "延时装置": 1})
        killer = next(p for p in st["players"] if p["identity"] == "杀手")
        self.assertEqual(killer["items"][0]["name"], "刀")

    def test_sample_day1_flow_and_rotation(self):
        eng = Engine(load_state(ROOT / "game" / "000042" / "state.json"))
        order0 = list(eng.state["order"])
        goto(eng, "案件调查", day=2)
        self.assertEqual(eng.state["order"], order0[1:] + order0[:1])
        self.assertEqual(eng.state["detective"], order0[1])
        self.assertIsNone(eng.state["winner"])


class TestStageFlow(unittest.TestCase):
    def test_day1_stages(self):
        eng = Engine(mk_state())
        seq = []
        for _ in range(6):
            seq.append(eng.state["phase"]["stage"])
            eng.advance()
        self.assertEqual(seq, ["开局准备", "公共发言", "自由活动", "晚间休息", "案件调查", "公共发言"])
        self.assertEqual(eng.state["phase"], {"day": 2, "stage": "投票"})

    def test_day1_murder_forbidden_and_state_untouched(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        rooms = {"A": "书房", "E": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"}
        self.assertTrue(eng.enter_rooms(rooms)["ok"])
        snap = copy.deepcopy(eng.state)
        ev = eng.murder("A", "E")
        self.assertFalse(ev["ok"])
        self.assertIn("第 1 天", ev["error"])
        self.assertEqual(eng.state, snap)

    def test_day4_locks_auto_open(self):
        eng = Engine(mk_state())
        goto(eng, "案件调查", day=4)
        self.assertFalse(eng.state["locks"]["地下室"])
        self.assertFalse(eng.state["locks"]["书房"])
        self.assertTrue(eng.state["locks"]["A的卧室"])

    def test_maingod_on_day5_end(self):
        eng = Engine(mk_state())
        winner = None
        for _ in range(60):
            ev = eng.advance()
            if eng.winner:
                winner = eng.winner
                self.assertEqual(ev["type"], "game_over")
                break
        self.assertEqual(winner, "主神")
        self.assertIn("第 5 天", eng.state["win_reason"])
        ev = eng.advance()
        self.assertFalse(ev["ok"])


class TestMurderConditions(unittest.TestCase):
    def test_only_killer_and_once_per_stage(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        self.assertFalse(eng.murder("B", "E")["ok"])
        self.assertFalse(eng.murder("C", "E")["ok"])
        ev = eng.murder("A", "E")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(eng.murder("A", "E")["ok"])

    def test_presence_limit_light_room(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "C": "书房", "E": "书房", "B": "监控室", "D": "D的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "E")
        self.assertFalse(ev["ok"])
        self.assertIn("非反派", ev["error"])

    def test_unarmed_physique_fail_then_accomplice_present(self):
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="女", physique=1)
        eng = Engine(mk_state(players=players))
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "E")
        self.assertFalse(ev["ok"])
        self.assertIn("空手", ev["error"])

        eng = Engine(mk_state(players=[P("A", "杀手", "魔术师", "刀", gender="女", physique=1)] + default_players()[1:]))
        free2(eng, {"A": "书房", "E": "书房", "B": "书房", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "E")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")

    def test_unarmed_gender_condition(self):
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="男", physique=1)
        eng = Engine(mk_state(players=players))
        free2(eng, {"A": "书房", "C": "书房", "B": "监控室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "C")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder_attempt")

    def test_dark_room_unarmed_no_extra_condition(self):
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="女", physique=1)
        eng = Engine(mk_state(players=players))
        ev = free2(eng, {"A": "地下室", "C": "地下室", "E": "地下室", "B": "监控室", "D": "D的卧室", "F": "F的卧室"})
        self.assertFalse(eng.state["locks"]["地下室"])
        ev = eng.murder("A", "E")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")
        case = eng.state["cases"][0]
        self.assertEqual(case["wound"], "空手")
        self.assertEqual(case["looted"], [])
        self.assertFalse(case["evidence_vulnerable"])

    def test_dark_presence_two_civilians_allowed_via_blackout(self):
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "毒药", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        self.assertTrue(eng.declare_blackout("B")["ok"])
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "书房", "C": "书房", "E": "书房", "B": "监控室", "D": "D的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)

    def test_weapon_murder_consumed_and_loot(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")
        self.assertNotIn("刀", items_of(eng, "A"))
        self.assertIn("万能钥匙", items_of(eng, "A"))
        self.assertEqual(items_of(eng, "A")["万能钥匙"]["uses_left"], 2)
        case = ev["case"]
        self.assertEqual(case["wound"], "利器")
        self.assertEqual(case["looted"], ["万能钥匙"])
        self.assertTrue(case["evidence_vulnerable"])

    def test_env_weapon_unconditional_and_wound_label(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        self.assertFalse(eng.murder("A", "E", weapon="电线")["ok"])
        ev = eng.murder("A", "E", weapon="雕像")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["wound"], "雕像")
        self.assertIn("刀", items_of(eng, "A"))

    def test_wrong_stage_murder_rejected(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=1)
        snap = copy.deepcopy(eng.state)
        ev = eng.murder("A", "E")
        self.assertFalse(ev["ok"])
        self.assertEqual(eng.state, snap)


class TestSelfDefense(unittest.TestCase):
    def rooms_for(self, eng, target_room="书房", target="E", extra=None):
        rooms = {"A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"}
        rooms["A"] = target_room
        rooms[target] = target_room
        rooms.update(extra or {})
        return free2(eng, rooms)

    def test_knife_vs_club_defended_weapon_not_consumed(self):
        players = default_players()
        players[4] = P("E", "平民", "黑客", "棒球棍", gender="男", physique=2)
        eng = Engine(mk_state(players=players, safe_item="消音手枪"))
        self.rooms_for(eng)
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"])
        self.assertEqual(ev["type"], "murder_attempt")
        self.assertIn("棒球棍", items_of(eng, "E"))
        self.assertNotIn("刀", items_of(eng, "A"))
        self.assertEqual(eng.state["last_murder_day"], 2)
        self.assertTrue(eng.state["stage_murder_used"])
        self.assertIn("无法分辨", ev["private"]["A"])
        self.assertIn("幸存", ev["private"]["E"])

    def test_club_vs_knife_and_gun_defended(self):
        for weapon, defender in [("棒球棍", "刀"), ("棒球棍", "消音手枪"), ("刀", "消音手枪")]:
            players = default_players()
            players[0] = P("A", "杀手", "魔术师", weapon, gender="男", physique=4)
            players[4] = P("E", "平民", "黑客", defender, gender="男", physique=2)
            safe = "毒药" if weapon != "毒药" and defender != "毒药" else "消音手枪"
            if defender == "消音手枪" or weapon == "消音手枪":
                safe = "毒药"
            eng = Engine(mk_state(players=players, safe_item=safe))
            self.rooms_for(eng)
            ev = eng.murder("A", "E", weapon=weapon)
            self.assertEqual(ev["type"], "murder_attempt", (weapon, defender, ev))
            self.assertIn(defender, items_of(eng, "E"))

    def test_gun_attack_no_weapon_defense(self):
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "消音手枪", gender="男", physique=4)
        players[4] = P("E", "平民", "黑客", "刀", gender="男", physique=2)
        eng = Engine(mk_state(players=players, safe_item="棒球棍"))
        self.rooms_for(eng)
        ev = eng.murder("A", "E", weapon="消音手枪")
        self.assertEqual(ev["type"], "murder", ev)

    def test_poison_only_defended_by_kit_or_dodge(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=2)
        ev = eng.transfer("B", "A", "毒药")
        self.assertTrue(ev["ok"], ev)
        players_rooms = {"A": "书房", "E": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"}
        free2(eng, players_rooms)
        ev = eng.murder("A", "E", weapon="毒药")
        self.assertEqual(ev["type"], "murder", ev)

        eng = Engine(mk_state())
        goto(eng, "公共发言", day=2)
        eng.transfer("B", "A", "毒药")
        free2(eng, {"A": "书房", "D": "书房", "B": "监控室", "C": "C的卧室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "D", weapon="毒药")
        self.assertEqual(ev["type"], "murder_attempt", ev)
        self.assertEqual(eng.state["skill_uses"]["D"]["敏捷闪避"], 0)

    def test_kit_defends_environment_weapon(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "C": "书房", "B": "监控室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "C", weapon="雕像")
        self.assertEqual(ev["type"], "murder_attempt", ev)
        self.assertNotIn("急救箱", items_of(eng, "C"))

    def test_dodge_defends_unarmed(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "D": "书房", "B": "监控室", "C": "C的卧室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "D")
        self.assertEqual(ev["type"], "murder_attempt", ev)
        self.assertEqual(eng.state["skill_uses"]["D"]["敏捷闪避"], 0)
        self.assertIn("手电筒", items_of(eng, "D"))

    def test_savior_kit_from_roommate(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "地下室", "C": "地下室", "E": "地下室", "B": "监控室", "D": "D的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "E", savior="C")
        self.assertEqual(ev["type"], "murder_attempt", ev)
        self.assertNotIn("急救箱", items_of(eng, "C"))

    def test_failed_attempt_stage_end_announcement(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "D": "书房", "B": "监控室", "C": "C的卧室", "E": "E的卧室", "F": "F的卧室"})
        eng.murder("A", "D")
        ev = eng.advance()
        self.assertEqual(ev["to"]["stage"], "晚间休息")
        self.assertTrue(any("D 遇袭但幸存" in a for a in ev["public"]["announcements"]))

    def test_night_retry_only_if_day_not_succeeded(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "D": "书房", "B": "监控室", "C": "C的卧室", "E": "E的卧室", "F": "F的卧室"})
        eng.murder("A", "D")
        goto(eng, "晚间休息", day=2)
        self.assertFalse(eng.state["stage_murder_used"])

        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        eng.murder("A", "E")
        goto(eng, "晚间休息", day=2)
        self.assertTrue(eng.state["stage_murder_used"])
        ev = eng.murder("A", "F")
        self.assertFalse(ev["ok"])


class TestDeviceAndMagic(unittest.TestCase):
    def test_device_set_trigger_and_no_stage_chance(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        self.assertFalse(eng.set_device("B", "监控室")["ok"])

        eng = Engine(mk_state())
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.set_device("B", "监控室")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(eng.set_device("B", "书房")["ok"])
        goto(eng, "自由活动", day=3)
        ev = eng.enter_rooms({"A": "书房", "B": "书房", "C": "监控室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev.get("device_triggers"), ["C"])
        case = eng.state["cases"][0]
        self.assertEqual(case["wound"], "机关")
        self.assertEqual(case["culprit"], "B")
        self.assertEqual(case["victim"], "C")
        self.assertIn("急救箱", items_of(eng, "C"))
        self.assertFalse(eng.state["stage_murder_used"])
        self.assertEqual(eng.state["last_murder_day"], 3)
        self.assertFalse(eng.state["devices"]["监控室"]["active"])

    def test_device_not_triggered_with_company(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        eng.set_device("B", "监控室")
        goto(eng, "自由活动", day=3)
        ev = eng.enter_rooms({"A": "书房", "B": "书房", "C": "监控室", "D": "监控室", "E": "E的卧室", "F": "F的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertNotIn("device_triggers", ev)
        self.assertTrue(player(eng, "C")["alive"])
        self.assertTrue(eng.state["devices"]["监控室"]["active"])

    def test_stage_magic_with_witnesses_undefendable(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "C": "书房", "D": "书房", "B": "监控室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.stage_magic("A", "D")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(player(eng, "D")["alive"])
        self.assertEqual(eng.state["skill_uses"]["D"]["敏捷闪避"], 1)
        self.assertEqual(ev["case"]["wound"], "空手")
        self.assertIn("凶手就在当时在场的人之中", ev["private"]["C"])
        self.assertFalse(ev["case"]["evidence_vulnerable"])
        self.assertFalse(eng.murder("A", "C")["ok"])
        self.assertEqual(eng.state["skill_uses"]["A"]["舞台魔术"], 0)

    def test_stage_magic_rejects(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        self.assertFalse(eng.stage_magic("B", "E")["ok"])
        players = default_players()
        players[0] = P("A", "平民", "魔术师", "刀", gender="男", physique=4)
        eng = Engine(mk_state(players=players))
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        self.assertFalse(eng.stage_magic("A", "E")["ok"])


class TestVote(unittest.TestCase):
    def votes_to(self, target, voters, abstain=()):
        return {v: target for v in voters} | {v: None for v in abstain}

    def test_threshold_strictly_over_half(self):
        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        ev = eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "B"})
        self.assertTrue(ev["ok"], ev)
        self.assertIsNone(ev["public"]["out"])

        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        ev = eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        self.assertEqual(ev["public"]["out"], "A")
        self.assertEqual(ev["public"]["identity"], "杀手")
        self.assertEqual(ev["public"]["profession"], "魔术师")

    def test_tie_no_out(self):
        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        ev = eng.resolve_vote({"C": "A", "D": "A", "E": "B", "F": "B"})
        self.assertIsNone(ev["public"]["out"])
        self.assertTrue(all(p["alive"] for p in eng.state["players"]))

    def test_killer_out_succession_then_civilian_win(self):
        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        ev = eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        self.assertEqual(ev["public"]["out"], "A")
        self.assertEqual(player(eng, "B")["identity"], "杀手")
        self.assertIn("继任", ev["private"]["B"])
        self.assertIsNone(eng.winner)
        self.assertIn("刀", {e["name"] for e in eng.state["sealed_items"]})

        goto(eng, "投票", day=3)
        ev = eng.resolve_vote({"C": "B", "D": "B", "E": "B", "F": "B"})
        self.assertEqual(ev["public"]["out"], "B")
        self.assertEqual(ev.get("winner"), "平民")

    def test_one_civilian_left_villains_win(self):
        players = default_players()
        players[2]["alive"] = False
        players[3]["alive"] = False
        eng = Engine(mk_state(players=players, day=2, stage="投票"))
        ev = eng.resolve_vote({"A": "E", "B": "E", "F": "E", "E": "A"})
        self.assertEqual(ev["public"]["out"], "E")
        self.assertEqual(ev.get("winner"), "反派")
        self.assertIn("只剩 1 人", ev["win_reason"])

    def test_memory_implant(self):
        players = default_players()
        players[1] = P("B", "帮凶", "心理医生", "毒药", gender="女", physique=2)
        votes = {"A": "C", "B": "C", "C": "A", "D": "A", "E": "A", "F": None}
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        ev = eng.resolve_vote(votes)
        self.assertIsNone(ev["public"]["out"])

        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        ev = eng.resolve_vote(votes, implant={"by": "B", "voter": "F", "target": "A"})
        self.assertEqual(ev["public"]["out"], "A")
        self.assertIn("票数被更改过", ev["public"]["announcements"])
        self.assertEqual(eng.state["skill_uses"]["B"]["记忆植入"], 0)

    def test_memory_implant_civilian_rejected(self):
        players = default_players()
        players[5] = P("F", "平民", "心理医生", "窃听器", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        snap = copy.deepcopy(eng.state)
        ev = eng.resolve_vote({"C": "A"}, implant={"by": "F", "voter": "E", "target": "A"})
        self.assertFalse(ev["ok"])
        self.assertEqual(eng.state, snap)

    def test_armed_accusation(self):
        players = default_players()
        players[4] = P("E", "平民", "黑客", "棒球棍", gender="男", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "B", "F": "B"})
        self.assertFalse(eng.armed_accusation("E", "A")["ok"])

        goto(eng, "投票", day=4)
        eng.resolve_vote({"C": "A", "D": "A", "E": "B", "F": "B"})
        self.assertFalse(eng.armed_accusation("C", "A")["ok"])
        ev = eng.armed_accusation("E", "A")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev.get("winner"), "平民")
        self.assertIn("武装指认", eng.state["win_reason"])

    def test_armed_accusation_wrong_target(self):
        players = default_players()
        players[4] = P("E", "平民", "黑客", "棒球棍", gender="男", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=4)
        eng.resolve_vote({"C": "A", "D": "A", "E": "B", "F": "B"})
        ev = eng.armed_accusation("E", "F")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(player(eng, "E")["alive"])
        self.assertIsNone(eng.winner)
        self.assertTrue(any("指认错误" in a for a in ev["public"]["announcements"]))

    def test_armed_accusation_requires_no_out_vote(self):
        players = default_players()
        players[4] = P("E", "平民", "黑客", "棒球棍", gender="男", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=4)
        eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        ev = eng.armed_accusation("E", "B")
        self.assertFalse(ev["ok"])

    def test_assign_sealed_next_day_available(self):
        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        self.assertIn("刀", {e["name"] for e in eng.state["sealed_items"]})
        ev = eng.assign_sealed({"刀": "C"})
        self.assertTrue(ev["ok"], ev)
        entry = items_of(eng, "C")["刀"]
        self.assertEqual(entry["available_day"], 3)
        self.assertEqual(eng.state["sealed_items"], [])

        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        ev = eng.assign_sealed({"刀": None})
        self.assertTrue(ev["ok"])
        self.assertEqual(ev["public"]["destroyed"], ["刀"])


class TestCountdownAndNightRaid(unittest.TestCase):
    def test_no_kill_countdown_civilian_win(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "D": "书房", "B": "监控室", "C": "C的卧室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "D")
        self.assertEqual(ev["type"], "murder_attempt")
        self.assertEqual(eng.state["last_murder_day"], 2)
        winner_ev = None
        for _ in range(30):
            ev = eng.advance()
            if eng.winner:
                winner_ev = ev
                break
        self.assertEqual(eng.winner, "平民")
        self.assertIn("无杀倒计时", winner_ev["win_reason"])
        self.assertEqual(eng.state["phase"], {"day": 5, "stage": "案件调查"})

    def test_night_bedroom_raid_relaxed_physique(self):
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "晚间休息", day=2)
        player(eng, "A")["items"].append({"name": "万能钥匙", "uses_left": 2, "available_day": 1})
        ev = eng.night_assign({"A": "F的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "F的卧室")
        ev = eng.murder("A", "F")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")
        self.assertEqual(ev["case"]["wound"], "空手")

    def test_night_raid_full_bedroom_counts_as_failure(self):
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        self.assertEqual(eng.state["order"], ["B", "C", "D", "E", "F", "A"])
        ev = eng.night_assign({"D": "C的卧室", "A": "C的卧室"}, invited={"D": "C"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["D"], "C的卧室")
        self.assertEqual(ev["positions"]["A"], "A的卧室")
        self.assertTrue(eng.state["stage_murder_used"])
        self.assertEqual(eng.state["last_murder_day"], 2)
        self.assertTrue(any(a["target"] is None for a in eng.state["attempts"]))

    def test_night_room_capacity_one(self):
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"C": "书房", "D": "书房"})
        self.assertEqual(ev["positions"]["C"], "书房")
        self.assertEqual(ev["positions"]["D"], "D的卧室")
        self.assertIn("无法进入", ev["private"]["D"])

    def test_killer_inside_blocks_invitee(self):
        order = ["F", "A", "B", "C", "D", "E"]
        players = default_players()
        players[0]["items"] = [{"name": "刀", "uses_left": 1, "available_day": 1}]
        eng = Engine(mk_state(players=players, order=order))
        goto(eng, "晚间休息", day=2)
        self.assertEqual(eng.state["order"], ["A", "B", "C", "D", "E", "F"])
        eng.state["players"][0]["items"].append({"name": "万能钥匙", "uses_left": 2, "available_day": 1})
        ev = eng.night_assign({"A": "C的卧室", "D": "C的卧室"}, invited={"D": "C"})
        self.assertEqual(ev["positions"]["A"], "C的卧室")
        self.assertEqual(ev["positions"]["D"], "D的卧室")


class TestMorningReportAndInvestigation(unittest.TestCase):
    def kill_e_day2(self, eng, weapon="刀"):
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "E", weapon=weapon)
        assert ev["ok"], ev
        return ev

    def test_morning_report(self):
        eng = Engine(mk_state())
        self.kill_e_day2(eng)
        ev = goto_ev = None
        while True:
            ev = eng.advance()
            if eng.state["phase"] == {"day": 3, "stage": "案件调查"}:
                break
        text = "；".join(ev["public"]["announcements"])
        self.assertIn("E 死亡", text)
        self.assertIn("书房", text)
        self.assertIn("黑客", text)
        monitoring = ev["public"]["monitoring"]
        self.assertIn("第2天自由活动", monitoring)
        self.assertIn("A", monitoring["第2天自由活动"]["书房"])
        self.assertEqual(eng.state["sealed_items"], [])

    def test_morning_seals_corpse_items_when_witnessed(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "地下室", "C": "地下室", "E": "地下室", "B": "监控室", "D": "D的卧室", "F": "F的卧室"})
        ev = eng.murder("A", "E")
        self.assertTrue(ev["ok"])
        goto(eng, "案件调查", day=3)
        sealed = {e["name"]: e for e in eng.state["sealed_items"]}
        self.assertIn("万能钥匙", sealed)
        self.assertEqual(sealed["万能钥匙"]["from"], "E")

    def test_investigate_truth_and_falsify(self):
        players = default_players()
        players[1] = P("B", "帮凶", "医师", "毒药", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        self.kill_e_day2(eng)
        ev = eng.falsify("B", 1, "伪造痕迹")
        self.assertTrue(ev["ok"], ev)
        goto(eng, "案件调查", day=3)
        ev = eng.investigate("C", 1, "武器伤痕")
        self.assertEqual(ev["private"]["C"]["result"], "利器")
        ev = eng.investigate("C", 1, "死亡时间")
        self.assertEqual(ev["private"]["C"]["result"], "第2天晚间休息")
        self.assertEqual(eng.state["skill_uses"]["C"]["现场调查"], 0)
        ev = eng.investigate("C", 1, "尸体转移")
        self.assertFalse(ev["ok"])

        eng = Engine(mk_state())
        self.kill_e_day2(eng)
        goto(eng, "案件调查", day=3)
        ev = eng.investigate("C", 1, "死亡时间")
        self.assertEqual(ev["private"]["C"]["result"], "第2天自由活动")
        ev = eng.investigate("C", 1, "尸体转移")
        self.assertEqual(ev["private"]["C"]["result"], False)

    def test_falsify_rejected_when_witnessed(self):
        players = default_players()
        players[1] = P("B", "帮凶", "医师", "毒药", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        free2(eng, {"A": "地下室", "C": "地下室", "E": "地下室", "B": "监控室", "D": "D的卧室", "F": "F的卧室"})
        eng.murder("A", "E")
        ev = eng.falsify("B", 1, "伪造痕迹")
        self.assertFalse(ev["ok"])

    def test_move_body(self):
        players = default_players()
        players[1] = P("B", "帮凶", "运动员", "毒药", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        self.kill_e_day2(eng)
        ev = eng.move_body("B", 1, "地下室")
        self.assertTrue(ev["ok"], ev)
        case = eng.state["cases"][0]
        self.assertEqual(case["room"], "地下室")
        self.assertTrue(case["body_moved"])
        goto(eng, "案件调查", day=3)
        ev = eng.investigate("C", 1, "尸体转移")
        self.assertEqual(ev["private"]["C"]["result"], True)
        self.assertFalse(eng.move_body("B", 1, "书房")["ok"])

    def test_profile_points_to_culprit(self):
        players = default_players()
        players[5] = P("F", "平民", "犯罪侧写师", "窃听器", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        self.kill_e_day2(eng)
        goto(eng, "案件调查", day=3)
        ev = eng.profile("F", 1, "体格")
        self.assertEqual(ev["private"]["F"]["result"], "强壮")
        ev = eng.profile("F", 1, "性别")
        self.assertFalse(ev["ok"])

    def test_detective_monitoring_privilege(self):
        eng = Engine(mk_state())
        self.kill_e_day2(eng)
        goto(eng, "案件调查", day=3)
        det = eng.state["detective"]
        ev = eng.detective_monitoring(det, "第2天自由活动", ["书房", "监控室"])
        self.assertTrue(ev["ok"], ev)
        self.assertIn("A", ev["private"][det]["书房"])
        self.assertIn("B", ev["private"][det]["监控室"])
        others = [n for n in ["A", "C", "E"] if n != det]
        self.assertFalse(eng.detective_monitoring(others[0], "第2天自由活动", ["书房"])["ok"])


class TestMonitoringLocksLedger(unittest.TestCase):
    def test_monitoring_records_entry_only(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "书房", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        recs = eng.state["monitoring"]["第1天自由活动"]
        self.assertEqual(set(recs["书房"]), {"A", "C"})
        self.assertEqual(recs["监控室"], ["B"])
        self.assertEqual(recs, eng.state["monitoring_true"]["第1天自由活动"])

    def test_tamper_monitoring(self):
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "毒药", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "书房", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.tamper_monitoring("B", "第1天自由活动", "书房", "remove", "A")
        self.assertTrue(ev["ok"], ev)
        recs = eng.state["monitoring"]["第1天自由活动"]["书房"]
        self.assertEqual(recs, ["C"])
        self.assertIn("A", eng.state["monitoring_true"]["第1天自由活动"]["书房"])
        self.assertFalse(eng.tamper_monitoring("B", "第1天自由活动", "书房", "add", "B")["ok"])

        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "毒药", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        eng.tamper_monitoring("B", "第1天自由活动", "书房", "replace", "A", "B")
        self.assertEqual(eng.state["monitoring"]["第1天自由活动"]["书房"], ["B"])
        ev = eng.view_monitoring("B", "第1天自由活动", "书房")
        self.assertEqual(ev["private"]["B"], {"第1天自由活动": {"书房": ["B"]}})

    def test_tamper_civilian_rejected(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "E": "监控室", "F": "F的卧室"})
        self.assertFalse(eng.tamper_monitoring("E", "第1天自由活动", "书房", "add", "E")["ok"])

    def test_unlock_permanent_and_bedroom_relock(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        ev = eng.enter_rooms({"E": "地下室", "A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(eng.state["locks"]["地下室"])
        self.assertEqual(eng.state["skill_uses"]["E"]["破门而入"], 0)
        goto(eng, "自由活动", day=2)
        self.assertFalse(eng.state["locks"]["地下室"])

        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        ev = eng.enter_rooms({"E": "C的卧室", "A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(eng.state["locks"]["C的卧室"])
        eng.advance()
        self.assertTrue(eng.state["locks"]["C的卧室"])

    def test_enter_locked_room_without_means_rejected(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        snap = copy.deepcopy(eng.state)
        ev = eng.enter_rooms({"A": "地下室", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        self.assertFalse(ev["ok"])
        self.assertEqual(eng.state, snap)

    def test_room_capacity_rejected(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        ev = eng.enter_rooms({"A": "书房", "B": "书房", "C": "书房", "D": "书房", "E": "E的卧室", "F": "F的卧室"})
        self.assertFalse(ev["ok"])
        self.assertIn("上限", ev["error"])

    def test_transfer_keeps_uses_and_limits(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=2)
        ev = eng.transfer("D", "C", "手电筒")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(items_of(eng, "C")["手电筒"]["uses_left"], 2)
        self.assertFalse(eng.transfer("D", "C", "手电筒")["ok"])
        self.assertFalse(eng.transfer("D", "E", "手电筒")["ok"])
        ev = eng.transfer("C", "F", "手电筒", accept=False)
        self.assertTrue(ev["ok"])
        self.assertFalse(ev["accepted"])
        self.assertNotIn("手电筒", items_of(eng, "F"))

    def test_open_safe(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"E": "书房", "A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "F": "F的卧室"})
        ev = eng.open_safe("E")
        self.assertTrue(ev["ok"], ev)
        self.assertIn("消音手枪", items_of(eng, "E"))
        self.assertIn("打开了保险箱", ev["private"]["A"])
        self.assertFalse(eng.open_safe("E")["ok"])

    def test_flashlight_and_blackout(self):
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "毒药", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=1)
        ev = eng.enter_rooms({"D": "地下室", "E": "地下室", "A": "书房", "B": "监控室", "C": "C的卧室", "F": "F的卧室"})
        self.assertTrue(ev["ok"], ev)
        ev = eng.use_flashlight("D")
        self.assertTrue(ev["ok"], ev)
        self.assertIn("地下室", eng.state["flashlight_lit"])
        self.assertEqual(items_of(eng, "D")["手电筒"]["uses_left"], 1)
        eng.advance()
        self.assertEqual(eng.state["flashlight_lit"], [])

        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        ev = eng.declare_blackout("B")
        self.assertTrue(ev["ok"], ev)
        self.assertTrue(eng.state["power_out"])
        self.assertFalse(eng.declare_blackout("B")["ok"])
        goto(eng, "晚间休息", day=2)
        eng.night_assign({"E": "地下室"})
        ev = eng.fix_generator("E")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(eng.state["power_out"])

    def test_blackout_civilian_rejected(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=2)
        self.assertFalse(eng.declare_blackout("D")["ok"])

    def test_see_through(self):
        players = default_players()
        players[5] = P("F", "平民", "心理医生", "窃听器", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "书房", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        goto(eng, "公共发言", day=2)
        ev = eng.see_through("F", "C", {"visited_rooms": ["书房"], "profession": "医师"})
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(ev["private"]["F"]["lies"])
        ev = eng.see_through("F", "C", {"denied_rooms": ["书房"]})
        self.assertTrue(ev["private"]["F"]["lies"])
        self.assertEqual(eng.state["skill_uses"]["F"]["看破伪装"], 0)
        self.assertFalse(eng.see_through("F", "C", {"profession": "医师"})["ok"])

    def test_seal_room_blocks_night_entry(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "F": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室"})
        ev = eng.seal_room("F", allowed=["C"])
        self.assertTrue(ev["ok"], ev)
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"E": "书房", "C": "书房"})
        self.assertEqual(ev["positions"]["E"], "E的卧室")
        self.assertEqual(ev["positions"]["C"], "书房")

    def test_passage(self):
        players = default_players()
        players[1] = P("B", "帮凶", "建筑师", "毒药", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        ev = eng.set_passage("B", "书房")
        self.assertTrue(ev["ok"], ev)
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "监控室", "B": "B的卧室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.use_passage("B")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(eng.state["positions"]["B"], "书房")
        recs = eng.state["monitoring_true"]["第2天自由活动"]
        self.assertNotIn("B", recs.get("书房", []))
        self.assertFalse(eng.use_passage("B")["ok"])

        eng = Engine(mk_state(players=players))
        eng.set_passage("B", "书房")
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "监控室", "B": "B的卧室", "C": "书房", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.use_passage("B")
        self.assertTrue(ev["ok"])
        self.assertTrue(ev["failed"])
        self.assertTrue(eng.state["passage"]["broken"])

    def test_bug(self):
        eng = Engine(mk_state())
        ev = eng.place_bug("F", "书房")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(eng.place_bug("F", "监控室")["ok"])
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "C的卧室", "D": "D的卧室", "E": "E的卧室", "F": "F的卧室"})
        ev = eng.bug_listen("F")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["room"], "书房")
        self.assertFalse(eng.bug_listen("F")["ok"])


if __name__ == "__main__":
    unittest.main()
