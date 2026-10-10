#!/bin/bash
# 视频生成脚本 - 统一入口
# 用法:
#   首帧生视频: ./generate_video.sh -f <first_frame> -p "<prompt>" [options]
#   首尾帧生视频: ./generate_video.sh -f <first_frame> -l <last_frame> -p "<prompt>" [options]

set -e

API_KEY="${API_KEY:-}"
VIDEO_MODEL="doubao-seedance-1-5-pro-251215"

# 默认值
FIRST_FRAME=""
LAST_FRAME=""
PROMPT=""
OUTPUT_FILE="video_output.mp4"
DRAFT_MODE=false
GENERATE_AUDIO=false
RESOLUTION="480p"
WATERMARK=false
DURATION="auto"
RATIO="9:16"

# 解析参数
while getopts "f:l:p:o:d:a:r:w:t:g:h" opt; do
    case $opt in
        f) FIRST_FRAME="$OPTARG" ;;
        l) LAST_FRAME="$OPTARG" ;;
        p) PROMPT="$OPTARG" ;;
        o) OUTPUT_FILE="$OPTARG" ;;
        d) DRAFT_MODE="$OPTARG" ;;
        a) GENERATE_AUDIO="$OPTARG" ;;
        r) RESOLUTION="$OPTARG" ;;
        w) WATERMARK="$OPTARG" ;;
        t) DURATION="$OPTARG" ;;
        g) RATIO="$OPTARG" ;;
        h)
            echo "视频生成脚本 - Seedance 1.5 Pro"
            echo ""
            echo "用法:"
            echo "  首帧生视频: $0 -f <first_frame> -p \"<prompt>\" [options]"
            echo "  首尾帧生视频: $0 -f <first_frame> -l <last_frame> -p \"<prompt>\" [options]"
            echo ""
            echo "必填参数:"
            echo "  -f <file>   首帧图片文件"
            echo "  -p <text>   运动描述提示词"
            echo ""
            echo "可选参数:"
            echo "  -l <file>   尾帧图片文件 (提供则为首尾帧模式)"
            echo "  -o <file>   输出视频文件 (默认: video_output.mp4)"
            echo "  -d <bool>   草稿模式 (默认: false)"
            echo "  -a <bool>   是否生成声音 (默认: false)"
            echo "  -r <text>   分辨率 (默认: 480p, 可选: 720p, 1080p)"
            echo "  -w <bool>   是否水印 (默认: false)"
            echo "  -t <num>    时长秒数 (默认: auto 智能时长, 可选: 5, 10)"
            echo "  -g <text>   宽高比 (默认: 9:16, 可选: 16:9, 1:1, adaptive)"
            echo "  -h          显示帮助信息"
            echo ""
            echo "示例:"
            echo "  # 首帧生视频 (默认参数)"
            echo "  $0 -f frame_01.png -p \"dramatic camera movement\" -o video_01.mp4"
            echo ""
            echo "  # 首尾帧生视频"
            echo "  $0 -f frame_01.png -l frame_02.png -p \"smooth transition\" -o video_01_02.mp4"
            echo ""
            echo "  # 带声音、720P"
            echo "  $0 -f frame_01.png -p \"dramatic movement\" -a true -r 720p -o video_hd.mp4"
            echo ""
            echo "  # 草稿模式、智能时长"
            echo "  $0 -f frame_01.png -p \"dramatic movement\" -d true -t auto -o video_draft.mp4"
            exit 0
            ;;
        \?)
            echo "无效参数: -$OPTARG" >&2
            exit 1
            ;;
    esac
done

# 验证 API_KEY
if [ -z "$API_KEY" ]; then
    echo "错误: 未提供 API_KEY"
    echo "请先提供用于生图/生视频的 API_KEY，或设置 API_KEY 环境变量"
    exit 1
fi

# 验证必填参数
if [ -z "$FIRST_FRAME" ]; then
    echo "错误: 首帧图片 (-f) 是必填参数"
    exit 1
fi

if [ -z "$PROMPT" ]; then
    echo "错误: 运动描述 (-p) 是必填参数"
    exit 1
fi

if [ ! -f "$FIRST_FRAME" ]; then
    echo "错误: 首帧图片不存在: $FIRST_FRAME"
    exit 1
fi

# 判断模式
if [ -n "$LAST_FRAME" ]; then
    MODE="first_last"
    if [ ! -f "$LAST_FRAME" ]; then
        echo "错误: 尾帧图片不存在: $LAST_FRAME"
        exit 1
    fi
else
    MODE="first_only"
fi

# 处理布尔值参数
DRAFT_MODE_JSON=$([ "$DRAFT_MODE" == "true" ] && echo "true" || echo "false")
GENERATE_AUDIO_JSON=$([ "$GENERATE_AUDIO" == "true" ] && echo "true" || echo "false")
WATERMARK_JSON=$([ "$WATERMARK" == "true" ] && echo "true" || echo "false")

# 处理时长参数
if [ "$DURATION" == "auto" ]; then
    DURATION_JSON="null"
else
    DURATION_JSON="$DURATION"
fi

# 显示信息
echo "=== 视频生成开始 ==="
echo "模式: $([ "$MODE" == "first_last" ] && echo "首尾帧" || echo "首帧")"
echo "首帧图片: $FIRST_FRAME"
[ "$MODE" == "first_last" ] && echo "尾帧图片: $LAST_FRAME"
echo "运动描述: $PROMPT"
echo "--- 参数 ---"
echo "输出文件: $OUTPUT_FILE"
echo "草稿模式: $DRAFT_MODE"
echo "生成声音: $GENERATE_AUDIO"
echo "分辨率: $RESOLUTION"
echo "水印: $WATERMARK"
echo "时长: $DURATION"
echo "宽高比: $RATIO"
echo ""

# 转换图片为 base64
echo "[1/4] 转换图片为 base64..."
FIRST_BASE64=$(base64 -w 0 "$FIRST_FRAME")
FIRST_SIZE=$(stat -c%s "$FIRST_FRAME")
echo "  首帧大小: $((FIRST_SIZE / 1024)) KB"

if [ "$MODE" == "first_last" ]; then
    LAST_BASE64=$(base64 -w 0 "$LAST_FRAME")
    LAST_SIZE=$(stat -c%s "$LAST_FRAME")
    echo "  尾帧大小: $((LAST_SIZE / 1024)) KB"
fi

# 构建 JSON 请求体
echo "[2/4] 创建视频生成任务..."

if [ "$MODE" == "first_last" ]; then
    # 首尾帧模式
    if [ "$DURATION" == "auto" ]; then
        TASK_RESPONSE=$(curl -s -X POST "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks" \
            -H "Authorization: Bearer $API_KEY" \
            -H "Content-Type: application/json" \
            -d @- << EOF
{
    "model": "$VIDEO_MODEL",
    "content": [
        {
            "type": "text",
            "text": "$PROMPT"
        },
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,$FIRST_BASE64"
            },
            "role": "first_frame"
        },
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,$LAST_BASE64"
            },
            "role": "last_frame"
        }
    ],
    "draft_mode": $DRAFT_MODE_JSON,
    "generate_audio": $GENERATE_AUDIO_JSON,
    "resolution": "$RESOLUTION",
    "watermark": $WATERMARK_JSON,
    "ratio": "$RATIO"
}
EOF
)
    else
        TASK_RESPONSE=$(curl -s -X POST "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks" \
            -H "Authorization: Bearer $API_KEY" \
            -H "Content-Type: application/json" \
            -d @- << EOF
{
    "model": "$VIDEO_MODEL",
    "content": [
        {
            "type": "text",
            "text": "$PROMPT"
        },
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,$FIRST_BASE64"
            },
            "role": "first_frame"
        },
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,$LAST_BASE64"
            },
            "role": "last_frame"
        }
    ],
    "draft_mode": $DRAFT_MODE_JSON,
    "generate_audio": $GENERATE_AUDIO_JSON,
    "resolution": "$RESOLUTION",
    "watermark": $WATERMARK_JSON,
    "duration": $DURATION_JSON,
    "ratio": "$RATIO"
}
EOF
)
    fi
else
    # 首帧模式
    if [ "$DURATION" == "auto" ]; then
        TASK_RESPONSE=$(curl -s -X POST "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks" \
            -H "Authorization: Bearer $API_KEY" \
            -H "Content-Type: application/json" \
            -d @- << EOF
{
    "model": "$VIDEO_MODEL",
    "content": [
        {
            "type": "text",
            "text": "$PROMPT"
        },
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,$FIRST_BASE64"
            }
        }
    ],
    "draft_mode": $DRAFT_MODE_JSON,
    "generate_audio": $GENERATE_AUDIO_JSON,
    "resolution": "$RESOLUTION",
    "watermark": $WATERMARK_JSON,
    "ratio": "$RATIO"
}
EOF
)
    else
        TASK_RESPONSE=$(curl -s -X POST "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks" \
            -H "Authorization: Bearer $API_KEY" \
            -H "Content-Type: application/json" \
            -d @- << EOF
{
    "model": "$VIDEO_MODEL",
    "content": [
        {
            "type": "text",
            "text": "$PROMPT"
        },
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,$FIRST_BASE64"
            }
        }
    ],
    "draft_mode": $DRAFT_MODE_JSON,
    "generate_audio": $GENERATE_AUDIO_JSON,
    "resolution": "$RESOLUTION",
    "watermark": $WATERMARK_JSON,
    "duration": $DURATION_JSON,
    "ratio": "$RATIO"
}
EOF
)
    fi
fi

TASK_ID=$(echo "$TASK_RESPONSE" | jq -r '.id')

if [ "$TASK_ID" == "null" ] || [ -z "$TASK_ID" ]; then
    echo "错误: 创建任务失败"
    echo "$TASK_RESPONSE" | jq '.'
    exit 1
fi

echo "  任务ID: $TASK_ID"

# 等待任务完成
echo "[3/4] 等待视频生成..."
STATUS="running"
MAX_WAIT=600
WAIT_TIME=0

while [ "$STATUS" == "running" ] && [ $WAIT_TIME -lt $MAX_WAIT ]; do
    sleep 10
    WAIT_TIME=$((WAIT_TIME + 10))

    STATUS_RESPONSE=$(curl -s -X GET "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/$TASK_ID" \
        -H "Authorization: Bearer $API_KEY")

    STATUS=$(echo "$STATUS_RESPONSE" | jq -r '.status')

    if [ "$STATUS" == "succeeded" ]; then
        VIDEO_URL=$(echo "$STATUS_RESPONSE" | jq -r '.content.video_url')
        DURATION_RESULT=$(echo "$STATUS_RESPONSE" | jq -r '.duration')
        FPS=$(echo "$STATUS_RESPONSE" | jq -r '.framespersecond')
        RESOLUTION_RESULT=$(echo "$STATUS_RESPONSE" | jq -r '.resolution')

        echo "  ✓ 生成完成 (时长: ${DURATION_RESULT}s, 帧率: ${FPS}fps, 分辨率: ${RESOLUTION_RESULT})"

        # 下载视频
        echo "[4/4] 下载视频..."
        curl -s -o "$OUTPUT_FILE" "$VIDEO_URL"
        OUTPUT_SIZE=$(stat -c%s "$OUTPUT_FILE" 2>/dev/null || echo "0")

        if [ "$OUTPUT_SIZE" -gt 0 ]; then
            echo "  ✓ 保存成功: $OUTPUT_FILE ($((OUTPUT_SIZE / 1024 / 1024)) MB)"
            echo ""
            echo "=== 视频生成完成 ==="
            exit 0
        else
            echo "  ✗ 下载失败"
            exit 1
        fi

    elif [ "$STATUS" == "failed" ]; then
        ERROR_MSG=$(echo "$STATUS_RESPONSE" | jq -r '.error.message // "Unknown error"')
        echo "  ✗ 任务失败: $ERROR_MSG"
        exit 1
    fi

    echo "  状态: $STATUS (${WAIT_TIME}s)"
done

echo "  ✗ 超时"
exit 1
