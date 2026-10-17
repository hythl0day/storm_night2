"""M3 规则引擎单元测试（对照 docs/rules-6.md §7/§8/§9 逐分支）。

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
from src.engine.state import is_dark
from src.gamedata import ITEM_USES

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "state_sample.json"  # 冻结的 deal 样例（seed 42），与 game/ 解耦


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
        P("B", "帮凶", "机械师", "不明药剂", gender="女", physique=2),
        P("C", "平民", "医师", "急救箱", gender="女", physique=3),
        P("D", "平民", "运动员", "手电筒", gender="男", physique=3),
        P("E", "平民", "黑客", "万能钥匙", gender="男", physique=2),
        P("F", "平民", "建筑师", "窃听器", gender="女", physique=2),
    ]


def mk_state(players=None, order=None, day=1, stage="开局准备", safe_item="左轮手枪"):
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
        st = load_state(FIXTURE)
        eng = Engine(st)
        self.assertEqual(st["phase"], {"day": 1, "stage": "开局准备"})
        self.assertTrue(st["locks"]["监控室"])   # v3.2：监控室上锁（首开需开锁手段）
        self.assertFalse(st["locks"]["地下室"])  # v3.2：地下室取消门锁
        self.assertFalse(st["locks"]["书房"])
        self.assertTrue(st["locks"]["桑娜·克劳德的卧室"])
        for p in st["players"]:
            skills = st["skill_uses"][p["name"]]
            self.assertTrue(skills, f"{p['name']} 缺少技能台账")
            self.assertTrue(all(n > 0 for n in skills.values()))
        killer = next(p for p in st["players"] if p["identity"] == "杀手")
        self.assertIn(killer["items"][0]["name"], {"左轮手枪", "刀", "棒球棍", "不明药剂"})

    def test_sample_day1_flow_and_rotation(self):
        eng = Engine(load_state(FIXTURE))
        order0 = list(eng.state["order"])
        goto(eng, "案件调查", day=2)
        self.assertEqual(eng.state["order"], order0[1:] + order0[:1])
        self.assertIsNone(eng.state["detective"])  # v3.3：未举行竞选则无侦探
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

    def test_day1_free_murder_allowed(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        rooms = {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"}
        self.assertTrue(eng.enter_rooms(rooms)["ok"])
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")
        self.assertEqual(ev["case"]["day"], 1)
        self.assertEqual(ev["case"]["period"], "第1天自由活动")
        self.assertTrue(eng.state["stage_murder_used"])

    def test_day4_locks_auto_open(self):
        eng = Engine(mk_state())
        goto(eng, "案件调查", day=4)
        self.assertFalse(eng.state["locks"]["地下室"])
        self.assertFalse(eng.state["locks"]["书房"])
        self.assertTrue(eng.state["locks"]["A的卧室"])

    def test_hall_rules_v3(self):
        """v3 会客室：不限容、永不黑暗、不可封闭；v3.5 起监控覆盖其门口；白天卧室清场；大厅谋杀受人数约束。"""
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        ev = eng.enter_rooms({n: "会客室" for n in ["A", "B", "C", "D", "E", "F"]})
        self.assertTrue(ev["ok"], ev)  # 不限容
        # v3.5：会客室纳入监控（rules-6 §9.1"覆盖所有房间门口"）
        self.assertEqual(set(eng.state["monitoring"]["第1天自由活动"]["会客室"]),
                         {"A", "B", "C", "D", "E", "F"})
        self.assertFalse(is_dark(eng.state, "会客室"))  # 永不黑暗
        self.assertFalse(eng.seal_room("F")["ok"])  # 大厅没有可锁的门（F 是建筑师）

        eng2 = Engine(mk_state())
        goto(eng2, "自由活动", day=1)
        ev = eng2.enter_rooms({"A": "书房", "B": "监控室", "C": "C的卧室", "D": "会客室", "E": "会客室", "F": "会客室"})
        self.assertTrue(ev["ok"])  # v3.5 顺序结算：选择无效不再整体驳回，滞留会客室
        self.assertEqual(eng2.state["positions"]["C"], "会客室")  # 白天卧室清场 → 滞留会客室
        self.assertIn("清场", ev["private"]["C"])
        self.assertEqual(ev.get("stranded"), {"C": "会客室"})

        eng3 = Engine(mk_state())
        free2(eng3, {"A": "会客室", "E": "会客室", "C": "会客室", "B": "监控室", "D": "书房", "F": "书房"})
        self.assertFalse(eng3.murder("A", "E")["ok"])  # 大厅是亮房：C 在场目击即不可动手

    def test_maingod_after_day6_vote(self):
        """v3.15：主神死线移至第 6 天投票后（终审日：只有白天会议，无自由活动/晚间休息）。
        第 2 天与第 4 天各一次谋杀行为（未遂/既遂）维持倒计时不归零，此后拖过第 5 天 → 第 6 天投票后主神胜。"""
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "书房", "D": "书房", "B": "监控室", "C": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng.murder("A", "D", weapon="刀")
        self.assertEqual(ev["type"], "murder_attempt")  # D【敏捷闪避】防御；未遂重置倒计时
        goto(eng, "自由活动", day=4)
        eng.enter_rooms({"A": "书房", "D": "书房", "B": "监控室", "C": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng.murder("A", "D")  # 刀已耗、闪避已耗：徒手杀死 D（A 魁梧 ≥ D 健壮），维持倒计时
        self.assertTrue(ev["ok"], ev)
        winner = None
        for _ in range(60):
            ev = eng.advance()
            if eng.winner:
                winner = eng.winner
                self.assertEqual(ev["type"], "game_over")
                break
        self.assertEqual(winner, "主神")
        self.assertIn("第 6 天", eng.state["win_reason"])
        self.assertEqual(ev["from"], {"day": 6, "stage": "投票"})  # 终审日投票后宣判


class TestMurderConditions(unittest.TestCase):
    def test_only_killer_and_once_per_stage(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        self.assertFalse(eng.murder("B", "E")["ok"])
        self.assertFalse(eng.murder("C", "E")["ok"])
        ev = eng.murder("A", "E")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(eng.murder("A", "E")["ok"])

    def test_presence_limit_light_room(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "C": "书房", "E": "书房", "B": "监控室", "D": "会客室", "F": "会客室"})
        ev = eng.murder("A", "E")
        self.assertFalse(ev["ok"])
        self.assertIn("非反派", ev["error"])

    def test_unarmed_physique_fail_then_accomplice_present(self):
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="女", physique=1)
        eng = Engine(mk_state(players=players))
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        ev = eng.murder("A", "E")
        self.assertFalse(ev["ok"])
        self.assertIn("空手", ev["error"])

        eng = Engine(mk_state(players=[P("A", "杀手", "魔术师", "刀", gender="女", physique=1)] + default_players()[1:]))
        free2(eng, {"A": "书房", "E": "书房", "B": "书房", "C": "会客室", "D": "会客室", "F": "会客室"})
        ev = eng.murder("A", "E")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")

    def test_unarmed_gender_condition(self):
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="男", physique=1)
        eng = Engine(mk_state(players=players))
        free2(eng, {"A": "书房", "C": "书房", "B": "监控室", "D": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng.murder("A", "C")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder_attempt")

    def test_dark_room_unarmed_no_extra_condition(self):
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="女", physique=1)
        eng = Engine(mk_state(players=players))
        ev = free2(eng, {"A": "地下室", "C": "地下室", "E": "地下室", "B": "监控室", "D": "会客室", "F": "会客室"})
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
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        self.assertTrue(eng.declare_blackout("B")["ok"])
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "书房", "C": "书房", "E": "书房", "B": "监控室", "D": "会客室", "F": "会客室"})
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)

    def test_weapon_murder_consumed_and_loot(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
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
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
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
        rooms = {"A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"}
        rooms["A"] = target_room
        rooms[target] = target_room
        rooms.update(extra or {})
        return free2(eng, rooms)

    def test_knife_vs_club_defended_weapon_not_consumed(self):
        players = default_players()
        players[4] = P("E", "平民", "黑客", "棒球棍", gender="男", physique=2)
        eng = Engine(mk_state(players=players, safe_item="左轮手枪"))
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

    def test_attempt_records_defense_via(self):
        """未遂台账记录防御途径（host 侧分析用，不下发给任何玩家；scorecard 未遂防御分布的数据源）。"""
        players = default_players()
        players[4] = P("E", "平民", "黑客", "棒球棍", gender="男", physique=2)
        eng = Engine(mk_state(players=players, safe_item="左轮手枪"))
        self.rooms_for(eng)
        ev = eng.murder("A", "E", weapon="刀")
        self.assertEqual(ev["type"], "murder_attempt")
        self.assertEqual(eng.state["attempts"][0]["defense"], "武器自卫")
        # 防御途径不进玩家私信（杀手只知"无法分辨"）
        self.assertNotIn("武器自卫", ev["private"]["A"])

    def test_club_vs_knife_and_gun_defended(self):
        for weapon, defender in [("棒球棍", "刀"), ("棒球棍", "左轮手枪"), ("刀", "左轮手枪")]:
            players = default_players()
            players[0] = P("A", "杀手", "魔术师", weapon, gender="男", physique=4)
            players[4] = P("E", "平民", "黑客", defender, gender="男", physique=2)
            safe = "不明药剂" if weapon != "不明药剂" and defender != "不明药剂" else "左轮手枪"
            if defender == "左轮手枪" or weapon == "左轮手枪":
                safe = "不明药剂"
            eng = Engine(mk_state(players=players, safe_item=safe))
            self.rooms_for(eng)
            ev = eng.murder("A", "E", weapon=weapon)
            self.assertEqual(ev["type"], "murder_attempt", (weapon, defender, ev))
            self.assertIn(defender, items_of(eng, "E"))

    def test_gun_attack_no_weapon_defense(self):
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "左轮手枪", gender="男", physique=4)
        players[4] = P("E", "平民", "黑客", "刀", gender="男", physique=2)
        eng = Engine(mk_state(players=players, safe_item="棒球棍"))
        self.rooms_for(eng)
        ev = eng.murder("A", "E", weapon="左轮手枪")
        self.assertEqual(ev["type"], "murder", ev)

    def test_poison_as_medicine_self_defense(self):
        """不明药剂药用：持有者遇袭时自动当作急救药（防一切袭击，用后消耗）。"""
        players = default_players()
        players[4] = P("E", "平民", "黑客", "不明药剂", gender="男", physique=2)
        eng = Engine(mk_state(players=players, safe_item="棒球棍"))
        self.rooms_for(eng)
        ev = eng.murder("A", "E", weapon="刀")
        self.assertEqual(ev["type"], "murder_attempt", ev)
        self.assertNotIn("不明药剂", items_of(eng, "E"))

    def test_poison_as_medicine_saves_roommate(self):
        """不明药剂药用：可救治同房被袭者（等同急救箱救他人）；黑暗房 ≤2 平民条件在地下室满足。"""
        players = default_players()
        players[3] = P("D", "平民", "运动员", "不明药剂", gender="男", physique=3)
        eng = Engine(mk_state(players=players, safe_item="棒球棍"))
        free2(eng, {"A": "地下室", "E": "地下室", "D": "地下室", "B": "监控室", "C": "会客室", "F": "会客室"})
        ev = eng.murder("A", "E", weapon="刀", savior="D")
        self.assertEqual(ev["type"], "murder_attempt", ev)
        self.assertNotIn("不明药剂", items_of(eng, "D"))
        self.assertFalse(eng.state["cases"])  # 无人死亡

    def test_poison_only_defended_by_kit_or_dodge(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=2)  # v3.27：当面赠送限房间行动阶段（此时全员仍滞留会客室）
        ev = eng.transfer("B", "A", "不明药剂")
        self.assertTrue(ev["ok"], ev)
        players_rooms = {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"}
        free2(eng, players_rooms)
        ev = eng.murder("A", "E", weapon="不明药剂")
        self.assertEqual(ev["type"], "murder", ev)

        eng = Engine(mk_state())
        goto(eng, "自由活动", day=2)  # v3.27：当面赠送限房间行动阶段
        eng.transfer("B", "A", "不明药剂")
        free2(eng, {"A": "书房", "D": "书房", "B": "监控室", "C": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng.murder("A", "D", weapon="不明药剂")
        self.assertEqual(ev["type"], "murder_attempt", ev)
        self.assertEqual(eng.state["skill_uses"]["D"]["敏捷闪避"], 0)

    def test_kit_defends_environment_weapon(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "C": "书房", "B": "监控室", "D": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng.murder("A", "C", weapon="雕像")
        self.assertEqual(ev["type"], "murder_attempt", ev)
        self.assertNotIn("急救箱", items_of(eng, "C"))

    def test_dodge_defends_unarmed(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "D": "书房", "B": "监控室", "C": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng.murder("A", "D")
        self.assertEqual(ev["type"], "murder_attempt", ev)
        self.assertEqual(eng.state["skill_uses"]["D"]["敏捷闪避"], 0)
        self.assertIn("手电筒", items_of(eng, "D"))

    def test_savior_kit_from_roommate(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "地下室", "C": "地下室", "E": "地下室", "B": "监控室", "D": "会客室", "F": "会客室"})
        ev = eng.murder("A", "E", savior="C")
        self.assertEqual(ev["type"], "murder_attempt", ev)
        self.assertNotIn("急救箱", items_of(eng, "C"))

    def test_attempt_announced_next_morning_only(self):
        """v3.22：死亡/未遂不再阶段末即时公告，统一在次日清晨公告（公告不含事发时段）。"""
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "D": "书房", "B": "监控室", "C": "会客室", "E": "会客室", "F": "会客室"})
        eng.murder("A", "D")
        ev = eng.advance()
        self.assertEqual(ev["to"]["stage"], "晚间休息")
        self.assertFalse(any("遇袭" in a for a in ev["public"]["announcements"]))  # 阶段末不公告
        ev = eng.advance()
        self.assertEqual(ev["to"]["stage"], "案件调查")
        self.assertTrue(any("D 遇袭但幸存" in a for a in ev["public"]["announcements"]))  # 清晨公告

    def test_night_murder_chance_independent(self):
        """v3.22：每阶段独立一次谋杀机会——白天无论未遂还是成功，进入晚间后机会都重置。"""
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "D": "书房", "B": "监控室", "C": "会客室", "E": "会客室", "F": "会客室"})
        eng.murder("A", "D")  # 未遂（D 敏捷闪避）
        goto(eng, "晚间休息", day=2)
        self.assertFalse(eng.state["stage_murder_used"])

        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        eng.murder("A", "E")  # 成功
        goto(eng, "晚间休息", day=2)
        self.assertFalse(eng.state["stage_murder_used"])
        ev = eng.murder("A", "F")
        self.assertFalse(ev["ok"])


class TestDeviceAndMagic(unittest.TestCase):
    def test_device_set_trigger_and_no_stage_chance(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng.set_device("B", "监控室")
        self.assertTrue(ev["ok"], ev)  # 首日自由活动也可设置
        self.assertEqual(eng.state["skill_uses"]["B"]["延时装置"], 0)

        eng = Engine(mk_state())
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng.set_device("B", "监控室")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(eng.set_device("B", "书房")["ok"])
        goto(eng, "自由活动", day=3)
        ev = eng.enter_rooms({"A": "书房", "B": "书房", "C": "监控室", "D": "会客室", "E": "会客室", "F": "会客室"})
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

    def test_device_set_at_night_triggers_next_stage_only(self):
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=1)
        eng.night_assign({"B": "监控室"})
        ev = eng.set_device("B", "监控室")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(eng.state["cases"], [])  # 放置当阶段不触发
        goto(eng, "自由活动", day=2)
        ev = eng.enter_rooms({"A": "会客室", "B": "书房", "C": "会客室",
                              "D": "监控室", "E": "会客室", "F": "会客室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev.get("device_triggers"), ["D"])
        self.assertEqual(eng.state["cases"][0]["period"], "第2天自由活动")
        self.assertFalse(player(eng, "D")["alive"])  # 敏捷闪避也不能防御

        eng2 = Engine(mk_state())
        goto(eng2, "晚间休息", day=1)
        eng2.night_assign({})
        ev = eng2.set_device("B", "B的卧室")
        self.assertFalse(ev["ok"])
        self.assertIn("非卧室", ev["error"])
        self.assertEqual(eng2.state["skill_uses"]["B"]["延时装置"], 1)

    def test_device_not_triggered_with_company(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"})
        eng.set_device("B", "监控室")
        goto(eng, "自由活动", day=3)
        ev = eng.enter_rooms({"A": "书房", "B": "书房", "C": "监控室", "D": "监控室", "E": "会客室", "F": "会客室"})
        self.assertTrue(ev["ok"], ev)
        self.assertNotIn("device_triggers", ev)
        self.assertTrue(player(eng, "C")["alive"])
        self.assertTrue(eng.state["devices"]["监控室"]["active"])

    def test_day1_stage_magic_allowed(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "C": "书房", "D": "书房", "B": "监控室", "E": "会客室", "F": "会客室"})
        ev = eng.stage_magic("A", "D")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["day"], 1)
        self.assertEqual(ev["case"]["period"], "第1天自由活动")
        self.assertFalse(player(eng, "D")["alive"])

    def test_stage_magic_with_witnesses_undefendable(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "C": "书房", "D": "书房", "B": "监控室", "E": "会客室", "F": "会客室"})
        ev = eng.stage_magic("A", "D")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(player(eng, "D")["alive"])
        self.assertEqual(eng.state["skill_uses"]["D"]["敏捷闪避"], 1)
        self.assertEqual(ev["case"]["wound"], "空手")
        self.assertIn("奇怪的动静", ev["private"]["C"])  # v3.30：在场者当场只觉奇怪动静、不知是谋杀
        self.assertFalse(ev["case"]["evidence_vulnerable"])
        self.assertFalse(eng.murder("A", "C")["ok"])
        self.assertEqual(eng.state["skill_uses"]["A"]["舞台魔术"], 0)

    def test_stage_magic_rejects(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        self.assertFalse(eng.stage_magic("B", "E")["ok"])
        players = default_players()
        players[0] = P("A", "平民", "魔术师", "刀", gender="男", physique=4)
        eng = Engine(mk_state(players=players))
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
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

    def test_lite_succession_includes_killer_guide(self):
        eng = Engine(mk_state())
        eng.state["is_lite_rule"] = True
        goto(eng, "投票", day=2)
        ev = eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        self.assertEqual(player(eng, "B")["identity"], "杀手")
        self.assertIn("行凶须知", ev["private"]["B"])
        self.assertIn("从第 1 天起", ev["private"]["B"])
        self.assertIn("进入空房没有目标", ev["private"]["B"])

    def test_one_civilian_left_game_continues(self):
        """v3.14：删去"只剩 1 平民反派直接胜"（v3.13 短命补丁回滚）——2v1 票仓锁死的残局
        不再提前收束，由下一轮投票自然处决；平民全灭才判反派胜。"""
        players = default_players()
        players[2]["alive"] = False
        players[3]["alive"] = False
        eng = Engine(mk_state(players=players, day=2, stage="投票"))
        ev = eng.resolve_vote({"A": "E", "B": "E", "F": "E", "E": "A"})
        self.assertEqual(ev["public"]["out"], "E")
        self.assertIsNone(ev.get("winner"))  # 剩 F 一名平民 + 2 反派：游戏继续
        # 下一轮投票处决最后一名平民 → 全灭，反派胜
        players[4]["alive"] = False  # E 已出局（上一票）
        eng2 = Engine(mk_state(players=players, day=3, stage="投票"))
        ev = eng2.resolve_vote({"A": "F", "B": "F", "F": "A"})
        self.assertEqual(ev.get("winner"), "反派")
        self.assertIn("所有平民", ev["win_reason"])

    def test_murder_leaving_one_civilian_continues(self):
        """v3.14：谋杀把平民杀到剩 1 人同样不提前收束——规则回归"杀光/票光所有平民"。"""
        players = default_players()
        players[2]["alive"] = False
        players[3]["alive"] = False  # C、D（平民）已死：存活 = A杀手、B帮凶、E平民、F平民
        eng = Engine(mk_state(players=players, day=2))
        goto(eng, "自由活动", day=2)
        ev = eng.enter_rooms({"A": "书房", "E": "书房", "B": "会客室", "F": "会客室"})
        assert ev["ok"], ev
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertIsNone(eng.winner)  # 只剩 F 一名平民：游戏继续

    def test_memory_implant(self):
        players = default_players()
        players[1] = P("B", "帮凶", "心理医生", "不明药剂", gender="女", physique=2)
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
        """v3：首次投票（第 2 天）无人出局即可武装指认；非平民不可指认。
        v3.21 淘汰制：指对 → 杀手出局、帮凶继任、游戏继续；帮凶已不在才平民胜。"""
        players = default_players()
        players[4] = P("E", "平民", "黑客", "棒球棍", gender="男", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "B", "F": "B"})  # 平票无人出局
        self.assertFalse(eng.armed_accusation("C", "A")["ok"])  # C 手无武器
        ev = eng.armed_accusation("E", "A")
        self.assertTrue(ev["ok"], ev)
        self.assertIsNone(eng.winner)                            # 帮凶继任，游戏继续
        self.assertEqual(ev["public"]["out"], "A")
        self.assertFalse(player(eng, "A")["alive"])              # 杀手出局
        self.assertEqual(player(eng, "B")["identity"], "杀手")   # 帮凶私下继任
        self.assertFalse(eng.armed_accusation("E", "B")["ok"])   # 每次投票至多一次（引擎级守卫）
        # 帮凶已不在时指认杀手 → 反派全灭、平民胜
        players2 = default_players()
        players2[4] = P("E", "平民", "黑客", "棒球棍", gender="男", physique=2)
        eng2 = Engine(mk_state(players=players2))
        player(eng2, "B")["alive"] = False
        goto(eng2, "投票", day=2)
        eng2.resolve_vote({"C": "A", "D": "A", "E": None, "F": None})  # 平票无人出局
        ev2 = eng2.armed_accusation("E", "A")
        self.assertTrue(ev2["ok"], ev2)
        self.assertEqual(ev2.get("winner"), "平民")

    def test_armed_accusation_wrong_target(self):
        players = default_players()
        players[4] = P("E", "平民", "黑客", "棒球棍", gender="男", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=3)
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
        goto(eng, "投票", day=3)
        eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        ev = eng.armed_accusation("E", "B")
        self.assertFalse(ev["ok"])

    def test_armed_accusation_poison_not_a_weapon(self):
        """不明药剂不能用于武装指认（rules-6 §7.3：仅左轮手枪/刀/棒球棍可威慑指认）。"""
        players = default_players()
        players[4] = P("E", "平民", "黑客", "不明药剂", gender="男", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "B", "F": "B"})  # 平票无人出局
        ev = eng.armed_accusation("E", "A")
        self.assertFalse(ev["ok"])
        self.assertIn("武器", ev["error"])

    def test_bequest_grant_and_table_fallback(self):
        """v3.5 遗嘱制：接受 → 当天即可用；拒收/未指定 → 不销毁，放回会客室桌上。"""
        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})  # A（杀手·魔术师·刀）出局
        self.assertEqual([e["name"] for e in eng.state["sealed_items"] if e.get("from") == "A"], ["刀"])
        ev = eng.bequest("A", {"刀": "C"}, {"C": {"刀": True}})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(items_of(eng, "C")["刀"]["available_day"], 2)  # v3.5：当天即可用
        self.assertEqual(eng.state["sealed_items"], [])
        self.assertIn("→ C", ev["public"]["announcements"][0])
        self.assertIn("当天即可用", ev["public"]["announcements"][0])

        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        ev = eng.bequest("A", {"刀": "C"}, {"C": {"刀": False}})  # 拒收 → 放回桌上（v3.5 不销毁）
        self.assertTrue(ev["ok"])
        self.assertNotIn("刀", items_of(eng, "C"))
        self.assertIn({"name": "刀", "uses_left": 1}, eng.state["table_items"])
        self.assertIn("放回会客室桌上", ev["public"]["announcements"][0])

        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        ev = eng.bequest("A", {}, {})  # 未指定 → 放回桌上
        self.assertTrue(ev["ok"])
        self.assertIn("未指定归属", ev["public"]["announcements"][0])
        self.assertIn("放回会客室桌上", ev["public"]["announcements"][0])
        self.assertEqual(eng.state["sealed_items"], [])
        self.assertIn({"name": "刀", "uses_left": 1}, eng.state["table_items"])

    def test_bequest_same_day_for_all(self):
        """v3.5：遗嘱道具一律当天即可用（不再只有继任帮凶例外）。"""
        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})  # A（杀手）出局 → B（帮凶）继任
        self.assertEqual(player(eng, "B")["identity"], "杀手")
        ev = eng.bequest("A", {"刀": "B"}, {"B": {"刀": True}})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(items_of(eng, "B")["刀"]["available_day"], 2)  # 当天即可用
        self.assertIn("当天即可用", ev["public"]["announcements"][0])

        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        ev = eng.bequest("A", {"刀": "C"}, {"C": {"刀": True}})  # 平民接收人同样当天即可用
        self.assertEqual(items_of(eng, "C")["刀"]["available_day"], 2)
        self.assertIn("当天即可用", ev["public"]["announcements"][0])


class TestHallNightSleep(unittest.TestCase):
    """v3.4 会客室夜宿（rules-6 §5/§7.7）：入夜自动落锁、【暴力破解】可开（随入）、
    停电/第 4 天起自由进入、清晨公开夜宿名单、谋杀按亮房条件。"""

    def test_hall_night_locked_and_crack_follow_in(self):
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        self.assertTrue(eng.state["locks"].get("会客室"))  # 入夜自动落锁
        ev = eng.night_assign({"C": "会客室"}, {})
        self.assertEqual(ev["positions"]["C"], "C的卧室")  # 无【暴力破解】手段进不去

        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"E": "会客室", "F": "会客室"}, {})
        self.assertEqual(ev["positions"]["E"], "会客室")   # E 黑客以暴力破解开锁
        self.assertEqual(ev["positions"]["F"], "会客室")   # 锁开后 F 随入
        self.assertEqual(eng.state["skill_uses"]["E"]["暴力破解"], 0)

    def test_hall_night_later_unlocker_admits_earlier_claimant(self):
        eng = Engine(mk_state(order=["C", "E", "F", "A", "B", "D"]))
        goto(eng, "晚间休息", day=1)
        ev = eng.night_assign({"C": "会客室", "E": "会客室", "F": "会客室"}, {})
        self.assertEqual(ev["unlockers"], {"会客室": "E"})
        self.assertEqual(ev["positions"]["C"], "会客室")  # C 虽先声明且无手段，也会随 E 的开锁进入
        self.assertEqual(ev["positions"]["E"], "会客室")
        self.assertEqual(ev["positions"]["F"], "会客室")
        self.assertEqual(eng.state["monitoring_true"]["第1天晚间休息"]["会客室"], ["E", "C", "F"])

    def test_hall_night_blackout_free_entry(self):
        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        self.assertTrue(eng.declare_blackout("B")["ok"])   # B 帮凶机械师拉电闸
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"C": "会客室"}, {})
        self.assertEqual(ev["positions"]["C"], "会客室")   # 停电锁失效，自由进入

    def test_hall_night_day4_free_entry(self):
        eng = Engine(mk_state())
        eng.state["phase"] = {"day": 3, "stage": "晚间休息"}
        eng.state["last_murder_day"] = 3  # 避开无杀倒计时
        goto(eng, "晚间休息", day=4)
        self.assertFalse(eng.state["locks"].get("会客室", False))  # 第 4 天起不再上锁
        ev = eng.night_assign({"C": "会客室"}, {})
        self.assertEqual(ev["positions"]["C"], "会客室")

    def test_hall_sleepers_announced_next_morning(self):
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        eng.night_assign({"E": "会客室"}, {})
        ev = eng.advance()  # → 第 3 天清晨
        self.assertTrue(any("夜宿会客室" in a and "E" in a for a in ev["public"]["announcements"]))

    def test_hall_night_murder_lit_condition(self):
        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        self.assertTrue(eng.declare_blackout("B")["ok"])
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"A": "会客室", "C": "会客室"}, {})
        self.assertEqual(ev["positions"]["A"], "会客室")
        ev = eng.murder("A", "C", weapon="刀")
        self.assertTrue(ev["ok"], ev)  # 恰好 1 平民，亮房条件可杀

        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        self.assertTrue(eng.declare_blackout("B")["ok"])
        goto(eng, "晚间休息", day=2)
        eng.night_assign({"A": "会客室", "C": "会客室", "D": "会客室"}, {})
        ev = eng.murder("A", "C", weapon="刀")
        self.assertFalse(ev["ok"])  # 2 名平民在大厅，亮房 ≤1 不满足


class TestDetectiveElection(unittest.TestCase):
    """v3.3 侦探竞选与警徽遗嘱（rules-6 §7.4）：第 1 天竞选产生，出局按遗嘱传承，不补选。"""

    def test_no_candidates(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=1)
        ev = eng.elect_detective([], [], {})
        self.assertTrue(ev["ok"], ev)
        self.assertIsNone(eng.state["detective"])
        self.assertTrue(eng.state["detective_election_done"])
        self.assertIn("无人报名", ev["public"]["announcements"][0])

    def test_single_candidate_acclaimed(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=1)
        ev = eng.elect_detective(["C"], ["C"], {})
        self.assertEqual(eng.state["detective"], "C")
        self.assertIn("直接当选", ev["public"]["announcements"][0])

    def test_tie_no_detective(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=1)
        ev = eng.elect_detective(["C", "D"], ["C", "D"], {"A": "C", "B": "D", "E": None, "F": None})
        self.assertIsNone(eng.state["detective"])
        self.assertIn("平票", ev["public"]["announcements"][0])

    def test_plurality_win_and_voter_eligibility(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=1)
        # 参选者 C/D 无投票权：C 的票被丢弃；A、B 投 C，E 投 D → C 2 票当选
        ev = eng.elect_detective(["C", "D"], ["C", "D"], {"C": "D", "A": "C", "B": "C", "E": "D", "F": None})
        self.assertEqual(eng.state["detective"], "C")
        self.assertIn("C 2票", ev["public"]["announcements"][0])

    def test_all_ran_no_voters(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=1)
        allsix = list(eng.state["order"])
        ev = eng.elect_detective(allsix, allsix[:2], {})
        self.assertIsNone(eng.state["detective"])
        self.assertIn("全员参选", ev["public"]["announcements"][0])

    def test_all_withdraw(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=1)
        ev = eng.elect_detective(["C", "D"], [], {})
        self.assertIsNone(eng.state["detective"])
        self.assertIn("全部退选", ev["public"]["announcements"][0])

    def test_election_only_day1_once(self):
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=1)
        self.assertTrue(eng.elect_detective(["C"], ["C"], {})["ok"])
        self.assertFalse(eng.elect_detective(["D"], ["D"], {})["ok"])  # 不重复竞选
        self.assertEqual(eng.state["detective"], "C")
        eng2 = Engine(mk_state())
        goto(eng2, "投票", day=2)
        self.assertFalse(eng2.elect_detective(["C"], ["C"], {})["ok"])  # 非第 1 天竞选时段

    def test_detective_vote_weight(self):
        """侦探一票计 1.5：6 人局侦探+2 人=3.5 过半（>3）；侦探+1 人=2.5 不过半。"""
        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        eng.state["detective"] = "C"
        ev = eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": None})
        self.assertEqual(ev["public"]["tally"]["A"], 3.5)
        self.assertEqual(ev["public"]["out"], "A")

        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        eng.state["detective"] = "C"
        ev = eng.resolve_vote({"C": "A", "D": "A"})
        self.assertEqual(ev["public"]["tally"]["A"], 2.5)
        self.assertIsNone(ev["public"]["out"])  # 2.5 不 > 3

        # 5 人局：侦探+1=2.5 不 >2.5；侦探+2=3.5 过半
        players = default_players()
        players[5]["alive"] = False  # F 已死
        eng = Engine(mk_state(players=players, day=2, stage="投票"))
        eng.state["detective"] = "C"
        self.assertIsNone(eng.resolve_vote({"C": "A", "D": "A"})["public"]["out"])
        eng = Engine(mk_state(players=players, day=2, stage="投票"))
        eng.state["detective"] = "C"
        ev = eng.resolve_vote({"C": "A", "D": "A", "E": "A"})
        self.assertEqual(ev["public"]["out"], "A")

    def test_badge_bequest_transfer_refuse_destroy(self):
        eng = Engine(mk_state())
        eng.state["detective"] = "C"
        player(eng, "C")["alive"] = False
        ev = eng.bequest_badge("C", "D", True)
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(eng.state["detective"], "D")
        self.assertIn("→ D", ev["public"]["announcements"][0])
        self.assertFalse(eng.bequest_badge("C", "E", True)["ok"])  # C 已非侦探

        eng = Engine(mk_state())
        eng.state["detective"] = "C"
        player(eng, "C")["alive"] = False
        ev = eng.bequest_badge("C", "D", False)  # 拒收 → 销毁
        self.assertIsNone(eng.state["detective"])
        self.assertIn("拒收", ev["public"]["announcements"][0])

        eng = Engine(mk_state())
        eng.state["detective"] = "C"
        player(eng, "C")["alive"] = False
        ev = eng.bequest_badge("C", None, True)  # 未指定 → 销毁
        self.assertIsNone(eng.state["detective"])
        self.assertIn("未指定", ev["public"]["announcements"][0])

    def test_badge_heir_exercises_weight(self):
        """警徽传承后，继任侦探的票权立即生效（5 人局 1.5+2=3.5 > 2.5）。"""
        eng = Engine(mk_state())
        eng.state["detective"] = "C"
        player(eng, "C")["alive"] = False
        eng.bequest_badge("C", "D", True)
        goto(eng, "投票", day=2)
        ev = eng.resolve_vote({"D": "A", "E": "A", "F": "A"})
        self.assertEqual(ev["public"]["out"], "A")


class TestCountdownAndNightRaid(unittest.TestCase):
    def test_no_kill_countdown_civilian_win(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "D": "书房", "B": "监控室", "C": "会客室", "E": "会客室", "F": "会客室"})
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

    def test_no_kill_countdown_from_day2_passive(self):
        """倒计时自第 2 天起算：全程无谋杀行为，第 4 天清晨平民直接获胜。"""
        eng = Engine(mk_state())
        winner_ev = None
        for _ in range(30):
            ev = eng.advance()
            if eng.winner:
                winner_ev = ev
                break
        self.assertEqual(eng.winner, "平民")
        self.assertIn("无杀倒计时", winner_ev["win_reason"])
        self.assertEqual(eng.state["phase"], {"day": 4, "stage": "案件调查"})

    def test_night_bedroom_unarmed_no_condition(self):
        """v3.16：夜间卧室（夜袭）空手谋杀无任何附加条件——体格 1 的女杀手也可空手夜袭体格 3 的男性
        （旧④"体格 ≥ 目标−1"在此驳回；v3.16 起夜间卧室豁免，空手仍不被凶器克制）。"""
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="女", physique=1)
        eng = Engine(mk_state(players=players))
        goto(eng, "晚间休息", day=2)
        player(eng, "A")["items"].append({"name": "万能钥匙", "uses_left": 2, "available_day": 1})
        eng.state["skill_uses"]["D"]["敏捷闪避"] = 0  # 排除被动防御，专注验证准入条件
        ev = eng.night_assign({"A": "D的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "D的卧室")
        ev = eng.murder("A", "D")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")
        self.assertEqual(ev["case"]["wound"], "空手")

    def test_night_hall_unarmed_still_gated(self):
        """v3.16：夜宿会客室为亮房大厅而非卧室——空手谋杀仍须满足 ①②③（夜间豁免仅限黑暗房间与卧室）。"""
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="女", physique=1)
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        self.assertTrue(eng.declare_blackout("B")["ok"])  # 停电门锁失效，二人才能夜宿会客室
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"A": "会客室", "D": "会客室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "会客室")
        self.assertEqual(ev["positions"]["D"], "会客室")
        ev = eng.murder("A", "D")  # 会客室永不黑暗：①②③ 均不满足 → 仍驳回
        self.assertFalse(ev["ok"])
        self.assertIn("空手", ev["error"])

    def test_countdown_text_plain(self):
        """v3.20 倒计时公告口径：0/2 直接给规则全文，1/2 给"今天再无杀则明早平民胜"——免换算。"""
        from src.engine.state import countdown_text
        t0 = countdown_text(2, None)
        self.assertIn("连续 2 个完整天", t0)
        self.assertIn("当前进度 0/2", t0)
        t1 = countdown_text(4, 2)  # 第 2 天有行为、第 3 天无杀 → 今天（第 4 天）再无杀则明早胜
        self.assertIn("1/2", t1)
        self.assertIn("明早平民直接获胜", t1)

    def test_night_full_bedroom_blocks_entry(self):
        """v3.22：卧室满 2 人天然挡住后来者（含杀手）——容量拦截，不消耗谋杀机会、不计倒计时。"""
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        self.assertEqual(eng.state["order"], ["B", "C", "D", "E", "F", "A"])  # A 最后行动，守夜对先成立
        ev = eng.night_assign({"D": "C的卧室", "A": "C的卧室"}, invited={"D": "C"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["D"], "C的卧室")
        self.assertEqual(ev["positions"]["A"], "A的卧室")
        self.assertEqual(ev["private"]["A"], "无法进入")                 # 卧室失败仅知"无法进入"
        self.assertFalse(eng.state["stage_murder_used"])                # 未发动不消耗
        self.assertIsNone(eng.state["last_murder_day"])                 # 不重置无杀倒计时
        self.assertTrue(any("满员" in n for n in ev.get("host", [])), ev.get("host"))  # host 留痕

    def test_night_empty_bedroom_no_cost(self):
        """v3.22：进入空卧室不消耗谋杀机会（与白天"未发动不消耗"一致）——夜袭空房白跑规则已删除。"""
        players = default_players()
        eng = Engine(mk_state(players=players))
        goto(eng, "晚间休息", day=2)
        player(eng, "A")["items"].append({"name": "万能钥匙", "uses_left": 2, "available_day": 1})
        ev = eng.night_assign({"A": "F的卧室", "F": "书房"})  # F 外出，卧室空置
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "F的卧室")
        self.assertFalse(eng.state["stage_murder_used"])      # 机会不消耗
        self.assertIsNone(eng.state["last_murder_day"])       # 不计谋杀行为
        self.assertFalse(eng.murder("A", "F")["ok"])          # 不在同一房间（F 在书房）

    def test_night_civilian_caution_occupied_room(self):
        """v3.22 平民慎入：平民非受邀进入已有他人的功能房→失败回卧室；反派与被邀请者豁免。"""
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"C": "书房", "D": "书房"})
        self.assertEqual(ev["positions"]["C"], "书房")
        self.assertEqual(ev["positions"]["D"], "D的卧室")
        self.assertIn("出于安全考虑", ev["private"]["D"])

        # 反派豁免
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"C": "书房", "A": "书房"})
        self.assertEqual(ev["positions"]["A"], "书房")

        # 被邀请者豁免（邀请者已在房内）
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"C": "书房", "D": "书房"}, invited={"D": "C"})
        self.assertEqual(ev["positions"]["D"], "书房")
        self.assertIn("接受了你的邀请", ev["private"].get("C", ""))

    def test_night_room_capacity_matches_day(self):
        """v3.22：夜间功能房人数上限与白天一致（书房 3 人），满员后无法进入。"""
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"B": "书房", "C": "书房", "E": "书房", "A": "书房"},
                              invited={"C": "B", "E": "B"})
        self.assertEqual(ev["positions"]["B"], "书房")
        self.assertEqual(ev["positions"]["C"], "书房")
        self.assertEqual(ev["positions"]["E"], "书房")
        self.assertEqual(ev["positions"]["A"], "A的卧室")  # 第 4 人：满员（反派也不例外）
        self.assertIn("满员", ev["private"]["A"])

    def test_invitee_enters_killer_hosted_bedroom(self):
        """应邀可进入杀手当房主的卧室——只按 2 人上限拦截，不按房内身份拦截。
        守夜由此成为"与虎谋皮"通道：黑暗卧室中受邀组合可低语，谋杀照 §7.6 判定（v3.22 卧室夜暗豁免空手）。
        （v3.5 二跑索尔案：杀手邀平民守夜被旧规则静默拒之门外，双方与 host 均不知原因。）"""
        order = ["F", "A", "B", "C", "D", "E"]
        players = default_players()
        eng = Engine(mk_state(players=players, order=order))
        goto(eng, "晚间休息", day=2)
        self.assertEqual(eng.state["order"], ["A", "B", "C", "D", "E", "F"])
        # A（杀手）当房主守在自己卧室，邀请顺序在后的 D 来守夜
        ev = eng.night_assign({"D": "A的卧室"}, invited={"D": "A"})
        self.assertEqual(ev["positions"]["A"], "A的卧室")
        self.assertEqual(ev["positions"]["D"], "A的卧室")  # 旧规则下 D 被静默拒之门外
        self.assertIn("接受了你的邀请", ev["private"].get("A", ""))  # 邀请成立对主人可见
        self.assertFalse(ev.get("host"))  # 无拦截即无 host 备注

    def test_invited_unlocker_keeps_inviter_notification(self):
        """应邀者同时是开锁者时会优先进门，但邀请成立通知仍按最终同室结果送达。"""
        eng = Engine(mk_state(order=["C", "E", "D", "A", "B", "F"]))
        goto(eng, "晚间休息", day=1)
        ev = eng.night_assign({"E": "C的卧室"}, invited={"E": "C"})
        self.assertEqual(ev["unlockers"], {"C的卧室": "E"})
        self.assertEqual(ev["positions"]["E"], "C的卧室")
        self.assertEqual(ev["positions"]["C"], "C的卧室")
        self.assertIn("E 接受了你的邀请", ev["private"].get("C", ""))

    def test_invitee_blocked_by_full_bedroom(self):
        """应邀者到达时卧室已满 2 人（杀手先开锁潜入 + 房主在内）→ 按容量拦截；
        玩家仅知"无法进入"，host 备注给出满员实情。"""
        order = ["F", "A", "B", "C", "D", "E"]
        players = default_players()
        players[0]["items"] = [{"name": "刀", "uses_left": 1, "available_day": 1},
                               {"name": "万能钥匙", "uses_left": 2, "available_day": 1}]
        eng = Engine(mk_state(players=players, order=order))
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"A": "C的卧室", "D": "C的卧室"}, invited={"D": "C"})
        self.assertEqual(ev["positions"]["A"], "C的卧室")  # 杀手先开锁进入
        self.assertEqual(ev["positions"]["D"], "D的卧室")  # 房内已 2 人，应邀者被容量拦下
        self.assertEqual(ev["private"]["D"], "无法进入")
        self.assertTrue(any("满员" in n for n in ev.get("host", [])), ev.get("host"))


class TestMorningReportAndInvestigation(unittest.TestCase):
    def kill_e_day2(self, eng, weapon="刀"):
        free2(eng, {"A": "书房", "E": "书房", "B": "书房", "C": "会客室", "D": "会客室", "F": "会客室"})
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

    def test_day1_two_cases_attempt_monitoring_and_investigation(self):
        """首日双案：自由活动和晚间各一起；同日另有未遂，清晨公告、监控与调查均不得串案。"""
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "E": "书房", "B": "监控室",
                         "C": "会客室", "D": "会客室", "F": "会客室"})
        self.assertTrue(eng.set_device("B", "监控室")["ok"])
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertIn("万能钥匙", items_of(eng, "A"))  # 缴获供当晚进他人卧室

        goto(eng, "晚间休息", day=1)
        ev = eng.night_assign({"A": "C的卧室", "F": "监控室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev.get("device_triggers"), ["F"])
        ev = eng.murder("A", "C")  # 黑暗卧室可空手；急救箱自动防御，形成同日未遂
        self.assertEqual(ev["type"], "murder_attempt")

        ev = eng.advance()
        self.assertEqual(eng.state["phase"], {"day": 2, "stage": "案件调查"})
        text = "；".join(ev["public"]["announcements"])
        self.assertIn("E 死亡", text)
        self.assertIn("F 死亡", text)
        self.assertIn("C 遇袭但幸存", text)
        monitoring = ev["public"]["monitoring"]
        for period in ("第1天自由活动", "第1天晚间休息"):
            for room in ("书房", "监控室", "E的卧室", "F的卧室"):
                self.assertIn(room, monitoring[period], (period, room))
        self.assertIn("A", monitoring["第1天自由活动"]["书房"])
        self.assertIn("F", monitoring["第1天晚间休息"]["监控室"])

        ev = eng.investigate("C", 1, "死亡时间")
        self.assertEqual(ev["private"]["C"]["result"], "第1天自由活动")
        ev = eng.investigate("C", 2, "死亡时间")
        self.assertEqual(ev["private"]["C"]["result"], "第1天晚间休息")
        self.assertEqual(eng.state["skill_uses"]["C"]["现场调查"], 0)

    def test_morning_seals_corpse_items_when_witnessed(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "地下室", "C": "地下室", "E": "地下室", "B": "监控室", "D": "会客室", "F": "会客室"})
        ev = eng.murder("A", "E")
        self.assertTrue(ev["ok"])
        goto(eng, "案件调查", day=3)
        sealed = {e["name"]: e for e in eng.state["sealed_items"]}
        self.assertIn("万能钥匙", sealed)
        self.assertEqual(sealed["万能钥匙"]["from"], "E")

    def test_investigate_truth_and_falsify(self):
        players = default_players()
        players[1] = P("B", "帮凶", "医师", "不明药剂", gender="女", physique=2)
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
        players[1] = P("B", "帮凶", "医师", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        free2(eng, {"A": "地下室", "C": "地下室", "E": "地下室", "B": "会客室", "D": "会客室", "F": "会客室"})
        eng.murder("A", "E")
        ev = eng.falsify("B", 1, "伪造痕迹")
        self.assertFalse(ev["ok"])

    def test_coverup_requires_scene_presence(self):
        """v3.18：毁证须身处案发现场——场外帮凶不能远程毁证/移尸；在场反派（含粉末道具）不受影响。"""
        players = default_players()
        players[1] = P("B", "帮凶", "医师", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        free2(eng, {"A": "书房", "E": "书房", "B": "会客室", "C": "会客室", "D": "会客室", "F": "会客室"})
        ev = eng.murder("A", "E")  # 现场仅 E 一名非反派 → 可毁证案件
        self.assertTrue(ev["ok"], ev)
        ev = eng.falsify("B", 1, "伪造痕迹")   # B 在会客室，不在案发现场
        self.assertFalse(ev["ok"])
        self.assertIn("案发现场", ev["error"])
        ev = eng.move_body("B", 1, "地下室")
        self.assertFalse(ev["ok"])
        self.assertIn("案发现场", ev["error"])
        player(eng, "A")["items"].append({"name": "可疑的粉末", "uses_left": 1, "available_day": 1})
        ev = eng.falsify("A", 1, "伪造证物")   # 杀手本人在场 → 仍可毁证
        self.assertTrue(ev["ok"], ev)

    def test_morning_gap_note_precise_periods(self):
        """v3.20：停电监控缺口公告精确到时段——白天（自由活动）修复则仅该时段缺失，
        不与宣读到的夜间记录矛盾；未修复则自由活动与当夜一并报缺失（game/838823 粗口径误导）。"""
        eng = Engine(mk_state())
        self.kill_e_day2(eng)  # 第 2 天有案件 → 清晨有监控宣读与缺口公告
        eng.state["blackout_days"] = [2]
        eng.state["power_restored"] = "第2天自由活动"
        ev = None
        while eng.state["phase"] != {"day": 3, "stage": "案件调查"}:
            ev = eng.advance()
        text = "\n".join(ev["public"]["announcements"])
        self.assertIn("第2天自由活动时段监控记录缺失", text)
        self.assertNotIn("第2天晚间休息时段监控记录缺失", text)
        # 未修复（或晚间才修复）：当夜记录同样缺失
        eng2 = Engine(mk_state())
        self.kill_e_day2(eng2)
        eng2.state["blackout_days"] = [2]
        eng2.state["power_restored"] = None
        ev2 = None
        while eng2.state["phase"] != {"day": 3, "stage": "案件调查"}:
            ev2 = eng2.advance()
        text2 = "\n".join(ev2["public"]["announcements"])
        self.assertIn("第2天自由活动与第2天晚间休息时段监控记录缺失", text2)

    def test_cross_day_blackout_repair_and_morning_gap(self):
        """跨日停电在第 3 天修复：修复日也记入缺失时段，恢复公告按实际修复日出现，
        次日清晨只报修复前的自由活动缺口，不误报已恢复的晚间。"""
        eng = Engine(mk_state())
        s = eng.state
        s["phase"] = {"day": 3, "stage": "自由活动"}
        s["power_out"] = True
        s["blackout_days"] = [2]
        s["blackout_periods"] = ["第2天自由活动", "第2天晚间休息"]
        s["positions"] = {"C": "地下室"}
        ev = eng.fix_generator("C")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(s["power_restored"], "第3天自由活动")
        self.assertIn("第3天自由活动", s["blackout_periods"])
        self.assertEqual(s["blackout_days"], [2])  # 一次跨日停电仍只计一个拉闸日

        s["phase"] = {"day": 3, "stage": "晚间休息"}
        public = {"announcements": []}
        eng._enter_stage("晚间休息", public)
        self.assertIn("供电已恢复", "\n".join(public["announcements"]))
        self.assertIn("地下室与夜间卧室仍黑暗", "\n".join(public["announcements"]))

        s["cases"].append({
            "id": 1, "victim": "E", "room": "书房", "crime_room": "书房",
            "day": 3, "stage": "自由活动", "period": "第3天自由活动",
            "method": "刀", "wound": "利器", "culprit": "A", "body_moved": False,
            "falsified": None, "looted": [], "evidence_vulnerable": True, "sealed": False,
        })
        player(eng, "E")["alive"] = False
        s["last_murder_day"] = 3
        ev = eng.advance()
        text = "\n".join(ev["public"]["announcements"])
        self.assertIn("第3天自由活动时段监控记录缺失", text)
        self.assertNotIn("第3天晚间休息时段监控记录缺失", text)

    def test_blackout_periods_legacy_migration(self):
        st = mk_state()
        st["blackout_days"] = [2]
        st["power_restored"] = "第3天自由活动"
        Engine(st)
        self.assertEqual(st["blackout_periods"],
                         ["第2天自由活动", "第2天晚间休息", "第3天自由活动"])

    def test_coverup_day_limit(self):
        """v3.19：毁证/移尸仅限案发当天——次日（清晨调查后）即使身处案发现场也无法再改动。"""
        players = default_players()
        players[1] = P("B", "帮凶", "医师", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        self.kill_e_day2(eng)  # 第 2 天 A 书房杀 E；A、B 同在案发现场
        goto(eng, "自由活动", day=3)
        eng.state["positions"]["B"] = "书房"  # 次日亲临现场也为时已晚
        ev = eng.falsify("B", 1, "伪造痕迹")
        self.assertFalse(ev["ok"])
        self.assertIn("仅限案发当天", ev["error"])
        ev = eng.move_body("B", 1, "地下室")
        self.assertFalse(ev["ok"])
        self.assertIn("仅限案发当天", ev["error"])

    def test_move_body(self):
        players = default_players()
        players[1] = P("B", "帮凶", "运动员", "不明药剂", gender="女", physique=2)
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
        self.assertEqual(ev["private"]["F"]["result"], "健壮或魁梧")  # v3.20：按角色表文字口径
        ev = eng.profile("F", 1, "性别")
        self.assertFalse(ev["ok"])


class TestMonitoringLocksLedger(unittest.TestCase):
    def test_monitoring_records_entry_only(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "书房", "D": "会客室", "E": "会客室", "F": "会客室"})
        recs = eng.state["monitoring"]["第1天自由活动"]
        self.assertEqual(set(recs["书房"]), {"A", "C"})
        self.assertEqual(recs["监控室"], ["B"])
        self.assertEqual(set(recs["会客室"]), {"D", "E", "F"})  # v3.5：会客室纳入监控
        self.assertEqual(recs, eng.state["monitoring_true"]["第1天自由活动"])

    def test_tamper_monitoring(self):
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "书房", "D": "会客室", "E": "会客室", "F": "会客室"})
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng.tamper_monitoring("B", "第1天自由活动", "书房", "remove", "A")
        self.assertTrue(ev["ok"], ev)
        recs = eng.state["monitoring"]["第1天自由活动"]["书房"]
        self.assertEqual(recs, ["C"])
        self.assertIn("A", eng.state["monitoring_true"]["第1天自由活动"]["书房"])
        self.assertFalse(eng.tamper_monitoring("B", "第1天自由活动", "书房", "add", "B")["ok"])

        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"})
        eng.tamper_monitoring("B", "第1天自由活动", "书房", "replace", "A", "B")
        self.assertEqual(eng.state["monitoring"]["第1天自由活动"]["书房"], ["B"])
        ev = eng.view_monitoring("B")
        self.assertEqual(ev["private"]["B"], eng.state["monitoring"])  # v3：监控室查看恒为全量返回
        self.assertEqual(ev["private"]["B"]["第1天自由活动"]["书房"], ["B"])

    def test_tamper_civilian_rejected(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "E": "监控室", "F": "会客室"})
        self.assertFalse(eng.tamper_monitoring("E", "第1天自由活动", "书房", "add", "E")["ok"])

    def test_unlock_permanent_and_bedroom_relock(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        ev = eng.enter_rooms({"E": "监控室", "A": "书房", "B": "书房", "C": "会客室", "D": "会客室", "F": "会客室"})
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(eng.state["locks"]["监控室"])  # v3.2：监控室上锁，暴力破解开后永久开启
        self.assertEqual(eng.state["skill_uses"]["E"]["暴力破解"], 0)
        goto(eng, "自由活动", day=2)
        self.assertFalse(eng.state["locks"]["监控室"])

        # 卧室被他人开锁进入后，下一时间阶段恢复上锁（v3：白天卧室清场，此场景只能在夜间发生）
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=1)
        ev = eng.night_assign({"E": "C的卧室"})
        self.assertEqual(ev["positions"]["E"], "C的卧室")  # E 黑客以【暴力破解】开锁进入
        self.assertFalse(eng.state["locks"]["C的卧室"])
        eng.advance()
        self.assertTrue(eng.state["locks"]["C的卧室"])

    def test_enter_locked_room_without_means_stranded(self):
        """v3.5 顺序结算：选择上锁房间而无开锁手段 → 滞留会客室（不再整体驳回），门保持上锁。"""
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        ev = eng.enter_rooms({"A": "监控室", "B": "书房", "C": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"})
        self.assertTrue(ev["ok"])
        self.assertEqual(eng.state["positions"]["A"], "会客室")  # A（杀手·魔术师）无开锁手段
        self.assertEqual(ev.get("stranded"), {"A": "会客室"})
        self.assertIn("上锁", ev["private"]["A"])
        self.assertTrue(eng.state["locks"]["监控室"])  # 门没被打开
        self.assertNotIn("监控室", eng.state["monitoring"].get("第1天自由活动", {}))  # 无人进入则无记录

    def test_room_capacity_latecomer_stranded(self):
        """v3.5 顺序结算：房间满员后，顺序靠后者滞留会客室（不再整体驳回）。"""
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        ev = eng.enter_rooms({"A": "书房", "B": "书房", "C": "书房", "D": "书房", "E": "会客室", "F": "会客室"})
        self.assertTrue(ev["ok"])
        self.assertEqual([n for n, r in eng.state["positions"].items() if r == "书房"], ["A", "B", "C"])
        self.assertEqual(eng.state["positions"]["D"], "会客室")  # 第 4 人（顺序靠后）被容量挡下
        self.assertIn("满员", ev["private"]["D"])

    def test_monitoring_room_capacity_two(self):
        """统一开锁轮：后续 E 开门救回顺序靠前的 D；E 优先进入，容量再挡住 F。"""
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        ev = eng.enter_rooms({"D": "监控室", "E": "监控室", "F": "监控室", "A": "书房", "B": "书房", "C": "会客室"})
        self.assertTrue(ev["ok"])
        self.assertEqual(ev["unlockers"], {"监控室": "E"})
        self.assertEqual(eng.state["positions"]["E"], "监控室")   # 开锁者优先
        self.assertEqual(eng.state["positions"]["D"], "监控室")   # 顺序靠前但无手段，不再被提前劝退
        self.assertEqual(eng.state["positions"]["F"], "会客室")   # 其余仍按行动顺序，容量不变
        self.assertEqual(eng.state["monitoring"]["第1天自由活动"]["监控室"], ["E", "D"])
        self.assertEqual(eng.state["skill_uses"]["E"]["暴力破解"], 0)
        self.assertEqual(items_of(eng, "E")["万能钥匙"]["uses_left"], 2)  # 职业技能优先，道具未消耗

    def test_entry_unlock_is_separate_and_can_be_declined(self):
        assignments = {"D": "监控室", "E": "监控室", "F": "监控室", "A": "书房", "B": "书房", "C": "会客室"}
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        before = copy.deepcopy(eng.state["positions"])
        ev = eng.unlock_for_entry("E", "监控室", assignments)
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(eng.state["positions"], before)  # 开锁轮不移动任何人
        self.assertFalse(eng.state["locks"]["监控室"])
        self.assertEqual(eng.state["skill_uses"]["E"]["暴力破解"], 0)
        entered = eng.enter_rooms(assignments, unlockers={"监控室": "E"})
        self.assertEqual(entered["positions"]["D"], "监控室")

        declined = Engine(mk_state())
        goto(declined, "自由活动", day=1)
        ev = declined.enter_rooms(assignments, unlockers={})  # 显式空表 = 无人愿意，不再自动开锁
        self.assertEqual(ev["positions"]["D"], "会客室")
        self.assertEqual(ev["positions"]["E"], "会客室")
        self.assertEqual(ev["positions"]["F"], "会客室")
        self.assertTrue(declined.state["locks"]["监控室"])
        self.assertEqual(declined.state["skill_uses"]["E"]["暴力破解"], 1)
        self.assertNotIn("监控室", declined.state["monitoring"].get("第1天自由活动", {}))

    def test_transfer_keeps_uses(self):
        """当面赠送（v3.27）：转手保留剩余次数；限同房；不设每日次数上限。"""
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=2)  # 此时全员仍滞留会客室
        ev = eng.transfer("D", "C", "手电筒")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(items_of(eng, "C")["手电筒"]["uses_left"], 2)
        self.assertFalse(eng.transfer("D", "C", "手电筒")["ok"])  # 已送出，未持有
        # 限同房：进入结算后 C 滞留会客室、E 在书房 → 无法当面交付
        free2(eng, {"A": "会客室", "B": "会客室", "C": "会客室", "D": "会客室", "E": "书房", "F": "会客室"})
        self.assertFalse(eng.transfer("C", "E", "手电筒")["ok"])
        # 同日再次发起不受限（v3.27 取消每日一次限制）：C → F 拒收也正常结算
        ev = eng.transfer("C", "F", "手电筒", accept=False)
        self.assertTrue(ev["ok"])
        self.assertFalse(ev["accepted"])
        self.assertNotIn("手电筒", items_of(eng, "F"))

    def test_open_safe(self):
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"E": "书房", "A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        ev = eng.open_safe("E")
        self.assertTrue(ev["ok"], ev)
        self.assertIn("左轮手枪", items_of(eng, "E"))
        self.assertIn("打开了保险箱", ev["private"]["A"])
        self.assertIn("左轮手枪", ev["private"]["A"])  # v3.3：内容开局已公告，同房者看得见取走的是哪件
        self.assertFalse(eng.open_safe("E")["ok"])

    def test_open_safe_dark_no_witness(self):
        """v3.3：停电（黑暗）中开保险箱，同房者收不到开箱通知（rules-6 §7.5 黑暗不可见）。"""
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)  # 暴力破解 → 可拉电闸
        eng = Engine(mk_state(players=players))
        goto(eng, "公共发言", day=1)
        ev = eng.declare_blackout("B")
        self.assertTrue(ev["ok"], ev)
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"E": "书房", "A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        ev = eng.open_safe("E")
        self.assertTrue(ev["ok"], ev)
        self.assertIn("左轮手枪", items_of(eng, "E"))
        self.assertNotIn("A", ev["private"])

    def test_flashlight_and_blackout(self):
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=1)
        ev = eng.enter_rooms({"D": "地下室", "E": "地下室", "A": "书房", "B": "监控室", "C": "会客室", "F": "会客室"})
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
        """v3.2：平民即使持有开锁手段（黑客的暴力破解）也不能拉电闸停电。"""
        eng = Engine(mk_state())
        goto(eng, "公共发言", day=2)
        ev = eng.declare_blackout("E")  # E 是平民黑客，持【暴力破解】
        self.assertFalse(ev["ok"])
        self.assertIn("反派", ev["error"])

    def test_blackout_window_until_rooms_entered(self):
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=2)
        ev = eng.declare_blackout("B")
        self.assertTrue(ev["ok"], ev)
        rooms = {"A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"}
        self.assertTrue(eng.enter_rooms(rooms)["ok"])
        self.assertEqual(eng.state["rooms_entered"], "第2天自由活动")
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng2 = Engine(mk_state(players=players))
        goto(eng2, "自由活动", day=2)
        self.assertTrue(eng2.enter_rooms(rooms)["ok"])
        self.assertFalse(eng2.declare_blackout("B")["ok"])

    def test_blackout_v32_package(self):
        """v3.2 停电包：门锁失效 + 监控空白 + 卧室黑暗化；杀手可在停电夜潜入满员卧室撕开守夜对。"""
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players, order=["C", "D", "A", "B", "E", "F"]))
        goto(eng, "投票", day=2)
        self.assertTrue(eng.declare_blackout("B")["ok"])           # 消耗 B 的暴力破解
        self.assertEqual(eng.state["skill_uses"]["B"]["暴力破解"], 0)
        goto(eng, "自由活动", day=2)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"})
        self.assertNotIn("第2天自由活动", eng.state["monitoring"])  # 停电期间监控空白
        self.assertNotIn("第2天自由活动", eng.state["monitoring_true"])
        goto(eng, "晚间休息", day=2)
        self.assertTrue(is_dark(eng.state, "C的卧室"))              # 卧室黑暗化（取消紧急照明）
        self.assertFalse(is_dark(eng.state, "会客室"))              # 会客室永不黑暗
        ev = eng.night_assign({"D": "C的卧室", "A": "C的卧室"}, invited={"D": "C"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "C的卧室")          # 黑暗中潜入满员卧室（门锁失效无需钥匙）
        self.assertFalse(eng.state["stage_murder_used"])           # 潜入本身不耗机会，深夜才判定
        ev = eng.murder("A", "C")                                   # 黑暗房 ≤2：可对守夜对动手
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder_attempt")              # C 的急救箱自动防御

    def test_night_assigned_marker(self):
        eng = Engine(mk_state())
        self.assertIsNone(eng.state["night_assigned"])
        goto(eng, "晚间休息", day=1)
        eng.night_assign({})
        self.assertEqual(eng.state["night_assigned"], "第1天晚间休息")

    def test_see_through(self):
        players = default_players()
        players[5] = P("F", "平民", "心理医生", "窃听器", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "书房", "D": "会客室", "E": "会客室", "F": "会客室"})
        goto(eng, "公共发言", day=2)
        ev = eng.see_through("F", "C", {"visited_rooms": ["书房"], "profession": "医师"})
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(ev["private"]["F"]["lies"])
        ev = eng.see_through("F", "C", {"denied_rooms": ["书房"]})
        self.assertTrue(ev["private"]["F"]["lies"])
        self.assertEqual(eng.state["skill_uses"]["F"]["看破伪装"], 0)
        self.assertFalse(eng.see_through("F", "C", {"profession": "医师"})["ok"])

    def test_see_through_unverifiable_not_counted(self):
        """v3.2 修复（game/42 误判）：占位符名目/无效房间 → "无法验证"，不污染谎言聚合。
        v3.5 起监控覆盖所有房间门口（含会客室），"无监控房间"情形只剩无效房间名。"""
        players = default_players()
        players[5] = P("F", "平民", "心理医生", "窃听器", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "公共发言", day=1)
        # C 是医师：职业声明属实；占位符道具名与无效房间名均为"无法验证"
        ev = eng.see_through("F", "C", {"profession": "医师", "item": "无声称", "visited_rooms": ["厨房"]})
        self.assertTrue(ev["ok"], ev)
        msg = ev["private"]["F"]
        self.assertFalse(msg["lies"])
        self.assertEqual(msg["verdicts"]["职业声明「医师」"], "属实")
        self.assertTrue(msg["verdicts"]["道具声明「无声称」"].startswith("无法验证"))
        self.assertTrue(msg["verdicts"]["去过厨房"].startswith("无法验证"))
        self.assertIn("全部属实", msg["conclusion"])
        # 谎言仍会被抓：C 不是化学家
        ev = eng.see_through("F", "C", {"profession": "化学家"})
        self.assertTrue(ev["private"]["F"]["lies"])
        self.assertEqual(ev["private"]["F"]["verdicts"]["职业声明「化学家」"], "谎言")
        # 空 claims 驳回
        self.assertFalse(eng.see_through("F", "C", {})["ok"])

    def test_see_through_blackout_gap_unverifiable(self):
        """v3.12（game/20 复盘：格罗克真实待过会客室，因停电监控空白被误判"谎言"票出）：
        存在停电空白时，行踪声称的"无记录"不构成谎言证据（判无法验证）；有记录为证的声称不受影响。"""
        players = default_players()
        players[5] = P("F", "平民", "心理医生", "窃听器", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "B": "监控室", "C": "书房", "D": "会客室", "E": "会客室", "F": "会客室"})
        goto(eng, "公共发言", day=2)
        eng.state["blackout_days"] = [2]  # 本局存在停电空白（监控缺失日）
        ev = eng.see_through("F", "C", {"visited_rooms": ["书房", "会客室"], "denied_rooms": ["会客室"]})
        self.assertTrue(ev["ok"], ev)
        v = ev["private"]["F"]["verdicts"]
        self.assertEqual(v["去过书房"], "属实")                       # 有记录为证，不受空白影响
        self.assertTrue(v["去过会客室"].startswith("无法验证"))        # 无记录+空白：不能排除空白期进入
        self.assertTrue(v["没去过会客室"].startswith("无法验证"))      # 无记录+空白：不能确认从未进入
        self.assertFalse(ev["private"]["F"]["lies"])
        # 无停电空白时恢复原口径：无记录 = 谎言/属实
        eng.state["blackout_days"] = []
        ev = eng.see_through("F", "C", {"visited_rooms": ["会客室"], "denied_rooms": ["书房"]})
        v = ev["private"]["F"]["verdicts"]
        self.assertEqual(v["去过会客室"], "谎言")
        self.assertEqual(v["没去过书房"], "谎言")
        self.assertTrue(ev["private"]["F"]["lies"])

    def test_see_through_all_invalid_rejected_no_consume(self):
        """v3.12（game/20 复盘：豆包把整句描述填进 item 白白消耗一次看破伪装）：
        claims 全部为非法名目 → 整体驳回、不消耗次数（走驳回重报）；部分合法则正常消耗。
        单个房间允许直接填字符串（容错）。"""
        players = default_players()
        players[5] = P("F", "平民", "心理医生", "窃听器", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "公共发言", day=1)
        ev = eng.see_through("F", "C", {"item": "声称刀为狄西遗嘱遗留，贴身不拔，未触碰桌上的病毒U盘"})
        self.assertFalse(ev["ok"])
        self.assertIn("不消耗次数", ev["error"])
        self.assertEqual(eng.state["skill_uses"]["F"]["看破伪装"], 2)  # 不消耗
        # 部分合法（item 非法 + 房间合法）：正常消耗，非法项判"无法验证"；房间接受单字符串
        ev = eng.see_through("F", "C", {"item": "一把刀", "visited_rooms": "书房"})
        self.assertTrue(ev["ok"], ev)
        v = ev["private"]["F"]["verdicts"]
        self.assertTrue(v["道具声明「一把刀」"].startswith("无法验证"))
        self.assertIn("去过书房", v)
        self.assertEqual(eng.state["skill_uses"]["F"]["看破伪装"], 1)

    def test_detective_skips_dead_and_order_filtered(self):
        """v3.2 修复（game/42 死侦探）：清晨公告顺序与存活名单不含死者（v3.3 起侦探竞选产生、不再每日轮换）。"""
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=1)
        s = eng.state
        s["cases"].append({"id": 1, "day": 1, "stage": "自由活动", "victim": "X", "room": "书房",
                           "culprit": "A", "period": "第1天自由活动", "wound": "利器",
                           "body_moved": False, "falsified": None, "sealed": True})
        s["profession_public"].append("X")  # 跳过清晨的职业公开（X 不是玩家）
        player(eng, "A")["alive"] = False   # A 为顺序首位
        ev = eng.advance()                  # → 第2天案件调查
        self.assertTrue(ev["ok"], ev)
        ann = "\n".join(ev["public"]["announcements"])
        self.assertNotIn("A →", ann)
        self.assertIn("目前存活 5 人：B、C、D、E、F", ann)

    def test_morning_roster_no_ghost_items(self):
        """v3.2 修复（game/42 幽灵道具）：items 为空列表时不得回退 deal 旧字段。"""
        from src.orchestrator import morning_roster_rows
        eng = Engine(mk_state())
        player(eng, "C")["items"] = []      # 急救箱已耗尽/被收走
        rows = morning_roster_rows(eng.state, {})
        row = next(r for r in rows if r[0] == "C")
        self.assertEqual(row[5], "无")
        row = next(r for r in rows if r[0] == "A")
        self.assertIn("刀", row[5])         # 正常持有不受影响

    def test_seal_room_blocks_night_entry(self):
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "F": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "E": "会客室"})
        ev = eng.seal_room("F", allowed=["C"])
        self.assertTrue(ev["ok"], ev)
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"E": "书房", "C": "书房"})
        self.assertEqual(ev["positions"]["E"], "E的卧室")
        self.assertEqual(ev["positions"]["C"], "书房")

    def test_passage(self):
        """v3 密道：开局确认不绑出口；仅晚间可用；出口三功能房任选；人数条件不满足当晚失败但保留；
        成功一次后损毁；监控无痕（记录上此人当晚在卧室）。"""
        players = default_players()
        players[1] = P("B", "帮凶", "建筑师", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        ev = eng.set_passage("B")
        self.assertTrue(ev["ok"], ev)
        goto(eng, "自由活动", day=1)
        self.assertFalse(eng.use_passage("B", "书房")["ok"])  # 白天不可用
        goto(eng, "晚间休息", day=1)
        eng.night_assign({"A": "监控室"})
        ev = eng.use_passage("B", "书房")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(ev.get("failed", False))
        self.assertEqual(eng.state["positions"]["B"], "书房")
        recs = eng.state["monitoring_true"]["第1天晚间休息"]
        self.assertNotIn("B", recs.get("书房", []))       # 书房无痕
        self.assertIn("B", recs.get("B的卧室", []))        # 记录上他在卧室
        self.assertFalse(eng.use_passage("B", "书房")["ok"])  # 成功一次即损毁

        # v3.22：会客室成为合法出口（亮房、无人 → 条件满足）；卧室仍非法
        eng = Engine(mk_state(players=players))
        eng.set_passage("B")
        goto(eng, "晚间休息", day=1)
        eng.night_assign({})
        ev = eng.use_passage("B", "会客室")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(ev.get("failed", False))
        self.assertEqual(eng.state["positions"]["B"], "会客室")
        eng = Engine(mk_state(players=players))
        eng.set_passage("B")
        goto(eng, "晚间休息", day=1)
        eng.night_assign({})
        self.assertFalse(eng.use_passage("B", "C的卧室")["ok"])

        # 出口人数不满足谋杀人数条件（亮房非反派 2 人）→ 当晚失败，密道保留
        eng = Engine(mk_state(players=players))
        eng.set_passage("B")
        goto(eng, "晚间休息", day=1)
        eng.night_assign({})
        eng.state["positions"]["C"] = "书房"  # 白盒构造：两名平民在书房（平民慎入规则下正常结算做不到）
        eng.state["positions"]["D"] = "书房"
        ev = eng.use_passage("B", "书房")
        self.assertTrue(ev["ok"])
        self.assertTrue(ev["failed"])
        self.assertFalse(eng.state["passage"]["used"])  # 失败不损毁
        self.assertEqual(eng.state["positions"]["B"], "B的卧室")

    def test_passage_murder_channel(self):
        """v3 密道击杀通道：杀手建筑师夜间经密道进入单独夜行者的房间并动手。"""
        players = default_players()
        players[0] = P("A", "杀手", "建筑师", "刀", gender="男", physique=4)
        eng = Engine(mk_state(players=players))
        eng.set_passage("A")
        goto(eng, "晚间休息", day=2)
        eng.night_assign({"E": "书房"})  # E 单独夜行（C 有急救箱、D 有闪避，E 无防御）
        ev = eng.use_passage("A", "书房")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(ev.get("failed", False))
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["room"], "书房")
        recs = eng.state["monitoring"]["第2天晚间休息"]["书房"]
        self.assertEqual(recs, ["E"])  # 监控里只有受害者——凶手无痕

    def test_morning_monitoring_covers_whole_day(self):
        """v3.22：清晨监控宣读覆盖案发当日全天两个时段 ×（案发房间 + 死者卧室）——不暴露死亡时段。"""
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        eng.murder("A", "E", weapon="刀")  # 自由活动杀人
        ev = None
        for _ in range(4):
            ev = eng.advance()
            if eng.state["phase"]["stage"] == "案件调查":
                break
        self.assertIsNotNone(ev)
        self.assertTrue(any("案发当日全天" in a for a in ev["public"]["announcements"]))
        mon = ev["public"].get("monitoring", {})
        self.assertIn("第2天自由活动", mon)
        self.assertIn("第2天晚间休息", mon)
        self.assertEqual(set(mon["第2天自由活动"].keys()), {"书房", "E的卧室"})
        self.assertEqual(mon["第2天自由活动"]["书房"], ["E", "A"])  # 按行动顺序进入（第 2 天 E 先于 A）

    def test_bug(self):
        """窃听器须在房间行动中到场放置；可放会客室，晚间卧室收听 ×2。"""
        eng = Engine(mk_state())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({"A": "书房", "F": "书房", "B": "监控室",
                         "C": "会客室", "D": "会客室", "E": "会客室"})
        ev = eng.place_bug("F", "书房")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(eng.state["bug_placed"], {"day": 1, "stage": "自由活动"})
        self.assertFalse(eng.place_bug("F", "监控室")["ok"])  # 已放置，且本人也不在该房间
        self.assertFalse(eng.bug_listen("F")["ok"])  # 自由活动不能收听
        goto(eng, "晚间休息", day=1)
        eng.night_assign({})
        ev = eng.bug_listen("F")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["room"], "书房")
        ev = eng.bug_listen("F")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(eng.bug_listen("F")["ok"])  # 第三次：次数用尽

        eng2 = Engine(mk_state())
        player(eng2, "F")["items"].append({"name": "万能钥匙", "uses_left": 2, "available_day": 1})
        goto(eng2, "晚间休息", day=1)
        eng2.night_assign({"F": "会客室"})
        self.assertTrue(eng2.place_bug("F", "会客室")["ok"])  # 晚间也可放置


class TestTableWeapon(unittest.TestCase):
    """v3.5 桌上凶器（rules-6 §2/§5/§7.3/§7.4/§7.7）：侦探佩枪、夜间独处取用、武装指认资格。"""

    def mk_table(self, **kw):
        st = mk_state(**kw)
        st["table_items"] = [{"name": "左轮手枪", "uses_left": 1}]
        return st

    def test_detective_elected_gets_gun(self):
        """侦探当选即获得桌上凶器（侦探佩枪，公开公告）。"""
        eng = Engine(self.mk_table())
        goto(eng, "公共发言", day=1)
        ev = eng.elect_detective(["C"], ["C"], {})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(eng.state["detective"], "C")
        self.assertEqual(eng.state["table_items"], [])
        self.assertEqual(items_of(eng, "C")["左轮手枪"]["available_day"], 1)  # 当天即可用
        self.assertIn("佩枪", ev["public"]["announcements"][0])

    def test_no_detective_gun_stays_on_table(self):
        """竞选流产 → 凶器留在桌上。"""
        eng = Engine(self.mk_table())
        goto(eng, "公共发言", day=1)
        ev = eng.elect_detective([], [], {})
        self.assertTrue(ev["ok"])
        self.assertIsNone(eng.state["detective"])
        self.assertEqual(eng.state["table_items"], [{"name": "左轮手枪", "uses_left": 1}])

    def test_accusation_monopoly_with_detective(self):
        """有侦探时仅侦探可指认（持械平民也不行）；侦探持佩枪可指认。"""
        players = default_players()
        players[4] = P("E", "平民", "黑客", "棒球棍", gender="男", physique=2)
        eng = Engine(self.mk_table(players=players))
        goto(eng, "公共发言", day=1)
        eng.elect_detective(["C"], ["C"], {})  # C（平民）当选并持佩枪
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "B", "E": "B", "F": "B"})  # 平票，无人出局
        ev = eng.armed_accusation("E", "A")
        self.assertFalse(ev["ok"])
        self.assertIn("侦探", ev["error"])  # 有侦探时仅侦探本人
        ev = eng.armed_accusation("C", "A")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(player(eng, "A")["alive"])              # v3.21：杀手出局、帮凶继任
        self.assertEqual(player(eng, "B")["identity"], "杀手")
        self.assertIsNone(eng.winner)

    def test_accusation_detective_needs_weapon(self):
        """侦探须持有武器才能指认（佩枪被缴获/送出后指认能力悬空）。"""
        eng = Engine(self.mk_table())
        goto(eng, "公共发言", day=1)
        eng.elect_detective(["C"], ["C"], {})
        cp = player(eng, "C")
        cp["items"] = [e for e in cp["items"] if e["name"] != "左轮手枪"]  # 模拟佩枪离场
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "B", "E": "B", "F": "B"})
        ev = eng.armed_accusation("C", "A")
        self.assertFalse(ev["ok"])
        self.assertIn("武器", ev["error"])

    def test_accusation_villain_detective_cannot(self):
        """侦探若为反派则无法指认（武装指认仅限平民）。"""
        eng = Engine(self.mk_table())
        goto(eng, "公共发言", day=1)
        eng.elect_detective(["B"], ["B"], {})  # B 是帮凶
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "B", "F": "C"})  # 无人过半
        ev = eng.armed_accusation("B", "C")
        self.assertFalse(ev["ok"])
        self.assertIn("平民", ev["error"])

    def test_accusation_temp_grab_without_detective(self):
        """无侦探且桌上凶器仍在：平民可临时取用指认；v3.22 起指认消耗武器——桌上凶器用掉即消失。"""
        eng = Engine(self.mk_table())
        goto(eng, "投票", day=2)  # 未举行竞选，detective=None
        eng.resolve_vote({"C": "A", "D": "B", "E": "B", "F": "B"})
        ev = eng.armed_accusation("C", "A")  # C 手无武器，临时取用桌上凶器
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(player(eng, "A")["alive"])              # v3.21：杀手出局、帮凶继任
        self.assertEqual(player(eng, "B")["identity"], "杀手")
        self.assertIsNone(eng.winner)
        self.assertIn("桌上的「左轮手枪」", ev["public"]["announcements"][0])
        self.assertIn("已消耗", ev["public"]["announcements"][0])
        self.assertEqual(eng.state["table_items"], [])           # 凶器已消耗，不再放回

    def test_accusation_temp_grab_wrong_consumes_gun(self):
        """临时取用指认错误：指认者出局；凶器已消耗（不在桌上、不入道具栏、不进遗物）。"""
        eng = Engine(self.mk_table())
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "B", "E": "B", "F": "B"})
        ev = eng.armed_accusation("C", "B")  # 指认帮凶 → 错误
        self.assertTrue(ev["ok"])
        self.assertFalse(player(eng, "C")["alive"])
        self.assertEqual(eng.state["table_items"], [])
        self.assertNotIn("左轮手枪", [e["name"] for e in eng.state["sealed_items"]])

    def test_accusation_consumes_own_weapon(self):
        """v3.22：持械指认消耗自己的武器（×1 即销毁）；可用 weapon 字段指定。"""
        players = default_players()
        players[3] = P("D", "平民", "黑客", "刀", gender="男", physique=2)
        players[3]["extra_items"] = ["棒球棍"]
        eng = Engine(mk_state(players=players))
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "B", "E": "B", "F": "B"})
        ev = eng.armed_accusation("D", "A", weapon="棒球棍")  # 指定用棍
        self.assertTrue(ev["ok"], ev)
        self.assertIn("棒球棍已消耗", ev["public"]["announcements"][0])
        names = [e["name"] for e in player(eng, "D")["items"]]
        self.assertNotIn("棒球棍", names)   # 用掉的销毁
        self.assertIn("刀", names)          # 未用的保留

    def test_accusation_no_weapon_no_table_rejected(self):
        """无侦探、桌上无凶器、手无武器 → 不能指认。"""
        eng = Engine(mk_state())
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "B", "E": "B", "F": "B"})
        ev = eng.armed_accusation("C", "A")
        self.assertFalse(ev["ok"])
        self.assertIn("武器", ev["error"])

    def test_accusation_last_civilian_pre_execution(self):
        """v3.21：投票将导致最后一名平民出局时，该平民可在出局执行前武装指认（背水一战）——
        指对则杀手出局、本人获救、帮凶继任；指错按原处决；放弃则 execute_pending_out 按原处决。"""
        trio = [P("A", "杀手", "魔术师", "刀", gender="男", physique=4),
                P("B", "帮凶", "机械师", "不明药剂", gender="女", physique=2),
                P("C", "平民", "医师", "急救箱", gender="女", physique=3)]

        # 指对：杀手伏法、最后平民获救
        eng = Engine(mk_state(players=[dict(p) for p in trio]))
        goto(eng, "投票", day=2)
        eng.resolve_vote({"A": "C", "B": "C", "C": "A"}, defer_out=True)  # C 2 票过半 → 待执行
        self.assertEqual(eng.state["pending_out"], "C")
        self.assertTrue(player(eng, "C")["alive"])  # 尚未执行
        self.assertFalse(eng.armed_accusation("B", "A")["ok"])  # 仅本人可用
        player(eng, "C")["items"].append({"name": "左轮手枪", "uses_left": 1, "available_day": 1})
        ev = eng.armed_accusation("C", "A")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(player(eng, "A")["alive"])   # 杀手出局
        self.assertTrue(player(eng, "C")["alive"])    # 最后平民获救
        self.assertEqual(player(eng, "B")["identity"], "杀手")  # 帮凶继任
        self.assertIsNone(eng.winner)
        self.assertIsNone(eng.state.get("pending_out"))

        # 指错（指到帮凶）：按原处决，本人出局 → 反派胜
        eng2 = Engine(mk_state(players=[dict(p) for p in trio]))
        goto(eng2, "投票", day=2)
        eng2.resolve_vote({"A": "C", "B": "C", "C": "A"}, defer_out=True)
        player(eng2, "C")["items"].append({"name": "左轮手枪", "uses_left": 1, "available_day": 1})
        ev2 = eng2.armed_accusation("C", "B")
        self.assertTrue(ev2["ok"], ev2)
        self.assertFalse(player(eng2, "C")["alive"])
        self.assertEqual(eng2.state["winner"], "反派")

        # 放弃指认：execute_pending_out 按原处决
        eng3 = Engine(mk_state(players=[dict(p) for p in trio]))
        goto(eng3, "投票", day=2)
        eng3.resolve_vote({"A": "C", "B": "C", "C": "A"}, defer_out=True)
        ev3 = eng3.execute_pending_out()
        self.assertTrue(ev3["ok"], ev3)
        self.assertFalse(player(eng3, "C")["alive"])
        self.assertEqual(eng3.state["winner"], "反派")
        self.assertFalse(eng3.execute_pending_out()["ok"])  # 无待执行结算 → 驳回

    def test_accusation_last_civilian_alive_count(self):
        """修正：背水一战窗口的"最后一名平民"按存活口径统计——原实现统计含死者的身份，
        有平民遇害（身份保留）后，最后一名存活平民的结算前指认被误驳回。"""
        # D/E/F（平民）已遇害，C 为最后一名存活平民
        eng = Engine(mk_state())
        for n in ("D", "E", "F"):
            player(eng, n)["alive"] = False
        goto(eng, "投票", day=2)
        ev0 = eng.resolve_vote({"A": "C", "B": "C", "C": "A"}, defer_out=True)  # C 2 票过半 → 待执行
        self.assertTrue(ev0["ok"], ev0)
        self.assertEqual(eng.state["pending_out"], "C")
        player(eng, "C")["items"].append({"name": "左轮手枪", "uses_left": 1, "available_day": 1})
        ev = eng.armed_accusation("C", "A")
        self.assertTrue(ev["ok"], ev)  # 原实现：含死者共 4 名"平民" → 误驳回
        self.assertFalse(player(eng, "A")["alive"])   # 指认正确：杀手出局
        self.assertTrue(player(eng, "C")["alive"])    # 最后平民获救
        self.assertEqual(player(eng, "B")["identity"], "杀手")  # 帮凶继任

        # 仍有三名存活平民时，结算前指认窗口保持关闭
        eng2 = Engine(mk_state())
        player(eng2, "D")["alive"] = False  # 仅一名平民遇害
        goto(eng2, "投票", day=2)
        eng2.resolve_vote({"A": "C", "B": "C", "E": "C", "C": "A", "F": "A"}, defer_out=True)
        self.assertEqual(eng2.state["pending_out"], "C")
        player(eng2, "C")["items"].append({"name": "左轮手枪", "uses_left": 1, "available_day": 1})
        ev2 = eng2.armed_accusation("C", "A")
        self.assertFalse(ev2["ok"])
        self.assertIn("最后一名平民", ev2["error"])

    def test_take_table_item_night_alone(self):
        """夜间独处会客室可取走桌上物品：当夜即可用，变动次日清晨公告。"""
        eng = Engine(self.mk_table())
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"E": "会客室"}, {})  # E 开锁进大厅（独占）
        self.assertEqual(ev["positions"]["E"], "会客室")
        ev = eng.take_table_item("E", "左轮手枪")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(eng.state["table_items"], [])
        self.assertEqual(items_of(eng, "E")["左轮手枪"]["uses_left"], 1)
        self.assertEqual(items_of(eng, "E")["左轮手枪"]["available_day"], 2)  # 当夜即可用
        ev = eng.advance()  # 次日清晨
        self.assertTrue(any("左轮手枪" in a and "不见" in a for a in ev["public"]["announcements"]))
        self.assertEqual(eng.state["table_events"], [])  # 公告后清空

    def test_take_table_item_not_alone_rejected(self):
        """会客室还有其他人时无法悄悄取用。"""
        eng = Engine(self.mk_table())
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"E": "会客室", "F": "会客室"}, {})  # E 开锁，F 随入
        self.assertEqual(ev["positions"]["F"], "会客室")
        ev = eng.take_table_item("E", "左轮手枪")
        self.assertFalse(ev["ok"])
        self.assertIn("其他人", ev["error"])
        self.assertEqual(len(eng.state["table_items"]), 1)

    def test_take_table_item_daytime_rejected(self):
        """白天（含自由活动滞留会客室）不可取桌上物品。"""
        eng = Engine(self.mk_table())
        goto(eng, "自由活动", day=1)
        eng.enter_rooms({n: "会客室" for n in ["A", "B", "C", "D", "E", "F"]})
        ev = eng.take_table_item("E", "左轮手枪")
        self.assertFalse(ev["ok"])
        self.assertIn("夜间", ev["error"])

    def test_take_table_item_blackout_no_trace(self):
        """停电夜偷枪：门锁失效自由进入，监控缺失不留进入记录。"""
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng = Engine(self.mk_table(players=players))
        goto(eng, "投票", day=2)
        self.assertTrue(eng.declare_blackout("B")["ok"])
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"C": "会客室"}, {})  # 停电锁失效，C 直接进
        self.assertEqual(ev["positions"]["C"], "会客室")
        ev = eng.take_table_item("C", "左轮手枪")
        self.assertTrue(ev["ok"], ev)
        self.assertNotIn("第2天晚间休息", eng.state["monitoring_true"])  # 停电期间监控缺失

    def test_table_fallback_keeps_uses(self):
        """遗嘱回桌的道具保留剩余次数；夜间取走后次数不变、当夜可用。"""
        eng = Engine(mk_state())
        player(eng, "A")["items"].append({"name": "万能钥匙", "uses_left": 1, "available_day": 1})
        goto(eng, "投票", day=2)
        eng.resolve_vote({"C": "A", "D": "A", "E": "A", "F": "A"})
        ev = eng.bequest("A", {}, {})  # 刀与万能钥匙（剩 1 次）都未指定 → 回桌
        self.assertTrue(ev["ok"])
        self.assertIn({"name": "万能钥匙", "uses_left": 1}, eng.state["table_items"])
        goto(eng, "晚间休息", day=2)
        eng.night_assign({"E": "会客室"}, {})
        ev = eng.take_table_item("E", "万能钥匙")
        self.assertTrue(ev["ok"], ev)
        uses = sorted(e["uses_left"] for e in player(eng, "E")["items"] if e["name"] == "万能钥匙")
        self.assertEqual(uses, [1, 2])  # 原有的 ×2（开锁消耗的是职业技能）+ 取走的 ×1

    def test_detective_gun_lootable_on_murder(self):
        """v3.5：佩枪是普通道具——侦探遇害且现场无其他非反派时，杀手缴获佩枪。"""
        players = default_players()
        players[2] = P("C", "平民", "医师", "测谎仪", gender="女", physique=2)  # 无防御道具
        eng = Engine(self.mk_table(players=players))
        goto(eng, "公共发言", day=1)
        eng.elect_detective(["C"], ["C"], {})  # C 当选并持佩枪
        free2(eng, {"A": "书房", "C": "书房", "B": "监控室", "D": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng.murder("A", "C")  # 空手（体格 4≥2）；佩枪防不了空手
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")
        self.assertIn("左轮手枪", ev["case"]["looted"])
        self.assertIn("左轮手枪", items_of(eng, "A"))


class TestGame2ReviewFixes(unittest.TestCase):
    """game/2 复盘修复（rules-6 §7.5 停电时机 / §7.7 夜袭空房与第 1 天口径 / 谋杀回执消耗提示）。"""

    def test_blackout_rejected_after_selection_begins(self):
        """首个进入落定后（selection_begun）停电声明窗口关闭——杜绝选房循环内声明造成"半段监控记录"。"""
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=2)
        self.assertTrue(eng.choose_room_one("C", "会客室")["ok"])
        ev = eng.declare_blackout("B")
        self.assertFalse(ev["ok"])
        self.assertIn("窗口已过", ev["error"])
        self.assertFalse(eng.state["power_out"])

    def test_blackout_preselection_full_suppression(self):
        """声明先于任何进入生效：停电时段监控整段零记录（含真实记录）；门锁对全员一致失效。"""
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=2)
        ev = eng.declare_blackout("B")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(eng.state["blackout_news"], "第2天自由活动")
        self.assertEqual(eng.state["skill_uses"]["B"]["暴力破解"], 0)
        # 门锁失效对顺序前后一致：无手段的 C 也免费进入上锁的监控室
        self.assertTrue(eng.choose_room_one("C", "监控室")["ok"])
        self.assertEqual(eng.state["positions"]["C"], "监控室")
        for n in ("A", "B", "D", "E", "F"):
            self.assertTrue(eng.choose_room_one(n, "会客室")["ok"])
        self.assertNotIn("第2天自由活动", eng.state["monitoring"])
        self.assertNotIn("第2天自由活动", eng.state["monitoring_true"])

    def test_day1_full_bedroom_blocks_entry(self):
        """首日开放谋杀不改变卧室容量：满员仍拦截进入，且不消耗当夜谋杀机会。"""
        players = default_players()
        players[1] = P("B", "帮凶", "黑客", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players, order=["C", "D", "A", "B", "E", "F"]))
        goto(eng, "公共发言", day=1)
        self.assertTrue(eng.declare_blackout("B")["ok"])  # 停电令门锁失效，排除开锁变量专测满员口径
        goto(eng, "晚间休息", day=1)
        ev = eng.night_assign({"D": "C的卧室", "A": "C的卧室"}, invited={"D": "C"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["D"], "C的卧室")
        self.assertEqual(ev["positions"]["A"], "A的卧室")  # 满员即拦截，不消耗机会
        self.assertFalse(eng.state["stage_murder_used"])

    def test_night_raid_empty_bedroom(self):
        """夜袭空卧室合法：照常进入、开锁手段消耗，但不产生谋杀判定/未遂记录、不重置无杀倒计时。"""
        players = default_players()
        players[0] = P("A", "杀手", "黑客", "刀", gender="男", physique=4)
        eng = Engine(mk_state(players=players, order=["C", "A", "B", "D", "E", "F"]))
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"C": "书房", "A": "C的卧室"})  # C 夜不归宿去书房
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "C的卧室")
        self.assertEqual(eng.state["skill_uses"]["A"]["暴力破解"], 0)  # 开锁手段照常消耗
        self.assertFalse(eng.state["cases"])
        self.assertFalse(eng.state["attempts"])
        self.assertIsNone(eng.state["last_murder_day"])

    def test_murder_receipt_notes_weapon_consumed(self):
        """谋杀回执明示凶器已消耗（成功与未遂）——防 AI 台账残留幻觉（game/2 索尔误报已消耗的刀）。"""
        eng = Engine(mk_state())  # A 杀手持刀；E 无自卫手段
        free2(eng, {"A": "书房", "E": "书房", "B": "会客室", "C": "会客室", "D": "会客室", "F": "会客室"})
        ev = eng.murder("A", "E", "刀")
        self.assertTrue(ev["ok"], ev)
        self.assertIn("刀已消耗", ev["private"]["A"])
        self.assertNotIn("刀", items_of(eng, "A"))  # 次数归零即销毁（从持有列表移除）

        eng2 = Engine(mk_state())  # 未遂：C 的急救箱自动防御，刀同样消耗
        free2(eng2, {"A": "书房", "C": "书房", "B": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"})
        ev = eng2.murder("A", "C", "刀")
        self.assertEqual(ev["type"], "murder_attempt")
        self.assertIn("刀已消耗", ev["private"]["A"])
        self.assertNotIn("刀", items_of(eng2, "A"))


class TestNightBedroomAccess(unittest.TestCase):
    """v3.22：夜间进入他人卧室一律需开锁手段或受邀（对所有人含反派一视同仁）；
    卧室满 2 人天然挡住后来者（含杀手）——旧"内应破门"口径已删除（rules-6 §7.7）。"""

    def test_killer_enters_villain_hosted_bedroom_with_means(self):
        """杀手持开锁手段可进入帮凶独居的卧室（锁对反派同样有效）；房内无平民则无可杀目标。"""
        players = default_players()
        players[0] = P("A", "杀手", "黑客", "刀", gender="男", physique=4)  # 暴力破解作开锁手段
        eng = Engine(mk_state(players=players))  # 第 2 天行动顺序 B→C→D→E→F→A
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"A": "B的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "B的卧室")
        self.assertEqual(eng.state["skill_uses"]["A"]["暴力破解"], 0)  # 开锁成本照付
        self.assertFalse(eng.murder("A", "B")["ok"])  # 不能以反派为目标

    def test_killer_blocked_without_means(self):
        """无开锁手段且未受邀：杀手同样进不了他人卧室（反派无锁豁免）。"""
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="男", physique=4)  # 无开锁手段
        eng = Engine(mk_state(players=players))
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"A": "F的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "A的卧室")
        self.assertEqual(ev["private"]["A"], "无法进入")

    def test_full_bedroom_blocks_killer_even_with_accomplice(self):
        """平民邀请帮凶守夜（引狼入室）后卧室满 2 人：杀手无法进入，机会与倒计时均不受影响。"""
        players = default_players()
        players[0] = P("A", "杀手", "黑客", "刀", gender="男", physique=4)
        # 跨日轮转后第 2 天顺序 = F→B→…→A：F（平民房主）先归房、B（帮凶）应邀随后、A 最后行动
        eng = Engine(mk_state(players=players, order=["A", "F", "B", "C", "D", "E"]))
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"B": "F的卧室", "A": "F的卧室"}, invited={"B": "F"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["B"], "F的卧室")
        self.assertEqual(ev["positions"]["A"], "A的卧室")          # 满 2 人：容量拦截（旧内应口径已删）
        self.assertEqual(eng.state["skill_uses"]["A"]["暴力破解"], 1)  # 容量先于开锁判定——手段未消耗
        self.assertFalse(eng.state["stage_murder_used"])
        self.assertIsNone(eng.state["last_murder_day"])

    def test_invited_killer_murders_host(self):
        """平民亲手邀请杀手守夜（与虎谋皮）：杀手应邀进入，黑暗卧室中可正常谋杀。"""
        players = default_players()
        players[0] = P("A", "杀手", "魔术师", "刀", gender="男", physique=4)
        eng = Engine(mk_state(players=players, order=["A", "F", "B", "C", "D", "E"]))
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"A": "F的卧室"}, invited={"A": "F"})  # F（平民，无防御）邀请 A
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "F的卧室")
        ev = eng.murder("A", "F", "刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["room"], "F的卧室")  # 尸体在房主卧室——自曝制衡


class TestDiscovery(unittest.TestCase):
    """v3.30 凶案发现与报案人：进入私下发现（≠自动报案）、报案为独立房间动作、
    同房多人各自决定、凶手可回现场自我报案、旁观者统一"奇怪动静"口径、旧档迁移。"""

    def test_discovery_private_and_report_action(self):
        """晚间进入案发房间 → 私下发现（discovered_by）但 reported_by 为空；
        报案是独立动作：report() 后才成为报案人，重复报案去重，清晨公告刊出。"""
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        case = eng.murder("A", "E", weapon="刀")["case"]
        self.assertEqual(case["reported_by"], [])
        goto(eng, "晚间休息", day=2)
        ev2 = eng.night_assign({"C": "书房"})
        self.assertIn("发现了E的尸体", ev2["private"]["C"])
        self.assertEqual(case["discovered_by"], ["C"])
        self.assertEqual(case["reported_by"], [])  # 发现 ≠ 报案
        ev3 = eng.report("C")
        self.assertTrue(ev3["ok"], ev3)
        self.assertEqual(case["reported_by"], ["C"])
        self.assertTrue(eng.report("C")["ok"])      # 重复报案去重不报错
        self.assertEqual(case["reported_by"], ["C"])
        ev4 = eng.advance()                          # 晚间休息 → 次日清晨公告
        anns = "\n".join(ev4["public"]["announcements"])
        self.assertIn("E 死亡，尸体发现于书房（报案人：C）", anns)

    def test_discovery_multiple_independent_report(self):
        """同房多人各自发现、各自决定：C 报案 D 沉默 → 公告仅列 C；D 的知情无公开痕迹。"""
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        case = eng.murder("A", "E", weapon="刀")["case"]
        goto(eng, "晚间休息", day=2)
        ev2 = eng.night_assign({"C": "书房", "D": "书房"}, invited={"D": "C"})
        self.assertIn("发现了E的尸体", ev2["private"]["C"])
        self.assertIn("发现了E的尸体", ev2["private"]["D"])
        self.assertEqual(set(case["discovered_by"]), {"C", "D"})
        self.assertTrue(eng.report("C")["ok"])
        self.assertEqual(case["reported_by"], ["C"])  # D 沉默
        ev4 = eng.advance()
        anns = "\n".join(ev4["public"]["announcements"])
        self.assertIn("（报案人：C）", anns)
        self.assertNotIn("（报案人：C、D）", anns)

    def test_killer_self_report(self):
        """凶手当晚回到案发现场 → 同样私下发现 → 可报案成为报案人（公告刊出）。"""
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        case = eng.murder("A", "E", weapon="刀")["case"]
        goto(eng, "晚间休息", day=2)
        ev2 = eng.night_assign({"A": "书房"})
        self.assertIn("发现了E的尸体", ev2["private"]["A"])
        self.assertEqual(case["discovered_by"], ["A"])
        self.assertTrue(eng.report("A")["ok"])
        ev4 = eng.advance()
        self.assertIn("E 死亡，尸体发现于书房（报案人：A）", "\n".join(ev4["public"]["announcements"]))

    def test_dark_bystander_noise_and_no_discovery(self):
        """黑暗房间谋杀的旁观者只得统一"奇怪动静"文案；未"进入"不算发现者，清晨无报案人。"""
        eng = Engine(mk_state())
        free2(eng, {"A": "地下室", "E": "地下室", "C": "地下室", "B": "会客室", "D": "会客室", "F": "会客室"})
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertIn("奇怪的动静", ev["private"]["C"])
        case = ev["case"]
        self.assertEqual(case["discovered_by"], [])
        eng.advance()  # 自由活动 → 晚间休息（无人再进地下室）
        ev4 = eng.advance()
        anns = "\n".join(ev4["public"]["announcements"])
        self.assertIn("E 死亡，尸体发现于地下室", anns)
        self.assertNotIn("报案人", anns)

    def test_stage_magic_discovered_later(self):
        """舞台魔术：目击者当场只知奇怪动静（不知是谋杀）；目击者当夜回到现场 → 发现并可报案。"""
        eng = Engine(mk_state())
        free2(eng, {"A": "书房", "C": "书房", "D": "书房", "B": "监控室", "E": "会客室", "F": "会客室"})
        ev = eng.stage_magic("A", "D")
        self.assertTrue(ev["ok"], ev)
        self.assertIn("奇怪的动静", ev["private"]["C"])
        self.assertNotIn("这是谋杀", ev["private"]["C"])
        self.assertEqual(ev["case"]["wound"], "空手")
        case = ev["case"]
        goto(eng, "晚间休息", day=2)
        ev2 = eng.night_assign({"C": "书房"})
        self.assertIn("发现了D的尸体", ev2["private"]["C"])
        self.assertEqual(case["discovered_by"], ["C"])
        self.assertTrue(eng.report("C")["ok"])
        ev4 = eng.advance()
        self.assertIn("D 死亡，尸体发现于书房（报案人：C）", "\n".join(ev4["public"]["announcements"]))

    def test_report_rejects(self):
        """报案校验：非行动阶段/无尸房间/不在现场 → 驳回。"""
        eng = Engine(mk_state())
        self.assertFalse(eng.report("C")["ok"])  # 开局准备：非行动阶段
        free2(eng, {"A": "书房", "E": "书房", "B": "监控室", "C": "会客室", "D": "会客室", "F": "会客室"})
        self.assertFalse(eng.report("C")["ok"])  # 会客室无尸体
        eng.murder("A", "E", weapon="刀")
        goto(eng, "晚间休息", day=2)
        eng.night_assign({"A": "书房"})          # A 回现场发现
        ev = eng.report("C")                     # C 在自己卧室
        self.assertFalse(ev["ok"])
        self.assertIn("尚未公告的尸体", ev["error"])

    def test_old_case_migration(self):
        """v3.30 旧档迁移：无 announced/discovered_by/reported_by 的历史案件 → ensure 补齐且视为已公告。"""
        st = mk_state()
        st["cases"] = [{"id": 1, "victim": "E", "room": "书房", "crime_room": "书房", "day": 1,
                        "stage": "自由活动", "period": "第1天自由活动", "method": "刀", "wound": "利器",
                        "culprit": "A", "body_moved": False, "falsified": None, "looted": [],
                        "evidence_vulnerable": False, "sealed": True}]
        eng = Engine(st)  # __init__ 调 ensure
        case = eng.state["cases"][0]
        self.assertTrue(case["announced"])  # 历史案件一律视为已公告（发现/报案窗口不再对其开放）
        self.assertEqual(case["discovered_by"], [])
        self.assertEqual(case["reported_by"], [])


class TestV331Buffs(unittest.TestCase):
    """v3.31：会客室桌上凶器临时取用（反派/用后消耗）；黑暗房间不设非反派人数限制（密道破满员扎堆）。"""

    def test_hall_table_weapon_murder(self):
        """杀手与目标独处会客室 → 桌上凶器视同环境凶器临时取用，成功后桌上消耗并入清晨公告。"""
        eng = Engine(mk_state())
        eng.state["table_items"] = [{"name": "左轮手枪", "uses_left": 1}]
        free2(eng, {"A": "会客室", "E": "会客室", "B": "书房", "C": "书房", "D": "书房", "F": "地下室"})
        ev = eng.murder("A", "E", weapon="左轮手枪")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["wound"], "枪")
        self.assertEqual(eng.state["table_items"], [])
        self.assertIn("已消耗", eng.state["table_events"][0])
        eng.advance()  # 自由活动 → 晚间休息
        ev4 = eng.advance()  # → 次日清晨公告
        anns = "\n".join(ev4["public"]["announcements"])
        self.assertIn("E 死亡", anns)
        self.assertIn("会客室桌上的「左轮手枪」已消耗", anns)

    def test_hall_table_weapon_requires_hall_and_table(self):
        """桌上凶器校验：人不在会客室、或桌上没有该武器 → 驳回。"""
        eng = Engine(mk_state())
        eng.state["table_items"] = [{"name": "左轮手枪", "uses_left": 1}]
        free2(eng, {"A": "书房", "E": "书房", "B": "会客室", "C": "会客室", "D": "地下室", "F": "地下室"})
        ev = eng.murder("A", "E", weapon="左轮手枪")  # A 不在会客室
        self.assertFalse(ev["ok"])
        self.assertIn("未持有", ev["error"])
        eng2 = Engine(mk_state())
        free2(eng2, {"A": "会客室", "E": "会客室", "B": "书房", "C": "书房", "D": "书房", "F": "地下室"})
        ev2 = eng2.murder("A", "E", weapon="左轮手枪")  # 桌上没有枪
        self.assertFalse(ev2["ok"])
        self.assertEqual(eng2.state["table_items"], [])

    def test_hall_table_weapon_consumed_on_failed_murder(self):
        """桌上凶器谋杀被防御（急救箱）→ 未遂但凶器照样消耗（同普通凶器：无论成败）。"""
        eng = Engine(mk_state())
        eng.state["table_items"] = [{"name": "左轮手枪", "uses_left": 1}]
        free2(eng, {"A": "会客室", "C": "会客室", "B": "书房", "D": "书房", "E": "书房", "F": "地下室"})
        ev = eng.murder("A", "C", weapon="左轮手枪")  # C 有急救箱 → 防御成功
        self.assertTrue(ev["ok"], ev)
        self.assertTrue(ev["defended"])
        self.assertTrue(player(eng, "C")["alive"])
        self.assertEqual(eng.state["table_items"], [])

    def test_dark_room_no_crowd_limit(self):
        """v3.31：黑暗房间不设非反派人数限制（白盒构造满员地下室 3 名非反派在场，旧规则会驳回）。"""
        eng = Engine(mk_state())
        free2(eng, {"A": "会客室", "B": "会客室", "C": "会客室", "D": "会客室", "E": "会客室", "F": "会客室"})
        eng.state["positions"] = {"A": "地下室", "C": "地下室", "D": "地下室", "E": "地下室",
                                  "B": "B的卧室", "F": "F的卧室"}
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["room"], "地下室")

    def test_passage_into_full_dark_room(self):
        """v3.31：密道可进满员黑暗房间（地下室 3 名平民受邀同入）并动手——破满员黑暗扎堆。"""
        players = default_players()
        players[0] = P("A", "杀手", "建筑师", "刀", gender="男", physique=4)
        eng = Engine(mk_state(players=players))
        eng.set_passage("A")
        goto(eng, "晚间休息", day=2)
        ev0 = eng.night_assign({"C": "地下室", "D": "地下室", "E": "地下室"}, invited={"D": "C", "E": "D"})
        self.assertTrue(ev0["ok"], ev0)
        self.assertEqual([ev0["positions"][n] for n in ("C", "D", "E")], ["地下室"] * 3)
        ev = eng.use_passage("A", "地下室")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(ev.get("failed", False))  # 旧规则：3 名非反派 → failed
        self.assertEqual(eng.state["positions"]["A"], "地下室")
        ev2 = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev2["ok"], ev2)


class TestV332PassageAccompliceMurder(unittest.TestCase):
    """v3.32：经密道进场的帮凶可借道谋杀（同舞台魔术/延时装置口径）——击杀归属发动者本人。"""

    def test_passage_accomplice_murder(self):
        """帮凶建筑师经密道进场后可当场动手：击杀归属帮凶本人、占用当阶段谋杀机会。"""
        players = default_players()
        players[1] = P("B", "帮凶", "建筑师", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        eng.set_passage("B")
        goto(eng, "晚间休息", day=2)
        eng.night_assign({"E": "书房"})  # 书房夜间明亮，E 单独在场
        ev = eng.use_passage("B", "书房")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(ev.get("failed", False))
        ev2 = eng.murder("B", "E", weapon="不明药剂")
        self.assertTrue(ev2["ok"], ev2)
        self.assertEqual(ev2["case"]["culprit"], "B")   # 击杀归属发动者本人（犯罪侧写同样指向 B）
        self.assertTrue(eng.state["stage_murder_used"])  # 占用当阶段谋杀机会
        self.assertFalse(player(eng, "E")["alive"])

    def test_accomplice_without_passage_cannot_murder(self):
        """对照：未走密道的帮凶仍无主动谋杀权。"""
        players = default_players()
        players[1] = P("B", "帮凶", "建筑师", "不明药剂", gender="女", physique=2)
        eng = Engine(mk_state(players=players))
        free2(eng, {"B": "地下室", "E": "地下室", "A": "会客室", "C": "会客室", "D": "会客室", "F": "会客室"})
        ev = eng.murder("B", "E", weapon="不明药剂")
        self.assertFalse(ev["ok"])
        self.assertIn("只有杀手", ev["error"])


if __name__ == "__main__":
    unittest.main()
