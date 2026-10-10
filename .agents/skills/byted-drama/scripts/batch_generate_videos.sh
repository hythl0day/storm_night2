#!/bin/bash
# 批量视频生成脚本
# 用法: ./batch_generate_videos.sh [起始镜号] [结束镜号]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FRAMES_DIR="$PROJECT_DIR/seedream_frames"
OUTPUT_DIR="$PROJECT_DIR/videos"
LOG_FILE="$PROJECT_DIR/video_generation.log"

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# 范围参数
START=${1:-1}
END=${2:-29}

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "========== 批量视频生成开始 =========="
log "范围: 分镜 $START 到 $END"
log "图片目录: $FRAMES_DIR"
log "输出目录: $OUTPUT_DIR"

# 统计
TOTAL=$((END - START + 1))
SUCCESS=0
FAILED=0

# 提示词函数
get_prompt() {
    local num=$1
    case $num in
        1) echo '特写镜头：能源塔中心爆发刺眼白光，光柱向上穿透云层，画面从中心过曝逐渐扩散，光芒四射，动态光效强烈，赛博朋克风格' ;;
        2) echo '全景镜头：2080年赛博朋克城市，镜头缓缓推进，霓虹灯残破闪烁，能源塔在远处黯淡无光，冷灰色调中带有粉色和青色霓虹残影，破败绝望的氛围' ;;
        3) echo '特写镜头：破碎的全息广告牌显示能源警告剩余百分之七，电流滋滋作响，全息图像时隐时现，故障闪烁效果' ;;
        4) echo '中景镜头：阿光站在废墟中仰望天空，背影，机械右臂微微反光，镜头缓慢上移，破旧深灰色夹克随风飘动，坚定而孤独' ;;
        5) echo '特写镜头：机械右臂上的希望二字清晰可见，银色金属质感，手指缓慢握拳，金属关节转动，光影在刻字上流动' ;;
        6) echo '闪回镜头：童年阿光与父亲在能源塔前合影，画面泛黄柔光，然后快速叠化到父亲临终时刻为机械臂刻字，回忆温情氛围' ;;
        7) echo '远景镜头：阿光走向远处的能源塔，背影逐渐消失在烟雾中，镜头缓慢拉远，神秘压抑的氛围' ;;
        8) echo '中景镜头：阿光潜入地下通道，蓝色应急灯闪烁照明，镜头跟随前进，墙壁上管线和蒸汽管道，神秘科技感氛围' ;;
        9) echo '特写镜头：生锈的电子门，门牌显示核心控制室绝密，金属锈迹和磨损，蓝色应急光照射，镜头缓慢推进' ;;
        10) echo '动作镜头：机械臂快速操作电子锁，火花四溅，金属摩擦，镜头跟随动作晃动，紧张刺激的氛围' ;;
        11) echo '全景镜头：控制室大门打开，室内屏幕逐一亮起，蓝色光芒照亮空间，镜头缓缓推进，科技感扑面而来' ;;
        12) echo '特写镜头：屏幕显示能源储量数据，绿色条形图显示充足状态，屏幕发光照亮阿光的脸，震惊的表情' ;;
        13) echo '特写镜头：屏幕显示能源被封锁启动者数据已删除，红色警告文字闪烁，神秘诡异的氛围' ;;
        14) echo '中景镜头：阿光震惊地看着屏幕，瞳孔反射蓝光，表情从疑惑转为愤怒，情绪转变明显' ;;
        15) echo '特写镜头：阿光警觉回头，眼神锐利，镜头快速转动，紧张悬疑的氛围，暗示有人接近' ;;
        16) echo '远景镜头：走廊尽头黑色身影出现，红光在身影背后闪烁，神秘人缓缓走近，威胁感十足' ;;
        17) echo '中景镜头：神秘人走进控制室，全身银色战甲，面罩遮面，身材高大，冷峻威严的姿态' ;;
        18) echo '对峙镜头：阿光转身面向神秘人，机械臂蓝光亮起，两人对峙，紧张气氛升级' ;;
        19) echo '动作镜头：神秘人发起攻击，能量冲击波从手中发射，蓝白色光芒，镜头跟随冲击波运动' ;;
        20) echo '特写镜头：阿光闪避能量冲击波，子弹时间慢动作，能量波擦过脸颊，发丝飘动，紧张刺激' ;;
        21) echo '动作镜头：阿光反击，机械臂重击地面，碎石飞溅，力量感十足，镜头震动效果' ;;
        22) echo '动作镜头：阿光被击退，身体撞向控制台，火花四溅，痛苦但坚定的表情' ;;
        23) echo '特写镜头：阿光的手缓缓伸向解除封锁按钮，绿色光芒从按钮透出，希望与危险并存' ;;
        24) echo '动作镜头：神秘人扑向阿光，银色战甲闪光，动作迅猛，试图阻止阿光按下按钮' ;;
        25) echo '特写镜头：阿光按下按钮的瞬间，绿色光芒从控制台爆发，光芒向四周扩散，胜利与希望' ;;
        26) echo '全景镜头：能源塔爆发白光，光柱穿透云层，镜头从地面仰拍，光芒照亮整座城市，壮观的视觉效果' ;;
        27) echo '远景镜头：城市霓虹灯逐一亮起，人们仰望天空，暖色调光芒洒下，希望重生的氛围' ;;
        28) echo '中景镜头：阿光站在窗前望着黎明，金色晨光照亮脸庞，释然的微笑，机械臂在光中闪光' ;;
        29) echo '黑屏转场：标题最后的光淡入，字幕真正的威胁还在黑暗中等待，神秘悬念结尾' ;;
        *) echo '' ;;
    esac
}

for i in $(seq $START $END); do
    FRAME_FILE="$FRAMES_DIR/frame_$(printf '%02d' $i).png"
    OUTPUT_FILE="$OUTPUT_DIR/video_frame_$(printf '%02d' $i).mp4"
    PROMPT=$(get_prompt $i)
    
    log ""
    log "---------- 分镜 $i/$END ----------"
    
    # 检查图片是否存在
    if [ ! -f "$FRAME_FILE" ]; then
        log "错误: 图片不存在 $FRAME_FILE"
        FAILED=$((FAILED + 1))
        continue
    fi
    
    # 检查视频是否已存在
    if [ -f "$OUTPUT_FILE" ]; then
        log "跳过: 视频已存在 $OUTPUT_FILE"
        SUCCESS=$((SUCCESS + 1))
        continue
    fi
    
    log "提示词: $PROMPT"
    log "开始生成..."
    
    # 调用生成脚本
    if "$SCRIPT_DIR/generate_video.sh" \
        -f "$FRAME_FILE" \
        -p "$PROMPT" \
        -o "$OUTPUT_FILE" \
        -r 480p \
        -g "9:16" \
        -d false \
        -a false \
        -w false \
        -t auto >> "$LOG_FILE" 2>&1; then
        log "成功: $OUTPUT_FILE"
        SUCCESS=$((SUCCESS + 1))
    else
        log "失败: 分镜 $i"
        FAILED=$((FAILED + 1))
    fi
done

log ""
log "========== 批量生成完成 =========="
log "总计: $TOTAL | 成功: $SUCCESS | 失败: $FAILED"
log "日志文件: $LOG_FILE"

# 显示生成的视频列表
log ""
log "生成的视频文件:"
ls -lh "$OUTPUT_DIR"/*.mp4 2>/dev/null | awk '{print $NF ": " $5}' | while read line; do
    log "  $line"
done
