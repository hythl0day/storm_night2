"""存档分叉重演：历史 outbox 重放、节点另存为与现有 Orchestrator 续跑。"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src import deal as deal_module
from src import orchestrator
from src.deal import deal, load_personas, write_card
from src.orchestrator import Orchestrator
from src.providers import load_config
from src.replay import ReplayError, ReplayPoint, _load_initial, prepare_replay
from test_engine import default_players
from test_orchestrator_game import make_fake_chat, write_game_dir

ROOT = Path(__file__).resolve().parent.parent


def run_with_chat(game_dir: Path, chat_fn):
    orch = Orchestrator(game_dir)
    original = orchestrator.chat
    orchestrator.chat = chat_fn
    try:
        orch.run_game()
    finally:
        orchestrator.chat = original
    return orch


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


class ReplayTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_finished_source(self) -> Path:
        source = write_game_dir(self.root / "source", default_players())
        initial = json.loads((source / "state.json").read_text(encoding="utf-8"))
        (source / "deal.json").write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")
        run_with_chat(source, make_fake_chat(True))
        return source

    def make_unrun_source(self) -> Path:
        source = write_game_dir(self.root / "source", default_players())
        initial = json.loads((source / "state.json").read_text(encoding="utf-8"))
        (source / "deal.json").write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")
        return source


class TestReplayBranch(ReplayTestBase):
    def test_branch_stops_before_target_and_resume_diverges(self):
        source = self.make_finished_source()
        source_before = tree_bytes(source)
        source_final = json.loads((source / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(source_final["cases"])
        branch = self.root / "branch"

        metadata = prepare_replay(source, branch, ReplayPoint(2, "A", "自由活动-房间行动"))
        self.assertGreater(metadata["replayed_calls"], 0)
        state = json.loads((branch / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["phase"], {"day": 2, "stage": "自由活动"})
        self.assertIsNone(state["winner"])
        self.assertEqual(state["positions"]["A"], "书房")
        self.assertEqual(state["positions"]["E"], "书房")
        wip = state["orch"]["wip"]["2:自由活动:房间行动"]
        self.assertNotIn("A", wip["done"])
        self.assertIn("书房", wip["rooms_sectioned"])
        self.assertFalse((branch / "outbox" / "A" / "day2-自由活动-房间行动.md").exists())
        public = (branch / "public" / "log.md").read_text(encoding="utf-8")
        self.assertNotIn("游戏结束", public)
        self.assertEqual((branch / "script.md").read_text(encoding="utf-8").count("### 书房 · 房间行动"), 1)

        resumed = Orchestrator(branch)
        menu = resumed.murder_menu(resumed.player("A"), 2)  # v3.27：谋杀拆为独立谋杀窗口菜单
        self.assertTrue(any('"type": "murder"' in item and 'E' in item for item in menu))
        run_with_chat(branch, make_fake_chat(False))

        branched_final = json.loads((branch / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(branched_final["winner"], "平民")
        self.assertIn("无杀倒计时", branched_final["win_reason"])
        self.assertEqual(branched_final["cases"], [])
        self.assertTrue((branch / "outbox" / "A" / "day2-自由活动-房间行动.md").exists())
        self.assertEqual((branch / "script.md").read_text(encoding="utf-8").count("### 书房 · 房间行动"), 1)
        self.assertEqual(tree_bytes(source), source_before)

    def test_missing_recorded_response_removes_partial_branch(self):
        source = self.make_unrun_source()
        branch = self.root / "branch"
        with self.assertRaisesRegex(ReplayError, "缺少目标前的原始回复"):
            prepare_replay(source, branch, ReplayPoint(2, "A", "自由活动-房间行动"))
        self.assertFalse(branch.exists())

    def test_output_must_be_independent_and_new(self):
        source = self.make_unrun_source()
        point = ReplayPoint(1, "F", "开局准备")
        with self.assertRaisesRegex(ReplayError, "独立于源存档"):
            prepare_replay(source, source, point)
        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(ReplayError, "输出目录已存在"):
            prepare_replay(source, existing, point)


class TestLegacyInitial(ReplayTestBase):
    def make_legacy_source(self) -> tuple[Path, dict]:
        players, _settings = load_config(str(ROOT / "players.toml"))
        initial = deal(42, players, load_personas(ROOT / "personas.toml"), None)
        source = self.root / "legacy"
        (source / "public").mkdir(parents=True)
        (source / "state.json").write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")
        (source / "players.toml").write_bytes((ROOT / "players.toml").read_bytes())
        (source / "rules.md").write_bytes((ROOT / "docs" / "rules-6.md").read_bytes())
        for p in initial["players"]:
            seat = source / "private" / p["id"]
            seat.mkdir(parents=True)
            (seat / "card.md").write_text(write_card(p, initial["players"]), encoding="utf-8")
        return source, initial

    def test_regenerates_initial_state_without_snapshot(self):
        source, initial = self.make_legacy_source()
        final = json.loads(json.dumps(initial))
        restored, personas_path = _load_initial(source, final)
        self.assertEqual(restored["order"], initial["order"])
        self.assertEqual(restored["players"], initial["players"])
        self.assertEqual(personas_path, ROOT / "personas.toml")

    def test_regeneration_mismatch_is_rejected(self):
        source, _initial = self.make_legacy_source()
        final = json.loads((source / "state.json").read_text(encoding="utf-8"))
        final["players"][0]["profession"] = "被篡改的职业"
        with self.assertRaisesRegex(ReplayError, "初始分配与源存档不一致"):
            _load_initial(source, final)

    def test_deal_writes_initial_state_and_personas_snapshot(self):
        out = self.root / "new-game"
        argv = [
            "src.deal", "--seed", "42", "--out", str(out),
            "--config", str(ROOT / "players.toml"),
            "--personas", str(ROOT / "personas.toml"),
            "--rules", str(ROOT / "docs" / "rules-6.md"),
        ]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            deal_module.main()
        self.assertEqual(
            json.loads((out / "state.json").read_text(encoding="utf-8")),
            json.loads((out / "deal.json").read_text(encoding="utf-8")),
        )
        self.assertEqual((out / "personas.toml").read_bytes(), (ROOT / "personas.toml").read_bytes())
        orch = Orchestrator(out)
        self.assertEqual(orch.personas, load_personas(out / "personas.toml"))


if __name__ == "__main__":
    unittest.main()
