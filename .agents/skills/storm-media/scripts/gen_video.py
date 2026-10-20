"""Seedance 2.5 视频生成 CLI（storm-media）。

用法：
    python gen_video.py --prompt "...（台词用{}括起，音乐()音效<>字幕【】）"
                        [--first-frame 图] [--last-frame 图] [--ref 素材 ...]
                        [--duration 4-30|-1] [--ratio adaptive] [--resolution 720p]
                        [--no-audio] [--watermark] [--out-dir 目录]

- 首帧/首尾帧任务 ratio 强制 adaptive（宽高比锁定首帧）；参考素材任务 ratio 自由。
- --ref 为 reference_image 素材（图片可本地路径/URL/asset://；视频参考仅公网 URL，2.5 不收 base64）。
- prompt 中素材指代：@图片1/@视频1/@音频1，按 content 数组内同类素材顺序编号。
- 创建后轮询（5s/次，上限 20 分钟），产物 URL 仅 24 小时有效，成功立即下载落盘。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import ark

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[4]  # 项目根


def main() -> None:
    ap = argparse.ArgumentParser(description="Seedance 2.5 视频生成（AgentPlan）")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--first-frame", help="首帧图（本地路径/URL/asset://）")
    ap.add_argument("--last-frame", help="尾帧图（须与首帧同画幅）")
    ap.add_argument("--ref", action="append", default=[], help="参考素材（图片/音频可本地；视频仅 URL）")
    ap.add_argument("--duration", type=int, default=-1, help="4–30 秒，或 -1 模型自选（默认 -1）")
    ap.add_argument("--ratio", default="adaptive",
                    choices=["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"])
    ap.add_argument("--resolution", default="720p", choices=["480p", "720p", "1080p"])
    ap.add_argument("--no-audio", action="store_true", help="不生成音频（默认生成）")
    ap.add_argument("--watermark", action="store_true", help="添加「AI生成」水印（默认不加）")
    ap.add_argument("--output-format", default="mp4", choices=["mp4", "mov"])
    ap.add_argument("--out-dir", help="输出目录（缺省 media/）")
    args = ap.parse_args()

    if args.last_frame and not args.first_frame:
        raise SystemExit("--last-frame 需与 --first-frame 同时使用")
    if args.duration != -1 and not (4 <= args.duration <= 30):
        raise SystemExit("--duration 取值 4–30 或 -1")

    content: list[dict] = [{"type": "text", "text": args.prompt}]
    if args.first_frame:
        content.append({"type": "image_url",
                        "image_url": {"url": ark.resolve_asset(args.first_frame)},
                        "role": "first_frame"})
    if args.last_frame:
        content.append({"type": "image_url",
                        "image_url": {"url": ark.resolve_asset(args.last_frame)},
                        "role": "last_frame"})
    for ref in args.ref:
        url = ark.resolve_asset(ref)
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "reference_image"})

    ratio = "adaptive" if args.first_frame else args.ratio  # 首帧任务锁定 adaptive
    body = {
        "model": ark.MODEL_VIDEO,
        "content": content,
        "ratio": ratio,
        "duration": args.duration,
        "resolution": args.resolution,
        "generate_audio": not args.no_audio,
        "watermark": bool(args.watermark),
        "output_format": args.output_format,
        "return_last_frame": True,  # 尾帧是信任产物（可作下一段首帧），有则落盘
    }

    print(f"创建任务：{ark.MODEL_VIDEO}｜{args.resolution}｜{ratio}｜时长 {args.duration}｜"
          f"音频 {'开' if not args.no_audio else '关'}｜素材 {len(content) - 1} 个")
    task_id = ark.create_task(body)
    print(f"任务 ID：{task_id}（轮询中，5s/次）")
    result = ark.poll_task(task_id)

    status = result.get("status")
    if status != "succeeded":
        err = result.get("error") or {}
        raise SystemExit(f"任务 {status}：{err.get('code', '')} {err.get('message', '')}")

    video_url = (result.get("content") or {}).get("video_url")
    if not video_url:
        raise SystemExit(f"任务成功但无视频 URL：{str(result)[:500]}")
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "media"
    out = ark.download(video_url, out_dir / f"{task_id}.{args.output_format}")
    ark.register_asset(out, video_url, "video", model=ark.MODEL_VIDEO, task_id=task_id,
                       prompt=args.prompt[:200])
    print(f"已保存：{out}（已登记 registry.json）")
    last_frame = (result.get("content") or {}).get("last_frame_url")
    if last_frame:
        lf = ark.download(last_frame, out_dir / f"{task_id}-last.jpg")
        # 尾帧也是信任产物（可作下一次首帧输入，免人脸审核）
        ark.register_asset(lf, last_frame, "last_frame", model=ark.MODEL_VIDEO, task_id=task_id)
        print(f"尾帧已保存：{lf}")


if __name__ == "__main__":
    main()
