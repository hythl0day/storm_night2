"""viewer.py 视图层单测：用临时目录构造最小对局存档。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import json
import tempfile
import unittest
from pathlib import Path

from src import viewer


def make_game(root: Path) -> Path:
    d = root / "game" / "t"
    (d / "private" / "kimi").mkdir(parents=True)
    (d / "public").mkdir()
    state = {
        "phase": {"day": 1, "stage": "公共发言"},
        "winner": None,
        "players": [
            {"id": "kimi", "name": "奇米", "alive": True, "identity": "平民", "profession": "医师",
             "items": [{"name": "窃听器", "uses_left": 1, "available_day": 1}]},
            {"id": "gpt", "name": "索尔", "alive": False, "identity": "杀手", "profession": "黑客", "items": []},
        ],
        "positions": {"奇米": "会客室", "索尔": "书房"},
        "skill_uses": {"奇米": {"现场调查": 2}},
        "cases": [{"id": 1}],
        "orch": {"feed": ["公告a", "公告b"], "cursor": {}, "private": {}},
    }
    (d / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    (d / "public" / "log.md").write_text("\n".join(f"line{i}" for i in range(30)), encoding="utf-8")
    (d / "private" / "kimi" / "card.md").write_text("私密卡内容", encoding="utf-8")
    (d / "private" / "kimi" / "notes.md").write_text("笔记内容", encoding="utf-8")
    (d / "private" / "kimi" / "inner.md").write_text("独白内容", encoding="utf-8")
    (d / "host_log.jsonl").write_text(
        json.dumps({"ts": "t", "player": "奇米", "stage": "开局准备", "ok": True,
                    "latency": 1.5, "total_tokens": 10}) + "\n"
        + json.dumps({"ts": "t2", "player": "索尔", "stage": "公共发言", "ok": False,
                      "latency": 0.1, "error": "HTTP 500"}) + "\n",
        encoding="utf-8")
    return d


class ViewerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = make_game(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_status(self):
        text = viewer.render_status(self.dir)
        self.assertIn("第1天 · 公共发言", text)
        self.assertIn("奇米（活）@ 会客室｜平民/医师｜道具：窃听器×1｜技能：现场调查×2", text)
        self.assertIn("索尔（亡）", text)
        self.assertIn("公开事件 2 条｜案件 1 起", text)

    def test_log_tail(self):
        text = viewer.render_log(self.dir, 5)
        self.assertIn("line29", text)
        self.assertNotIn("line24", text)

    def test_peek_by_id_and_name(self):
        for key in ("kimi", "奇米"):
            text = viewer.render_peek(self.dir, key)
            self.assertIn("私密卡内容", text)
            self.assertIn("笔记内容", text)
            self.assertIn("独白内容", text)

    def test_peek_unknown(self):
        self.assertIn("未找到席位", viewer.render_peek(self.dir, "nobody"))

    def test_inner(self):
        self.assertEqual(viewer.render_inner(self.dir, "kimi"), "独白内容")
        self.assertIn("还没有内心独白", viewer.render_inner(self.dir, "gpt"))

    def test_host_log(self):
        text = viewer.render_host_log(self.dir, 10)
        self.assertIn("奇米 开局准备 ok 1.5s tok=10", text)
        self.assertIn("FAIL", text)
        self.assertIn("HTTP 500", text)

    def test_missing_files(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        (empty / "state.json").write_text(json.dumps({
            "phase": {"day": 1, "stage": "开局准备"}, "winner": None,
            "players": [], "positions": {}, "skill_uses": {}, "cases": [],
            "orch": {"feed": []},
        }), encoding="utf-8")
        self.assertIn("尚不存在", viewer.render_log(empty))
        self.assertIn("尚不存在", viewer.render_host_log(empty))
        self.assertIn("公开事件 0 条", viewer.render_status(empty))


class TestResolveGameDir(unittest.TestCase):
    """--game 参数解析：完整路径直接用；仅 id 时自动到 game/ 下找；都不存在时原样返回（下游报错）。"""

    def test_resolve(self):
        from src import resolve_game_dir
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "game"
            (base / "42").mkdir(parents=True)
            self.assertEqual(resolve_game_dir("42", base), base / "42")
            self.assertEqual(resolve_game_dir(str(base / "42"), base), base / "42")
            self.assertEqual(resolve_game_dir("99999", base), Path("99999"))


if __name__ == "__main__":
    unittest.main()
