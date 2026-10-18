"""从既有对局的指定席位前分叉另存为。

目标节点之前复用源存档 outbox 中的原始回复，不调用 API；这些回复仍按当前代码
依次经过编排器和引擎，重建 state、日志、笔记与 WIP。到达节点后保存新目录，
由现有 console/orchestrator --resume 接续实时生成。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import resolve_game_dir
from .deal import (
    RULES_VERSION,
    announcement,
    deal,
    load_personas,
    write_card,
    write_roster,
)
from .orchestrator import Orchestrator
from .providers import load_config

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent


class ReplayError(RuntimeError):
    pass


class ReplayBoundary(Exception):
    pass


@dataclass(frozen=True)
class ReplayPoint:
    day: int
    seat: str
    label: str


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _script_initial_order(source: Path) -> list[str] | None:
    path = source / "script.md"
    if not path.exists():
        return None
    m = re.search(r"^- 首日行动顺序：(.+)$", path.read_text(encoding="utf-8"), re.M)
    if not m:
        return None
    return [name.strip() for name in m.group(1).split("→")]


def _validate_initial(initial: dict, source_state: dict, source: Path) -> None:
    errors: list[str] = []
    for key in ("seed", "safe_item", "ruleset", "is_lite_rule"):
        if initial.get(key) != source_state.get(key):
            errors.append(f"{key}: {initial.get(key)!r} != {source_state.get(key)!r}")
    expected_order = _script_initial_order(source)
    if expected_order is not None and initial.get("order") != expected_order:
        errors.append("首日行动顺序与 script.md 不一致")

    final_players = source_state.get("players", [])
    by_key = {(p.get("id") or p["name"]): p for p in final_players}
    by_key.update({p["name"]: p for p in final_players})
    initial_players = initial.get("players", [])
    if len(initial_players) != len(final_players):
        errors.append(f"席位数：{len(initial_players)} != {len(final_players)}")
    for p in initial_players:
        q = by_key.get(p.get("id") or p["name"]) or by_key.get(p["name"])
        if q is None:
            errors.append(f"席位 {p['name']} 不在源存档中")
            continue
        for key in ("id", "name", "gender", "physique", "nationality", "profession", "item", "item_uses_left"):
            if p.get(key) != q.get(key):
                errors.append(f"{p['name']} 的 {key}: {p.get(key)!r} != {q.get(key)!r}")
        if p.get("extra_items", []) != q.get("extra_items", []):
            errors.append(f"{p['name']} 的额外初始道具不一致")
    if errors:
        raise ReplayError("初始分配与源存档不一致：" + "；".join(errors[:8]))


def _load_initial(source: Path, source_state: dict) -> tuple[dict, Path]:
    personas_path = source / "personas.toml"
    if not personas_path.exists():
        personas_path = ROOT / "personas.toml"
    snapshot = source / "deal.json"
    if snapshot.exists():
        initial = _read_json(snapshot)
    else:
        players, _settings = load_config(str(source / "players.toml"))
        try:
            initial = deal(
                int(source_state["seed"]),
                players,
                load_personas(personas_path),
                None,
                lite=bool(source_state.get("is_lite_rule")),
                exclude=set(source_state.get("excluded_seats", [])),
            )
        except SystemExit as e:
            raise ReplayError(f"无法按源存档重新生成开局：{e}") from e
    if initial.get("phase") != {"day": 1, "stage": "开局准备"}:
        raise ReplayError("deal.json 不是第 1 天开局准备状态的初始快照")
    _validate_initial(initial, source_state, source)
    initial["rules_version"] = RULES_VERSION
    return initial, personas_path


def _copy_initial_files(source: Path, target: Path, initial: dict, personas_path: Path) -> None:
    (target / "public").mkdir(parents=True)
    for p in initial["players"]:
        seat = p["id"] or p["name"]
        (target / "private" / seat).mkdir(parents=True)
        (target / "inbox" / seat).mkdir(parents=True)
        (target / "outbox" / seat).mkdir(parents=True)

    (target / "seed.txt").write_text(f"{initial['seed']}\n", encoding="utf-8")
    initial_json = json.dumps(initial, ensure_ascii=False, indent=2) + "\n"
    (target / "deal.json").write_text(initial_json, encoding="utf-8")
    (target / "state.json").write_text(initial_json, encoding="utf-8")

    rules_path = ROOT / "docs" / initial["ruleset"]
    if not rules_path.exists():
        raise ReplayError(f"找不到当前规则书：{rules_path}")
    shutil.copyfile(rules_path, target / "rules.md")
    shutil.copyfile(source / "players.toml", target / "players.toml")
    shutil.copyfile(personas_path, target / "personas.toml")

    for filename, fallback in (
        ("roster.md", lambda: write_roster(initial["players"])),
        ("announcement.md", lambda: announcement(initial["safe_item"])),
    ):
        src = source / "public" / filename
        if src.exists():
            shutil.copyfile(src, target / "public" / filename)
        else:
            (target / "public" / filename).write_text(fallback(), encoding="utf-8")

    for p in initial["players"]:
        seat = p["id"] or p["name"]
        src = source / "private" / seat / "card.md"
        dst = target / "private" / seat / "card.md"
        if src.exists():
            shutil.copyfile(src, dst)
        else:
            dst.write_text(write_card(p, initial["players"], lite=bool(initial.get("is_lite_rule"))), encoding="utf-8")


class ReplayOrchestrator(Orchestrator):
    def __init__(self, game_dir: Path, source: Path, point: ReplayPoint):
        super().__init__(game_dir)
        self.source = source
        self.point = point
        self.reached = False
        self.replayed_calls = 0

    def call_ai(self, p: dict, label: str, briefing: str) -> dict:
        day = self.state["phase"]["day"]
        seat = self.seat(p)
        if day == self.point.day and label == self.point.label and self.point.seat in (seat, p["name"]):
            self.reached = True
            raise ReplayBoundary
        if day > self.point.day:
            raise ReplayError("已越过目标日期仍未到达分叉点")

        filename = f"day{day}-{label}.md"
        recorded = self.source / "outbox" / seat / filename
        if not recorded.exists():
            raise ReplayError(f"缺少目标前的原始回复：{recorded}")

        seat_dir_in = self.dir / "inbox" / seat
        seat_dir_out = self.dir / "outbox" / seat
        seat_dir_in.mkdir(parents=True, exist_ok=True)
        seat_dir_out.mkdir(parents=True, exist_ok=True)
        (seat_dir_in / filename).write_text(briefing, encoding="utf-8")
        content = recorded.read_text(encoding="utf-8")
        (seat_dir_out / filename).write_text(content, encoding="utf-8")
        thinking = recorded.with_name(recorded.stem + "-thinking.md")
        if thinking.exists():
            shutil.copyfile(thinking, seat_dir_out / thinking.name)

        try:
            payload = self.extract_payload(content)
        except Exception:
            payload = {"speech": self.salvage_speech(content), "inner": "", "notes": "", "actions": []}
        self._commit_feed(p["name"])
        payload.setdefault("speech", "")
        payload.setdefault("inner", "")
        payload.setdefault("notes", "")
        actions = payload.get("actions")
        payload["actions"] = actions if isinstance(actions, list) else []
        self._fire("on_payload", p["name"], label, payload)
        self.archive_private(p, label, payload)
        self.replayed_calls += 1
        return payload


def prepare_replay(source: Path, out: Path, point: ReplayPoint) -> dict:
    source = Path(source)
    out = Path(out)
    if not (source / "state.json").exists():
        raise ReplayError(f"源存档缺少 state.json：{source}")
    source_resolved = source.resolve()
    out_resolved = out.resolve()
    if out_resolved == source_resolved or out_resolved.is_relative_to(source_resolved):
        raise ReplayError("输出目录必须独立于源存档，不能覆盖或嵌入源目录")
    if out.exists():
        raise ReplayError(f"输出目录已存在：{out}")
    if point.day < 1:
        raise ReplayError("分叉日期必须不小于 1")

    source_state = _read_json(source / "state.json")
    initial, personas_path = _load_initial(source, source_state)
    if not any(point.seat in (p.get("id"), p["name"]) for p in initial["players"]):
        raise ReplayError(f"分叉席位不在本局阵容中：{point.seat}")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{out.name}.tmp-", dir=out.parent))
    try:
        _copy_initial_files(source, tmp, initial, personas_path)
        orch = ReplayOrchestrator(tmp, source, point)
        orch.log_header(f"第{orch.state['phase']['day']}天 · 开局")
        try:
            orch.run_game()
        except ReplayBoundary:
            pass
        if not orch.reached:
            raise ReplayError(
                f"对局结束仍未到达分叉点：第 {point.day} 天 · {point.seat} · {point.label}"
            )
        orch.save_state()
        metadata = {
            "source": str(source),
            "source_rules_version": source_state.get("rules_version"),
            "rules_version": RULES_VERSION,
            "point": {"day": point.day, "seat": point.seat, "label": point.label},
            "replayed_calls": orch.replayed_calls,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        _write_json(tmp / "replay.json", metadata)
        tmp.rename(out)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return metadata


def main() -> None:
    ap = argparse.ArgumentParser(description="在指定席位的 AI 调用前，将对局分叉另存为")
    ap.add_argument("--game", required=True, help="源对局目录（只读；如 game/43501 或仅 id 43501）")
    ap.add_argument("--day", required=True, type=int, help="分叉节点所在天数")
    ap.add_argument("--seat", required=True, help="分叉席位 id 或角色名")
    ap.add_argument("--label", required=True, help="调用标签，如 自由活动-房间行动")
    ap.add_argument("--out", required=True, help="新分支目录（必须不存在）")
    args = ap.parse_args()
    source, out = resolve_game_dir(args.game), Path(args.out)
    try:
        metadata = prepare_replay(source, out, ReplayPoint(args.day, args.seat, args.label))
    except ReplayError as e:
        raise SystemExit(f"重演失败：{e}") from e
    print(f"已重放 {metadata['replayed_calls']} 次历史调用，分支停在：第 {args.day} 天 · {args.seat} · {args.label} 之前")
    print(f"分支目录：{out}")
    print("console 实时观战续跑：")
    print(f"  python -m src.console --game {out} --resume")
    print("无头续跑：")
    print(f"  python -m src.orchestrator --game {out} --resume")


if __name__ == "__main__":
    main()
