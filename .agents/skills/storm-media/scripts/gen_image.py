"""Seedream 图片生成 CLI（storm-media）。

用法：
    python gen_image.py --prompt "..." [--ref 图1 --ref 图2 ...] [--model lite|pro]
                        [--size 2K] [--group N] [--out 输出路径] [--watermark]

- 参考图（--ref）：本地路径（转 base64 data URI）/ http(s) URL / asset:// ID；
  Lite ≤14 张、Pro ≤10 张；prompt 里用「图1/图2」指代，顺序与 --ref 传入顺序一致。
- 组图（--group N）：Lite 连贯组图，只发 sequential_image_generation=auto，张数由 prompt 语义引导。
- 产物 URL 仅 24 小时有效，生成后立即下载落盘。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import ark

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[4]  # 项目根（.agents/skills/storm-media/scripts/ 上四级）


def main() -> None:
    ap = argparse.ArgumentParser(description="Seedream 图片生成（AgentPlan）")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--ref", action="append", default=[], help="参考图：本地路径/URL/asset://（可重复）")
    ap.add_argument("--model", choices=["lite", "pro"], default="lite")
    ap.add_argument("--size", default="2K", help="Lite: 2K/3K/4K；Pro: 1K/1.5K/2K 或 宽x高")
    ap.add_argument("--group", type=int, default=0, help="Lite 连贯组图张数（>1 启用；≤15）")
    ap.add_argument("--out", help="输出路径（多张时自动加序号）；缺省 media/<时间戳>.<扩展名>")
    ap.add_argument("--watermark", action="store_true", help="添加「AI生成」水印（默认不加）")
    args = ap.parse_args()

    model_id = ark.MODEL_IMAGE[args.model]
    limit = 14 if args.model == "lite" else 10
    if len(args.ref) > limit:
        raise SystemExit(f"参考图 {len(args.ref)} 张超过 {args.model} 上限 {limit}")

    body: dict = {
        "model": model_id,
        "prompt": args.prompt,
        "size": args.size,
        "response_format": "url",
        "watermark": bool(args.watermark),
    }
    if args.ref:
        refs = [ark.resolve_asset(r) for r in args.ref]
        body["image"] = refs[0] if len(refs) == 1 else refs
    if args.group > 1:
        if args.model != "lite":
            raise SystemExit("连贯组图仅 Lite 支持")
        if len(args.ref) + args.group > 15:
            raise SystemExit("组图：参考图数 + 生成图数 ≤ 15")
        body["sequential_image_generation"] = "auto"

    print(f"生成中：model={model_id} size={args.size} 参考图 {len(args.ref)} 张"
          + (f" 组图 {args.group} 张" if args.group > 1 else ""))
    data = ark.image_generation(body)
    urls = [d["url"] for d in data.get("data", []) if d.get("url")]
    if not urls:
        raise SystemExit(f"响应无图片：{str(data)[:500]}")

    saved = []
    for i, url in enumerate(urls):
        if args.out:
            out = Path(args.out)
            if len(urls) > 1:
                out = out.with_name(f"{out.stem}-{i + 1}{out.suffix or '.jpeg'}")
        else:
            ext = ".png" if ".png" in url else ".jpeg"
            out = ROOT / "media" / f"{datetime.now():%Y%m%d-%H%M%S}-{i + 1}{ext}"
        ark.download(url, out)
        ark.register_asset(out, url, "image", model=model_id,
                           prompt=args.prompt[:200], refs=[r[:120] for r in args.ref])
        saved.append(out)
        print(f"已保存：{out}（已登记 registry.json）")
    print(f"== 完成：{len(saved)} 张 ==")


if __name__ == "__main__":
    main()
