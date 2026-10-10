"""prompt 模板加载器。

模板文件放在项目根目录的 prompt/ 下（与 src/ 平级）。
用 __file__ 锚定路径，从任何工作目录运行都能正确找到模板。
模板支持 str.format 占位符（如 {name}、{profile}）。
"""
from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompt"


def load_prompt(filename: str) -> str:
    return (PROMPT_DIR / filename).read_text(encoding="utf-8").strip()


def render(filename: str, **kwargs) -> str:
    return load_prompt(filename).format(**kwargs)
