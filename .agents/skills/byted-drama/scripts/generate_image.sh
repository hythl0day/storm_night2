#!/bin/bash
# generate_image.sh - 使用 Seedream API 生成分镜图片
#
# 用法:
#   ./generate_image.sh [选项]
#
# 选项:
#   -p, --prompt <文本>      图片描述提示词（必需）
#   -o, --output <文件>      输出文件路径（默认: output.png）
#   -e, --endpoint <id>      Seedream endpoint ID（默认: ep-20260225094435-gzb5q）
#   -s, --size <尺寸>        图片尺寸（默认: 1440x2560）
#   -n, --count <数量>       生成数量（默认: 1）
#   -k, --api-key <key>      API Key（或使用 API_KEY 环境变量）
#   -h, --help               显示帮助信息
#
# 示例:
#   ./generate_image.sh -p "赛博朋克风格的霓虹街道，夜晚，雨滴反光" -o frame_01.png
#   ./generate_image.sh --prompt "古风少女，白衣胜雪，剑指苍穹" --output frame_02.png

set -euo pipefail

# 默认值
DEFAULT_ENDPOINT="doubao-seedream-5-0-260128"
DEFAULT_SIZE="1440x2560"
DEFAULT_COUNT=1
DEFAULT_OUTPUT="output.png"

# API 配置
API_URL="https://ark.cn-beijing.volces.com/api/v3/images/generations"

# 帮助信息
show_help() {
    cat << EOF
generate_image.sh - 使用 Seedream API 生成分镜图片

用法:
  $0 [选项]

选项:
  -p, --prompt <文本>      图片描述提示词（必需）
  -o, --output <文件>      输出文件路径（默认: $DEFAULT_OUTPUT）
  -e, --endpoint <id>      Seedream endpoint ID（默认: $DEFAULT_ENDPOINT）
  -s, --size <尺寸>        图片尺寸（默认: $DEFAULT_SIZE）
  -n, --count <数量>       生成数量（默认: $DEFAULT_COUNT）
  -k, --api-key <key>      API Key（或使用 API_KEY 环境变量）
  -h, --help               显示帮助信息

常见尺寸:
  - 1440x2560  (9:16 竖屏，默认，适合抖音/小红书)
  - 2560x1440  (16:9 横屏，适合 B站/YouTube)
  - 1024x1024  (1:1 方形，适合微信朋友圈)

示例:
  $0 -p "赛博朋克风格的霓虹街道，夜晚，雨滴反光" -o frame_01.png
  $0 --prompt "古风少女，白衣胜雪，剑指苍穹" --output frame_02.png --size 2560x1440

环境变量:
  API_KEY             Seedream API 密钥

EOF
    exit 0
}

# 参数解析
PROMPT=""
OUTPUT="$DEFAULT_OUTPUT"
ENDPOINT="$DEFAULT_ENDPOINT"
SIZE="$DEFAULT_SIZE"
COUNT="$DEFAULT_COUNT"
API_KEY="${API_KEY:-}"

while [[ $# -gt 0 ]]; do
    case $1 in
        -p|--prompt)
            PROMPT="$2"
            shift 2
            ;;
        -o|--output)
            OUTPUT="$2"
            shift 2
            ;;
        -e|--endpoint)
            ENDPOINT="$2"
            shift 2
            ;;
        -s|--size)
            SIZE="$2"
            shift 2
            ;;
        -n|--count)
            COUNT="$2"
            shift 2
            ;;
        -k|--api-key)
            API_KEY="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            ;;
        *)
            echo "❌ 未知选项: $1" >&2
            exit 1
            ;;
    esac
done

# 验证必需参数
if [[ -z "$PROMPT" ]]; then
    echo "❌ 错误: 缺少提示词参数 (-p, --prompt)" >&2
    exit 1
fi

if [[ -z "$API_KEY" ]]; then
    echo "❌ 错误: 未提供 API Key" >&2
    echo "   请先提供用于生图/生视频的 API_KEY，或设置 API_KEY 环境变量" >&2
    exit 1
fi

# 确保输出目录存在
OUTPUT_DIR=$(dirname "$OUTPUT")
if [[ -n "$OUTPUT_DIR" && "$OUTPUT_DIR" != "." ]]; then
    mkdir -p "$OUTPUT_DIR"
fi

echo "🎨 生成图片中..."
echo "   提示词: $PROMPT"
echo "   尺寸: $SIZE"
echo "   输出: $OUTPUT"

# 构建请求体
REQUEST_BODY=$(cat <<EOF
{
  "model": "$ENDPOINT",
  "prompt": $(echo "$PROMPT" | jq -Rs .),
  "size": "$SIZE",
  "n": $COUNT
}
EOF
)

# 调用 API
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $API_KEY" \
    -d "$REQUEST_BODY")

# 分离响应体和状态码
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
RESPONSE_BODY=$(echo "$RESPONSE" | sed '$d')

# 检查 HTTP 状态码
if [[ "$HTTP_CODE" != "200" ]]; then
    echo "❌ API 调用失败 (HTTP $HTTP_CODE)" >&2
    echo "$RESPONSE_BODY" | jq -r '.error.message // .message // "未知错误"' 2>/dev/null || echo "$RESPONSE_BODY" >&2
    exit 1
fi

# 提取图片 URL
IMAGE_URL=$(echo "$RESPONSE_BODY" | jq -r '.data[0].url // empty')

if [[ -z "$IMAGE_URL" ]]; then
    echo "❌ 响应中未找到图片 URL" >&2
    echo "   响应: $RESPONSE_BODY" >&2
    exit 1
fi

# 下载图片
echo "⬇️  下载图片..."
curl -s -o "$OUTPUT" "$IMAGE_URL"

if [[ $? -eq 0 && -f "$OUTPUT" ]]; then
    FILE_SIZE=$(du -h "$OUTPUT" | cut -f1)
    echo "✅ 图片已保存: $OUTPUT ($FILE_SIZE)"
    
    # 返回文件路径供后续处理
    echo "$OUTPUT"
else
    echo "❌ 图片下载失败" >&2
    exit 1
fi
