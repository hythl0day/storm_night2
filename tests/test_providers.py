"""providers._ThinkTagger 单元测试：<think>…</think> 段识别为推理内容（跨 chunk 安全，不经网络）。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import unittest
from unittest import mock

from src.providers import Player, _ThinkTagger, chat


def run(tagger: _ThinkTagger, chunks: list[str]) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for c in chunks:
        out.extend(tagger.feed(c))
    out.extend(tagger.flush())
    return out


class TestThinkTagger(unittest.TestCase):
    def test_plain_content(self):
        self.assertEqual(run(_ThinkTagger(), ["hello", " world"]), [("hello", False), (" world", False)])

    def test_single_think_block(self):
        out = run(_ThinkTagger(), ["<think>想一想</think>正文"])
        self.assertEqual(out, [("想一想", True), ("正文", False)])

    def test_tag_split_across_chunks(self):
        out = run(_ThinkTagger(), ["<th", "ink>想", "一想</t", "hink>正文"])
        self.assertEqual(out, [("想", True), ("一想", True), ("正文", False)])

    def test_split_inside_text_kept(self):
        # 尾部像标签碎片但不是时照常吐出（'<t' 若拼不上 '<think>' 会在下一块原样输出）
        out = run(_ThinkTagger(), ["abc<", "def>"])
        self.assertEqual(out, [("abc", False), ("<def>", False)])

    def test_unclosed_think_flushes_as_reasoning(self):
        out = run(_ThinkTagger(), ["<think>没写完"])
        self.assertEqual(out, [("没写完", True)])

    def test_multiple_blocks(self):
        out = run(_ThinkTagger(), ["<think>a</think>x<think>b</think>y"])
        self.assertEqual(out, [("a", True), ("x", False), ("b", True), ("y", False)])

    def test_empty_and_tail_only(self):
        tagger = _ThinkTagger()
        self.assertEqual(tagger.feed(""), [])
        self.assertEqual(run(_ThinkTagger(), ["<t"]), [("<t", False)])


class TestEmptyResponseRetry(unittest.TestCase):
    """v4.0.7（game/247671 复盘：apinebula 的 Claude 上游约 3/4 概率返回空 200 → 席位整环节沉默）：
    HTTP 200 但正文为空视为可重试错误，chat() 内部重试；重试耗尽才 ok=False。"""

    def _player(self):
        return Player(name="克劳德", model="m", base_url="http://x", api_key_env="K",
                      api_type="anthropic", api_key="k")

    def test_empty_then_content_recovers(self):
        responses = iter([("", {}, "end_turn", ""), ("", {}, "end_turn", ""),
                          ('```json\n{"speech": "好"}\n```', {"total_tokens": 10}, "end_turn", "")])
        with mock.patch("src.providers._chat_anthropic", side_effect=lambda *a, **k: next(responses)), \
             mock.patch("src.providers.time.sleep"):
            r = chat(self._player(), [{"role": "user", "content": "hi"}])
        self.assertTrue(r.ok)
        self.assertIn("speech", r.content)

    def test_all_empty_fails_not_ok(self):
        with mock.patch("src.providers._chat_anthropic", return_value=("", {}, "end_turn", "")), \
             mock.patch("src.providers.time.sleep"):
            r = chat(self._player(), [{"role": "user", "content": "hi"}])
        self.assertFalse(r.ok)
        self.assertIn("空响应", r.error)


if __name__ == "__main__":
    unittest.main()
