#!/bin/bash
# new_project.sh - 创建新的视频创作项目
#
# 用法:
#   ./new_project.sh [选项]
#
# 选项:
#   -n, --name <名称>       项目名称（必需）
#   -t, --type <类型>       项目类型：drama/short_film/movie（默认：drama）
#   -s, --style <风格>      视觉风格（默认根据类型自动选择）
#   -r, --resolution <分辨率> 视频分辨率（默认根据类型自动选择）
#   -d, --duration <时长>   预计时长，单位秒（默认：60）
#   -o, --output <目录>     项目根目录（默认：~/workspace/agent/workspace/projects）
#   -h, --help              显示帮助信息
#
# 示例:
#   ./new_project.sh --name "赛博朋克之梦" --type drama --style anime
#   ./new_project.sh -n "仙剑奇缘" -t short_film -s live_action

set -euo pipefail

# 默认值
DEFAULT_TYPE="drama"
DEFAULT_STYLE=""
DEFAULT_RESOLUTION=""
DEFAULT_DURATION=60
PROJECTS_ROOT="${PROJECTS_ROOT:-/home/gem/workspace/agent/workspace/projects}"

# 类型配置
declare -A TYPE_CONFIG=(
    ["drama"]="漫剧|1440x2560|9:16|anime"
    ["short_film"]="短剧|1920x1080|16:9|movie"
    ["movie"]="电影|1920x1080|16:9|movie"
)

# 风格映射
declare -A STYLE_NAMES=(
    ["anime"]="动漫"
    ["japanese_anime"]="日漫"
    ["american_comic"]="美漫"
    ["chinese_anime"]="国漫"
    ["movie"]="电影"
    ["documentary"]="纪录片"
    ["live_action"]="真人"
)

# 类型名称映射
declare -A TYPE_NAMES=(
    ["drama"]="漫剧"
    ["short_film"]="短剧"
    ["movie"]="电影"
)

# 帮助信息
show_help() {
    cat << EOF
new_project.sh - 创建新的视频创作项目

用法:
  $0 [选项]

选项:
  -n, --name <名称>       项目名称（必需）
  -t, --type <类型>       项目类型（默认：drama）
                          - drama: 漫剧（AI动漫风格）
                          - short_film: 短剧（真人剧情）
                          - movie: 电影（完整作品）
  -s, --style <风格>      视觉风格
                          漫剧: anime/japanese_anime/american_comic/chinese_anime
                          短剧/电影: movie/documentary/live_action
  -r, --resolution <分辨率> 视频分辨率（默认根据类型）
  -d, --duration <时长>   预计时长，单位秒（默认：60）
  -o, --output <目录>     项目根目录（默认：~/workspace/agent/workspace/projects）
  -h, --help              显示帮助信息

示例:
  $0 --name "赛博朋克之梦" --type drama --style anime
  $0 -n "仙剑奇缘" -t short_film -s live_action -r 1920x1080

EOF
    exit 0
}

# 参数解析
PROJECT_NAME=""
PROJECT_TYPE="$DEFAULT_TYPE"
PROJECT_STYLE="$DEFAULT_STYLE"
PROJECT_RESOLUTION="$DEFAULT_RESOLUTION"
PROJECT_DURATION="$DEFAULT_DURATION"

while [[ $# -gt 0 ]]; do
    case $1 in
        -n|--name)
            PROJECT_NAME="$2"
            shift 2
            ;;
        -t|--type)
            PROJECT_TYPE="$2"
            shift 2
            ;;
        -s|--style)
            PROJECT_STYLE="$2"
            shift 2
            ;;
        -r|--resolution)
            PROJECT_RESOLUTION="$2"
            shift 2
            ;;
        -d|--duration)
            PROJECT_DURATION="$2"
            shift 2
            ;;
        -o|--output)
            PROJECTS_ROOT="$2"
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
if [[ -z "$PROJECT_NAME" ]]; then
    echo "❌ 错误: 缺少项目名称 (-n, --name)" >&2
    exit 1
fi

# 验证项目类型
if [[ ! -v TYPE_CONFIG["$PROJECT_TYPE"] ]]; then
    echo "❌ 错误: 无效的项目类型 '$PROJECT_TYPE'" >&2
    echo "   可用类型: drama, short_film, movie" >&2
    exit 1
fi

# 解析类型默认配置
IFS='|' read -r TYPE_LABEL DEFAULT_RES DEFAULT_RATIO DEFAULT_STYLE <<< "${TYPE_CONFIG[$PROJECT_TYPE]}"

# 设置默认值
if [[ -z "$PROJECT_STYLE" ]]; then
    PROJECT_STYLE="$DEFAULT_STYLE"
fi

if [[ -z "$PROJECT_RESOLUTION" ]]; then
    PROJECT_RESOLUTION="$DEFAULT_RES"
fi

# 验证风格
if [[ "$PROJECT_TYPE" == "drama" ]]; then
    VALID_STYLES="anime japanese_anime american_comic chinese_anime"
else
    VALID_STYLES="movie documentary live_action"
fi

if [[ ! " $VALID_STYLES " =~ " $PROJECT_STYLE " ]]; then
    echo "⚠️  风格 '$PROJECT_STYLE' 可能不适用于类型 '$PROJECT_TYPE'" >&2
    echo "   推荐风格: $VALID_STYLES" >&2
fi

# 创建项目目录
PROJECT_DIR="$PROJECTS_ROOT/$PROJECT_NAME"

if [[ -d "$PROJECT_DIR" ]]; then
    echo "❌ 错误: 项目 '$PROJECT_NAME' 已存在" >&2
    echo "   路径: $PROJECT_DIR" >&2
    exit 1
fi

echo "🎬 创建新项目..."

# 创建目录结构
mkdir -p "$PROJECT_DIR"/{frames,videos,final,assets/{audio,images,fonts}}

# 生成项目配置文件
PROJECT_JSON="$PROJECT_DIR/project.json"
TIMESTAMP=$(date -Iseconds)

cat > "$PROJECT_JSON" << EOF
{
  "name": "$PROJECT_NAME",
  "type": "$PROJECT_TYPE",
  "type_label": "$TYPE_LABEL",
  "style": "$PROJECT_STYLE",
  "style_label": "${STYLE_NAMES[$PROJECT_STYLE]:-$PROJECT_STYLE}",
  "created_at": "$TIMESTAMP",
  "updated_at": "$TIMESTAMP",
  "resolution": "$PROJECT_RESOLUTION",
  "ratio": "$DEFAULT_RATIO",
  "duration": $PROJECT_DURATION,
  "status": "created",
  "stats": {
    "frames_total": 0,
    "frames_generated": 0,
    "videos_generated": 0
  }
}
EOF

# 创建剧本模板
SCRIPT_MD="$PROJECT_DIR/script.md"
cat > "$SCRIPT_MD" << EOF
# $PROJECT_NAME

> 类型: $TYPE_LABEL | 风格: ${STYLE_NAMES[$PROJECT_STYLE]:-$PROJECT_STYLE}
> 创建时间: $(date '+%Y-%m-%d %H:%M')

## 故事梗概

[待填写]

## 角色设定

### 主角
- 姓名：
- 外观：
- 性格：

### 配角
- 姓名：
- 外观：
- 性格：

## 分镜脚本

### 第一幕：钩子（0-10秒）

#### 分镜 1
- **画面**：
- **对白/旁白**：
- **时长**：3秒
- **运镜**：

---

EOF

# 创建视频提示词文件
VIDEO_PROMPTS="$PROJECT_DIR/video_prompts.md"
cat > "$VIDEO_PROMPTS" << EOF
# 视频提示词 - $PROJECT_NAME

## 风格基础提示词

**通用风格**: ${STYLE_NAMES[$PROJECT_STYLE]:-$PROJECT_STYLE}风格，高质量，专业制作

## 分镜提示词

### 分镜 1
\`\`\`
画面描述：[待填写]
运镜描述：[待填写]
\`\`\`

EOF

# 创建字幕文件模板
SUBTITLES_VTT="$PROJECT_DIR/subtitles.vtt"
cat > "$SUBTITLES_VTT" << EOF
WEBVTT

00:00:00.000 --> 00:00:03.000
[字幕示例 - 请替换为实际内容]

EOF

# 创建 README
README="$PROJECT_DIR/README.md"
cat > "$README" << EOF
# $PROJECT_NAME

${TYPE_LABEL}项目 - ${STYLE_NAMES[$PROJECT_STYLE]:-$PROJECT_STYLE}风格

## 项目信息

- **类型**: $TYPE_LABEL
- **风格**: ${STYLE_NAMES[$PROJECT_STYLE]:-$PROJECT_STYLE}
- **分辨率**: $PROJECT_RESOLUTION
- **画幅**: $DEFAULT_RATIO
- **预计时长**: ${PROJECT_DURATION}秒

## 目录结构

\`\`\`
$PROJECT_NAME/
├── project.json      # 项目配置
├── script.md         # 剧本
├── video_prompts.md  # 视频提示词
├── subtitles.vtt     # 字幕
├── frames/           # 分镜图片
├── videos/           # 视频片段
├── final/            # 最终成片
└── assets/           # 素材资源
\`\`\`

## 制作进度

- [ ] 完成剧本
- [ ] 生成分镜图片
- [ ] 生成视频片段
- [ ] 合成最终视频

EOF

# 输出结果
echo ""
echo "✅ 项目创建成功！"
echo ""
echo "📁 项目目录: $PROJECT_DIR"
echo "📋 项目配置:"
echo "   - 名称: $PROJECT_NAME"
echo "   - 类型: $TYPE_LABEL"
echo "   - 风格: ${STYLE_NAMES[$PROJECT_STYLE]:-$PROJECT_STYLE}"
echo "   - 分辨率: $PROJECT_RESOLUTION"
echo "   - 画幅: $DEFAULT_RATIO"
echo "   - 预计时长: ${PROJECT_DURATION}秒"
echo ""
echo "📝 已创建文件:"
echo "   - project.json    (项目配置)"
echo "   - script.md       (剧本模板)"
echo "   - video_prompts.md (提示词模板)"
echo "   - subtitles.vtt   (字幕模板)"
echo "   - README.md       (项目说明)"
echo ""
echo "🚀 接下来可以:"
echo "   1. 编辑剧本: $PROJECT_DIR/script.md"
echo "   2. 添加素材: $PROJECT_DIR/assets/"
echo "   3. 开始创作分镜"

# 返回项目目录路径
echo ""
echo "$PROJECT_DIR"
