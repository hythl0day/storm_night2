#!/bin/bash
# 视频合成脚本
# 用法: ./merge_videos.sh <视频目录> <输出文件>

set -e

VIDEO_DIR="${1:-videos}"
OUTPUT="${2:-final_video.mp4}"
CONCAT_LIST="concat_list.txt"

# 生成合并列表
echo "生成合并列表..."
ls "$VIDEO_DIR"/*.mp4 | sort -V | while read f; do
    echo "file '$f'"
done > "$CONCAT_LIST"

# 合并视频
echo "合并视频..."
ffmpeg -y -f concat -safe 0 -i "$CONCAT_LIST" -c copy "$OUTPUT"

# 显示结果
SIZE=$(ls -lh "$OUTPUT" | awk '{print $5}')
DURATION=$(ffprobe -v quiet -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$OUTPUT" 2>/dev/null | cut -d. -f1)

echo "完成: $OUTPUT"
echo "大小: $SIZE"
echo "时长: ${DURATION}秒"
