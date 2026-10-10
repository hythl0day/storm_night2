#!/bin/bash
# 视频任务查询脚本
# 用法: ./query_video_task.sh <task_id> [output_file]

set -e

API_KEY="${API_KEY:-}"

TASK_ID="$1"
OUTPUT_FILE="${2:-}"

if [ -z "$API_KEY" ]; then
    echo "错误: 未提供 API_KEY"
    echo "请先提供用于生图/生视频的 API_KEY，或设置 API_KEY 环境变量"
    exit 1
fi

if [ -z "$TASK_ID" ]; then
    echo "用法: $0 <task_id> [output_file]"
    echo "示例: $0 cgt-20260314172446-86g4s video.mp4"
    echo ""
    echo "参数:"
    echo "  task_id      任务ID (必填)"
    echo "  output_file  输出文件 (可选，提供则自动下载视频)"
    exit 1
fi

echo "=== 查询视频生成任务 ==="
echo "任务ID: $TASK_ID"
echo ""

# 循环查询直到完成
MAX_WAIT=600
WAIT_TIME=0
POLL_INTERVAL=10

while [ $WAIT_TIME -lt $MAX_WAIT ]; do
    # 查询任务状态
    RESPONSE=$(curl -s -X GET "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/$TASK_ID" \
        -H "Authorization: Bearer $API_KEY")

    STATUS=$(echo "$RESPONSE" | jq -r '.status')

    case "$STATUS" in
        "succeeded")
            # 提取视频信息
            VIDEO_URL=$(echo "$RESPONSE" | jq -r '.content.video_url')
            DURATION=$(echo "$RESPONSE" | jq -r '.duration')
            FPS=$(echo "$RESPONSE" | jq -r '.framespersecond')
            RESOLUTION=$(echo "$RESPONSE" | jq -r '.resolution')
            RATIO=$(echo "$RESPONSE" | jq -r '.ratio')
            MODEL=$(echo "$RESPONSE" | jq -r '.model')
            CREATED=$(echo "$RESPONSE" | jq -r '.created_at')
            UPDATED=$(echo "$RESPONSE" | jq -r '.updated_at')

            echo "✓ 任务完成!"
            echo ""
            echo "=== 视频信息 ==="
            echo "模型: $MODEL"
            echo "时长: ${DURATION}s"
            echo "帧率: ${FPS}fps"
            echo "分辨率: $RESOLUTION"
            echo "宽高比: $RATIO"
            echo "创建时间: $(date -d @$CREATED '+%Y-%m-%d %H:%M:%S')"
            echo "完成时间: $(date -d @$UPDATED '+%Y-%m-%d %H:%M:%S')"
            echo ""
            echo "视频URL:"
            echo "$VIDEO_URL"
            echo ""

            # 如果指定了输出文件，下载视频
            if [ -n "$OUTPUT_FILE" ]; then
                echo "下载视频到: $OUTPUT_FILE"
                curl -s -o "$OUTPUT_FILE" "$VIDEO_URL"
                FILE_SIZE=$(stat -c%s "$OUTPUT_FILE" 2>/dev/null || echo "0")
                if [ "$FILE_SIZE" -gt 0 ]; then
                    echo "✓ 下载成功: $OUTPUT_FILE ($((FILE_SIZE / 1024 / 1024)) MB)"
                else
                    echo "✗ 下载失败"
                    exit 1
                fi
            fi

            # 输出 JSON 格式结果
            echo ""
            echo "=== JSON 结果 ==="
            echo "$RESPONSE" | jq '{
                id: .id,
                status: .status,
                video_url: .content.video_url,
                duration: .duration,
                fps: .framespersecond,
                resolution: .resolution,
                ratio: .ratio,
                model: .model,
                usage: .usage
            }'

            exit 0
            ;;

        "failed")
            ERROR_CODE=$(echo "$RESPONSE" | jq -r '.error.code // "Unknown"')
            ERROR_MSG=$(echo "$RESPONSE" | jq -r '.error.message // "Unknown error"')

            echo "✗ 任务失败"
            echo ""
            echo "=== 错误信息 ==="
            echo "错误码: $ERROR_CODE"
            echo "错误信息: $ERROR_MSG"
            echo ""
            echo "=== JSON 结果 ==="
            echo "$RESPONSE" | jq '.'
            exit 1
            ;;

        "running"|"pending")
            WAIT_TIME=$((WAIT_TIME + POLL_INTERVAL))
            echo "[$(date '+%H:%M:%S')] 状态: $STATUS (${WAIT_TIME}s / ${MAX_WAIT}s)"
            sleep $POLL_INTERVAL
            ;;

        *)
            echo "未知状态: $STATUS"
            echo "$RESPONSE" | jq '.'
            exit 1
            ;;
    esac
done

echo ""
echo "✗ 查询超时 (超过 ${MAX_WAIT}s)"
exit 1
