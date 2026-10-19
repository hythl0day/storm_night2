"""M5 平衡场景回归（v3 版）：平民扎堆（恒同室）与会客室蹲守，及反制路径（停电/舞台魔术/延时装置/开锁夜袭/密道）。

对照 docs/dev-plan.md M5 验收与 docs/rules-6.md v3：两种"龟缩"打法都必须存在可触发的反制。
v3 起白天卧室清场，蹲守的唯一去处是会客室（不限容、永不黑暗；v3.5 起纳入监控）。
引擎级确定性测试，不经 API；机制单点用例见 test_engine.py，本文件做场景化串联。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import unittest

from src.deal import ROOT, deal, load_personas, validate, write_card
from src.engine import Engine
from src.gamedata import (
    ITEM_DESC,
    ITEMS_GENERAL,
    SKILL_DESC_GENERAL,
    TABLE_WEAPON,
    VILLAIN_ITEMS,
    VILLAIN_WEAPON_POOL,
    WEAPONS,
)
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
        """3 平民占满书房（上限 3）后，杀手连门都进不去——扎堆的第一层保护来自容量。

        v3.5 顺序结算：容量挡下的是顺序靠后者，故本例把杀手放在行动顺序队尾。
        """
        # v3.5 顺序结算：容量挡下的是顺序靠后者。行动顺序每天轮转——
        # 初始顺序队首 F 在第 2 天移到队尾，当天顺序为 C→D→E→B→A→F，杀手 A 晚于三位平民。
        eng = Engine(mk_state(order=["F", "C", "D", "E", "B", "A"]))
        stack_day2(eng, {"A": "会客室", "B": "会客室", "C": "书房", "D": "书房", "E": "书房", "F": "会客室"})
        ev = eng.enter_rooms({"A": "书房", "B": "会客室", "C": "书房", "D": "书房", "E": "书房", "F": "会客室"})
        self.assertTrue(ev["ok"])
        self.assertEqual(ev["positions"]["A"], "会客室")  # 书房已满 3 人，靠后的杀手滞留会客室
        self.assertIn("A", ev.get("stranded", {}))
        self.assertIn("满员", ev["private"]["A"])

    def test_a2_vanilla_murder_rejected_in_lit_room(self):
        """亮房里 2 名平民在场，普通谋杀被驳回（在场非反派 >1）。"""
        eng = Engine(mk_state())
        stack_day2(eng, {"A": "书房", "B": "会客室", "C": "书房", "D": "会客室", "E": "书房", "F": "会客室"})
        ev = eng.murder("A", "E", weapon="刀")
        self.assertFalse(ev["ok"])
        self.assertIn("非反派", ev["error"])

    def test_a3_counter_blackout(self):
        """反制·停电：反派用开锁手段拉电闸，书房变黑暗，谋杀不设人数限制，两人扎堆被撕开。"""
        players = default_players()
        players[1] = P("B", "杀手", "机械师", "不明药剂", gender="女", physique=2)  # 暴力破解可拉电闸
        eng = Engine(mk_state(players=players))
        goto(eng, "自由活动", day=2)
        ev = eng.declare_blackout("B")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(eng.state["skill_uses"]["B"]["暴力破解"], 1)  # 停电消耗一次开锁手段（机械师×2 → 1）
        stack_day2(eng, {"A": "书房", "B": "会客室", "C": "书房", "D": "会客室", "E": "书房", "F": "会客室"})
        self.assertTrue(eng.state["power_out"])
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["type"], "murder")
        self.assertEqual(ev["case"]["victim"], "E")
        self.assertIn("奇怪的动静", ev["private"]["C"])  # v3.30：黑暗旁观者当场只知奇怪动静，不知遇害

    def test_a4_counter_stage_magic(self):
        """反制·舞台魔术：亮房、多名非反派在场也能动手（v4.0：可防御——E 无救援手段故仍击杀）。"""
        eng = Engine(mk_state())
        stack_day2(eng, {"A": "书房", "B": "会客室", "C": "书房", "D": "会客室", "E": "书房", "F": "会客室"})
        self.assertFalse(eng.murder("A", "E", weapon="刀")["ok"])  # 普通谋杀先被扎堆挡下
        ev = eng.stage_magic("A", "E")
        self.assertTrue(ev["ok"], ev)
        self.assertFalse(player(eng, "E")["alive"])
        self.assertIn("奇怪的动静", ev["private"]["C"])  # v3.30：在场者当场只觉奇怪动静、不知是谋杀

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
        stack_day2(eng, {"A": "会客室", "E": "会客室", "B": "书房", "C": "书房", "D": "书房", "F": "地下室"})
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["room"], "会客室")

    def test_b2_counter_unlock_night_raid(self):
        """反制·开锁夜袭：杀手用【暴力破解】夜闯独居卧室，完成击杀（夜间机制不变）。"""
        players = default_players()
        players[0] = P("A", "杀手", "机械师", "刀", gender="男", physique=4)  # 机械师通用技能【暴力破解】×2
        eng = Engine(mk_state(players=players))
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"A": "E的卧室"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "E的卧室")
        self.assertEqual(eng.state["skill_uses"]["A"]["暴力破解"], 1)  # ×2 → 开锁后剩 1
        ev = eng.murder("A", "E", weapon="刀")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["case"]["victim"], "E")

    def test_b3_counter_delayed_device_in_hall(self):
        """反制·延时装置：装置预设会客室，蹲守者独自滞留大厅即收到推送，机械师手动启动
        （v4.0：不再自动触发——谋杀窗口 device_fire，占用本窗口尝试）。"""
        eng = Engine(mk_state())  # B = 杀手 机械师（反派技能【延时装置】）
        eng.state["last_murder_day"] = 1  # 垫底：避免第 3 天清晨倒计时获胜（v4.0 从第 1 天起算）
        stack_day2(eng, {"A": "书房", "B": "会客室", "C": "书房", "D": "监控室", "E": "监控室", "F": "会客室"})
        ev = eng.set_device("B", "会客室")
        self.assertTrue(ev["ok"], ev)
        goto(eng, "自由活动", day=3)
        ev = eng.enter_rooms({"A": "书房", "B": "书房", "C": "会客室", "D": "监控室", "E": "监控室", "F": "书房"})
        self.assertTrue(ev["ok"], ev)
        self.assertNotIn("device_triggers", ev)                       # v4.0：进入结算不再自动触发
        self.assertEqual(eng.pending_devices(),
                         [{"room": "会客室", "setter": "B", "victim": "C"}])  # C 独自滞留大厅 → 机关推送
        ev = eng.device_fire("B")
        self.assertTrue(ev["ok"], ev)
        case = eng.state["cases"][0]
        self.assertEqual(case["wound"], "机关")
        self.assertEqual(case["room"], "会客室")
        self.assertFalse(player(eng, "C")["alive"])

    def test_b4_paired_bedroom_beats_night_raid(self):
        """反制的边界：两人合住一间卧室即满员，杀手无法进入（v3.22：容量拦截，不消耗机会），无人遇害。"""
        eng = Engine(mk_state())
        goto(eng, "晚间休息", day=2)
        ev = eng.night_assign({"D": "C的卧室", "A": "C的卧室"}, invited={"D": "C"})
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["positions"]["A"], "A的卧室")
        self.assertFalse(eng.state["stage_murder_used"])
        self.assertIsNone(eng.state["last_murder_day"])  # 不重置无杀倒计时
        self.assertFalse(eng.state["cases"])
        # 邀请成立对主人私密可见（黑暗中也知道应邀者到来）
        self.assertIn("接受了你的邀请", ev["private"].get("C", ""))


class TestDealVillainGuarantees(unittest.TestCase):
    """deal 分配约束（v4.0）：平民×4 + 杀手×2；两杀手各一件互不重复凶器（刀/棒球棍/不明药剂），
    随机一名另得「万能钥匙」、另一名另得一件反派道具（可疑的粉末/木马程序U盘）；
    平民车道（螺丝起子 1 人 + 其余 3 人从「通用池 5 件 + 未分出的反派道具」共 6 件各抽 1）；
    桌上凶器固定「左轮手枪」（不进反派车道）；四凶器严格必现；职业 8 选 6 纯随机（无保底互换）。"""

    @classmethod
    def setUpClass(cls):
        cls.players, _ = load_config(str(ROOT / "players.toml"))
        cls.personas = load_personas(ROOT / "personas.toml")

    def test_villain_lanes_across_seeds(self):
        for seed in range(1, 41):
            st = deal(seed, self.players, self.personas, None)
            self.assertEqual(validate(st), [], f"seed={seed} 分配校验失败")
            killers = [p for p in st["players"] if p["identity"] == "杀手"]
            self.assertEqual(len(killers), 2, f"seed={seed} 杀手人数错误")
            # 双杀手车道：各一件互不重复的反派池凶器（左轮手枪固定上桌）
            for k in killers:
                self.assertIn(k["item"], VILLAIN_WEAPON_POOL, f"seed={seed} 杀手凶器非法")
            self.assertNotEqual(killers[0]["item"], killers[1]["item"], f"seed={seed} 反派凶器重复")
            # 随机一名杀手另得万能钥匙，另一名另得一件反派道具
            key_holders = [k for k in killers if "万能钥匙" in k.get("extra_items", [])]
            gear_holders = [k for k in killers
                            if any(e in VILLAIN_ITEMS for e in k.get("extra_items", []))]
            self.assertEqual(len(key_holders), 1, f"seed={seed} 万能钥匙持有人数错误")
            self.assertEqual(len(gear_holders), 1, f"seed={seed} 反派道具持有人数错误")
            self.assertIsNot(key_holders[0], gear_holders[0], f"seed={seed} 钥匙与反派道具落入同一名杀手")
            self.assertEqual(len(gear_holders[0]["extra_items"]), 1, f"seed={seed} 反派道具数错误")
            self.assertEqual(st["table_items"], [{"name": TABLE_WEAPON, "uses_left": 1}],
                             f"seed={seed} 桌上凶器缺失")
            # 平民车道：螺丝起子恰好 1 人；无人持有万能钥匙；其余为通用池道具 /
            # 未分出的反派道具 /（保险箱抽中反派道具时的）剩余凶器
            civs = [p for p in st["players"] if p["identity"] == "平民"]
            self.assertEqual(len(civs), 4, f"seed={seed} 平民人数错误")
            for p in civs:
                self.assertNotEqual(p["item"], "万能钥匙",
                                    f"seed={seed} 平民 {p['name']} 抽到万能钥匙（禁入平民车道）")
                self.assertFalse(p.get("extra_items"), f"seed={seed} 平民 {p['name']} 持有额外道具")
            self.assertEqual(sum(1 for p in civs if p["item"] == "螺丝起子"), 1,
                             f"seed={seed} 螺丝起子车道错误")
            for p in civs:
                if p["item"] == "螺丝起子":
                    continue
                self.assertTrue(
                    p["item"] in ITEMS_GENERAL or p["item"] in VILLAIN_ITEMS
                    or p["item"] in VILLAIN_WEAPON_POOL,
                    f"seed={seed} 平民 {p['name']} 持有车道外道具 {p['item']}")
            # 四凶器严格必现：反派 2 件 + 桌上 1 件 + 保险箱或第四平民 1 件
            weapons_in_play = [p["item"] for p in st["players"] if p["item"] in WEAPONS]
            if st["safe_item"] in WEAPONS:
                weapons_in_play.append(st["safe_item"])
            weapons_in_play.append(TABLE_WEAPON)
            self.assertEqual(sorted(weapons_in_play), sorted(WEAPONS), f"seed={seed} 四凶器未全部入场")
            # 道具全局不重复（含保险箱）
            all_items = [p["item"] for p in st["players"]] + \
                        [e for p in st["players"] for e in p.get("extra_items", [])]
            self.assertEqual(len(set(all_items)), len(all_items), f"seed={seed} 道具重复")
            self.assertNotIn(st["safe_item"], all_items, f"seed={seed} 保险箱道具与玩家道具冲突")

    def test_no_villain_profession_guarantee(self):
        """v4.0 已删除 VILLAIN_PROFESSIONS 保底互换：职业 8 选 6 纯随机，允许反派队无功能技能职业。"""
        import src.deal as deal_module
        self.assertFalse(hasattr(deal_module, "VILLAIN_PROFESSIONS"))
        # 存在某 seed 使两名杀手都不持旧保底职业（证明无互换后处理）
        legacy_valves = {"魔术师", "机械师", "心理医生", "黑客", "建筑师"}
        for seed in range(1, 200):
            st = deal(seed, self.players, self.personas, None)
            profs = {p["profession"] for p in st["players"] if p["identity"] == "杀手"}
            if not profs & legacy_valves:
                return
        self.fail("200 个 seed 内未出现无保底职业的反派队，疑似仍存在互换后处理")

    def test_deal_lite_flag(self):
        """--lite：state 标记 is_lite_rule、ruleset 指向 lite 规则书；不消耗随机序列（同 seed 分配逐字节一致）。"""
        full = deal(42, self.players, self.personas, None)
        lite = deal(42, self.players, self.personas, None, lite=True)
        self.assertFalse(full["is_lite_rule"])
        self.assertEqual(full["ruleset"], "rules-6.md")
        self.assertTrue(lite["is_lite_rule"])
        self.assertEqual(lite["ruleset"], "rules-6-lite.md")
        self.assertEqual(lite["rules_version"], "v4.0.4")
        self.assertEqual(full["players"], lite["players"])
        self.assertEqual(full["order"], lite["order"])
        self.assertFalse(validate(lite))  # lite 局同样过分配校验

    def test_lite_card_self_contained(self):
        """lite 私密卡自包含：技能/道具带内联定义、不引用规则书章节（lite 规则书无 §3/§4 词条）；
        full 卡保持缩写 + 章节引用；仅 lite 杀手额外获得完整行凶须知。"""
        st = deal(42, self.players, self.personas, None, lite=True)
        for p in st["players"]:
            card = write_card(p, st["players"], lite=True)
            self.assertNotIn("规则书 §", card, p["name"])
            self.assertIn(SKILL_DESC_GENERAL[p["profession"]], card, p["name"])
            self.assertIn(ITEM_DESC[p["item"]], card, p["name"])
            full_card = write_card(p, st["players"], lite=False)
            self.assertIn("规则书 §", full_card)
            self.assertNotIn("## 行凶须知", full_card)
            if p["identity"] == "杀手":
                self.assertIn("## 行凶须知", card)
                self.assertIn("从第 1 天起", card)
                self.assertIn("进入空房没有目标", card)
            else:
                self.assertNotIn("## 行凶须知", card)

    def test_deal_still_deterministic(self):
        self.assertEqual(deal(42, self.players, self.personas, None),
                         deal(42, self.players, self.personas, None))

    def test_deal_writes_lite_flag(self):
        """deal 写入 is_lite_rule（默认 False = 高级规则；评分卡对缺字段旧档同样视为 False）。"""
        st = deal(42, self.players, self.personas, None)
        self.assertIn("is_lite_rule", st)
        self.assertFalse(st["is_lite_rule"])


class TestDealExclude(unittest.TestCase):
    """--exclude 屏蔽席位：随机抽席前从阵容池剔除（仅影响随机模式；rng 调用序列不变，同 seed + 同名单可复现）。"""

    @classmethod
    def setUpClass(cls):
        cls.players, _ = load_config(str(ROOT / "players.toml"))
        cls.personas = load_personas(ROOT / "personas.toml")

    def test_excluded_never_drawn(self):
        for seed in range(1, 21):
            st = deal(seed, self.players, self.personas, None, exclude={"grok", "doubao"})
            ids = [p["id"] for p in st["players"]]
            self.assertNotIn("grok", ids, f"seed={seed} 屏蔽席位仍被抽中")
            self.assertNotIn("doubao", ids, f"seed={seed} 屏蔽席位仍被抽中")
            self.assertEqual(validate(st), [], f"seed={seed} 分配校验失败")
            self.assertEqual(st["excluded_seats"], ["doubao", "grok"], f"seed={seed} 屏蔽名单留痕错误")

    def test_exclude_deterministic(self):
        self.assertEqual(deal(7, self.players, self.personas, None, exclude={"grok"}),
                         deal(7, self.players, self.personas, None, exclude={"grok"}))

    def test_exclude_pool_exhaustion(self):
        # 美国侧屏蔽 2 席后只剩 2 席（< 3），拒绝生成
        with self.assertRaises(SystemExit):
            deal(1, self.players, self.personas, None, exclude={"grok", "gemini"})

    def test_exclude_clashes_with_manual_seats(self):
        seats = ["gpt", "claude", "gemini", "deepseek", "kimi", "glm"]
        with self.assertRaises(SystemExit):
            deal(1, self.players, self.personas, seats, exclude={"gpt"})


class TestDealSeatSpecs(unittest.TestCase):
    """--seats 扩展：部分名单（不足 6 席随机补足，尽量维持中美 3+3）与身份锁定（「id或名字:身份」，先落位）。"""

    @classmethod
    def setUpClass(cls):
        cls.players, _ = load_config(str(ROOT / "players.toml"))
        cls.personas = load_personas(ROOT / "personas.toml")

    def _nations(self, st) -> tuple[int, int]:
        nats = [self.personas.get(p["name"], {}).get("nationality") for p in st["players"]]
        return nats.count("美国"), nats.count("中国")

    def test_partial_seats_filled_symmetric(self):
        # gpt/claude 均美国：固定 2 美 + 随机补 1 美 3 中 → 仍 3+3
        for seed in range(1, 21):
            st = deal(seed, self.players, self.personas, ["gpt", "claude"])
            ids = [p["id"] for p in st["players"]]
            self.assertIn("gpt", ids, f"seed={seed} 指名席位未入选")
            self.assertIn("claude", ids, f"seed={seed} 指名席位未入选")
            self.assertEqual(len(ids), 6)
            self.assertEqual(self._nations(st), (3, 3), f"seed={seed} 部分名单未维持中美 3+3")
            self.assertEqual(validate(st), [], f"seed={seed} 分配校验失败")

    def test_partial_seats_deterministic(self):
        self.assertEqual(deal(7, self.players, self.personas, ["gpt", "claude"]),
                         deal(7, self.players, self.personas, ["gpt", "claude"]))

    def test_partial_seats_asymmetric_fills_with_warning(self):
        # 固定 4 席全是美国 → 无法 3+3：合并池补足（仅剩中国池），不对称仅提示
        st = deal(3, self.players, self.personas, ["gpt", "claude", "gemini", "grok"])
        self.assertEqual(len(st["players"]), 6)
        self.assertEqual(self._nations(st), (4, 2))
        self.assertEqual(validate(st), [])

    def test_partial_seats_pool_exhaustion(self):
        # 固定 1 席 + 屏蔽 3 席 → 阵容池剩 4 席 < 需补足 5 席
        with self.assertRaises(SystemExit):
            deal(1, self.players, self.personas, ["gpt"], exclude={"grok", "gemini", "claude"})

    def test_pin_identity_lands(self):
        st = deal(42, self.players, self.personas, ["gpt:杀手", "claude", "glm:平民"])
        by_id = {p["id"]: p for p in st["players"]}
        self.assertEqual(by_id["gpt"]["identity"], "杀手")
        self.assertEqual(by_id["glm"]["identity"], "平民")
        self.assertEqual(st["pinned_identities"], {"glm": "平民", "gpt": "杀手"})
        ids = [p["identity"] for p in st["players"]]
        self.assertEqual(ids.count("杀手"), 2)
        self.assertEqual(ids.count("平民"), 4)
        self.assertEqual(validate(st), [])
        # 车道不受身份锁定影响：两名杀手中恰好一人持有万能钥匙
        killers = [p for p in st["players"] if p["identity"] == "杀手"]
        self.assertEqual(sum(1 for k in killers if "万能钥匙" in k.get("extra_items", [])), 1)

    def test_pin_both_villains(self):
        st = deal(42, self.players, self.personas, ["gpt:杀手", "deepseek:杀手"])
        ids = {p["id"]: p["identity"] for p in st["players"]}
        self.assertEqual(ids["gpt"], "杀手")
        self.assertEqual(ids["deepseek"], "杀手")
        self.assertEqual(validate(st), [])

    def test_pin_deterministic(self):
        self.assertEqual(deal(9, self.players, self.personas, ["gpt:杀手", "claude"]),
                         deal(9, self.players, self.personas, ["gpt:杀手", "claude"]))

    def test_full_manual_seats_still_work(self):
        st = deal(42, self.players, self.personas,
                  ["gpt", "claude", "gemini", "deepseek", "kimi", "glm"])
        self.assertEqual(len(st["players"]), 6)
        self.assertEqual(st["pinned_identities"], {})
        self.assertEqual(validate(st), [])

    def test_duplicate_seat_rejected(self):
        with self.assertRaises(SystemExit):
            deal(1, self.players, self.personas, ["gpt", "gpt:杀手"])

    def test_unknown_seat_rejected(self):
        with self.assertRaises(SystemExit):
            deal(1, self.players, self.personas, ["gpt", "nonexistent"])

    def test_invalid_identity_rejected(self):
        with self.assertRaises(SystemExit):
            deal(1, self.players, self.personas, ["gpt:主教"])

    def test_pin_quota_rejected(self):
        # 杀手配额 2：指定 3 名杀手报错
        with self.assertRaises(SystemExit):
            deal(1, self.players, self.personas, ["gpt:杀手", "claude:杀手", "glm:杀手"])
        with self.assertRaises(SystemExit):
            deal(1, self.players, self.personas,
                 ["gpt:平民", "claude:平民", "gemini:平民", "grok:平民", "deepseek:平民"])

    def test_pin_clashes_with_exclude(self):
        with self.assertRaises(SystemExit):
            deal(1, self.players, self.personas, ["gpt:杀手", "claude"], exclude={"gpt"})


if __name__ == "__main__":
    unittest.main()
