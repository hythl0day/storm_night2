#!/bin/bash
# 批量视频生成脚本
# 用法: ./batch_generate.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FRAMES_DIR="$PROJECT_DIR/seedream_frames"
OUTPUT_DIR="$PROJECT_DIR/videos"

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# 运动描述提示词
declare -A PROMPTS=(
  ["frame_01"]="camera slowly zooms out from energy tower, dramatic reveal of the glowing beam piercing through dark clouds, cinematic"
  ["frame_02"]="panoramic view of 2080 dystopian cityscape, camera slowly pans right across neon-lit buildings, establishing shot"
  ["frame_03"]="close-up of broken holographic billboard, glitching effects, sparks flying, dramatic lighting"
  ["frame_04"]="camera follows the figure walking towards the distant tower, back view, mysterious atmosphere, slow movement"
  ["frame_05"]="close-up of mechanical arm, zoom in on the word HOPE engraved on it, subtle glow, emotional moment"
  ["frame_06"]="flashback scene, warm lighting, camera slowly zooms in on father and child, nostalgic atmosphere, soft focus"
  ["frame_07"]="wide shot of figure walking towards energy tower, camera slowly follows from behind, silhouette in fog"
  ["frame_08"]="medium shot of figure sneaking through underground tunnel, blue emergency lights flickering, tense atmosphere"
  ["frame_09"]="close-up of rusty electronic door, camera zooms in on CORE CONTROL ROOM TOP SECRET text, dramatic"
  ["frame_10"]="action shot of mechanical arm hacking the door lock, sparks flying, dynamic camera movement"
  ["frame_11"]="medium shot inside control room, screens lighting up one by one, dramatic reveal, blue glow"
  ["frame_12"]="close-up of screen showing ENERGY SUFFICIENT message, camera zooms in slowly, hopeful moment"
  ["frame_13"]="close-up of screen showing ENERGY BLOCKED warning, red alert flashing, tension builds, dramatic"
  ["frame_14"]="medium shot of shocked expression on face, dramatic lighting change, camera pushes in, emotional"
  ["frame_15"]="medium shot of figure turning around alertly, tense atmosphere, dramatic lighting, slow motion"
  ["frame_16"]="silhouette of mysterious figure appearing in doorway, dramatic backlighting, ominous atmosphere"
  ["frame_17"]="mysterious figure entering control room, camera tracks the movement, tense atmosphere, dramatic lighting"
  ["frame_18"]="two-shot of confrontation scene, camera alternates between characters, dramatic tension, standoff"
  ["frame_19"]="action shot of mysterious figure attacking, fast camera movement, dynamic action, intense"
  ["frame_20"]="medium shot of figure dodging attack, slow motion effect, dramatic lighting, heroic moment"
  ["frame_21"]="medium shot of figure counterattacking with mechanical arm, dynamic action sequence, powerful"
  ["frame_22"]="mysterious figure staggered by counterattack, dramatic moment, camera shake effect, intense"
  ["frame_23"]="close-up of mysterious figure removing mask, slow reveal, dramatic lighting, shocking moment"
  ["frame_24"]="close-up of surprised expression, recognition in eyes, emotional moment, dramatic reveal"
  ["frame_25"]="close-up of father figure, warm lighting, emotional reunion scene, tears of joy, touching"
  ["frame_26"]="medium shot of two figures at control panel, working together, hopeful atmosphere, teamwork"
  ["frame_27"]="wide shot of energy tower reactivating, massive light beam shooting into sky, triumphant moment, epic"
  ["frame_28"]="aerial view of city lights coming back on one by one, hopeful atmosphere, camera rises, inspiring"
  ["frame_29"]="medium shot of figure standing before tower, looking at bright future, cinematic ending, hopeful"
)

echo "=== 批量视频生成 ==="
echo "分镜目录: $FRAMES_DIR"
echo "输出目录: $OUTPUT_DIR"
echo ""

# 统计
TOTAL=0
SUCCESS=0
FAILED=0

# 遍历所有分镜
for i in $(seq -w 1 29); do
    FRAME_FILE="$FRAMES_DIR/frame_$i.png"

    if [ ! -f "$FRAME_FILE" ]; then
        echo "跳过 frame_$i (文件不存在)"
        continue
    fi

    TOTAL=$((TOTAL + 1))
    FRAME_NAME="frame_$i"
    OUTPUT_FILE="$OUTPUT_DIR/video_$i.mp4"
    PROMPT="${PROMPTS[$FRAME_NAME]}"

    if [ -z "$PROMPT" ]; then
        PROMPT="dramatic camera movement, cinematic style"
    fi

    echo "[$TOTAL/29] 处理 $FRAME_NAME..."

    # 调用生成脚本
    if "$SCRIPT_DIR/generate_video.sh" "$FRAME_FILE" "$PROMPT" "$OUTPUT_FILE" > /dev/null 2>&1; then
        SUCCESS=$((SUCCESS + 1))
        echo "  ✓ 成功"
    else
        FAILED=$((FAILED + 1))
        echo "  ✗ 失败"
    fi

    echo ""
done

echo "=== 批量生成完成 ==="
echo "总计: $TOTAL"
echo "成功: $SUCCESS"
echo "失败: $FAILED"
