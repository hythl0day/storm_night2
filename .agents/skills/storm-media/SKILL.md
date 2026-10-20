---
name: storm-media
description: 《暴风夜》节目素材生成管线（Python 薄客户端 + 节目专用工作流）。用 Seedream 生成角色/场景/道具 canon 图与分镜首帧，用 Seedance 2.5 生成视频。触发词：生图、人设图、场景图、分镜、首帧、生成视频、出片、canon、storm-media。
---

# storm-media —— 《暴风夜》媒体素材管线

计划与 API 规格全文见 `docs/media-skill-plan.md`；本文件是工作流纪律（agent 侧）。

## 脚本（本 skill 根目录 scripts/ 下，Python + httpx，无任何安装步骤）

```bash
# 图片（Seedream）：文生图 / 参考图生图 / Lite 连贯组图
python scripts/gen_image.py --prompt "..." [--ref 图1 --ref 图2] [--model lite|pro] \
    [--size 2K] [--group N] [--out 输出路径] [--watermark]

# 视频（Seedance 2.5）：首帧/首尾帧/参考素材
python scripts/gen_video.py --prompt "...台词用{}括起..." [--first-frame 图] [--last-frame 图] \
    [--ref 素材 ...] [--duration 4-30|-1] [--ratio adaptive] [--resolution 720p] [--no-audio] [--out-dir 目录]
```

- Key：环境变量 `RELAY_API_KEY_CN`（`ARK_API_KEY` 兜底），无需其他配置。
- 产物 URL 仅 24 小时有效，脚本生成后立即下载落盘，并把原始 URL 登记到 `media/registry.json`（字段：ts/kind/file/url/model/task_id/prompt/refs）。**喂给 Seedance 的参考素材优先用 registry 里的原始 URL**（信任链免审核）；URL 过期（>24h）再用本地文件。

## 铁律

1. **人脸信任链**：含人脸素材必须是本账号 Seedream 文生图自产（30 天有效期，超期重新生成或图生图刷新）。禁止外部导入人脸参考图——会触发 Seedance 输入审核拦截。
2. **台词铁律**：成片台词取材自 AI 真实生成——`game/<id>/` 存档（speech/inner 通道）或 scene/interview 补拍归档；允许剪辑修剪与措辞润色（AI 演技生涩，成句可重写），但不得歪曲原本的意图、立场与推理逻辑，不得添加它没表达过的信息，游戏事实以存档裁决为准。台词在视频 prompt 中用 `{台词原文}` 标记；音乐 `()`、音效 `<>`、字幕 `【】`。
3. **成本闸**：批量视频生成前必须向用户报价确认；单张图片可直接生成。

## 工作流（canon 优先，禁止跳步）

角色图 → 场景图 → 道具图 → 分镜首帧 → 视频。

1. **角色图**：Seedream Pro 文生图，白底单张含面部特写 + 正/侧/背全身（参考 `media/characters/claude-sheet.png`）；人设对齐 `personas.toml`（性别/体格/国籍/性格底色）。
2. **场景图**：会客室（壁炉/长桌/规则书）/书房（保险箱/雕像）/监控室/地下室/卧室/走廊/采访黑屋；风格与角色图一致。
3. **道具图**：12 种道具特写（剧情证物）。
4. **分镜首帧**：Seedream 多参考生图（角色 canon + 场景 canon），prompt 内用「图1/图2」指代、末尾注释、传参顺序一致；单镜头 1–5 个主体最稳，多人群戏给角色拆单视图参考。
5. **视频**：首帧/首尾帧任务（ratio 自动锁 adaptive）；时长 4–30s 或 -1 自选；音频默认开。

## 目录约定

```
media/
  characters/       # 角色 canon 图（白底四视图，跨集复用；人脸图 30 天有效期，注意刷新）
  scenes/           # 场景 canon 图
  props/            # 道具 canon 图
  style.md          # 风格基线（待定：真人 vs 动漫试点对比后定稿）
  registry.json     # 生成台账（原始 URL 24h 有效，拿到即下载落盘）
  <episode>/        # 每集产出
    frames/         # 分镜首帧
    videos/         # 视频片段
```

## 提示词要点（Seedance 2.5）

- 基础公式：主体 + 动作/事件 + 场景环境 + 视觉风格 + 运镜 + 声音；时间戳（0-3s…）或「镜头 N」切分。
- 素材指代：`@图片1`（按 content 数组内同类素材顺序编号），写明每个素材参考什么、不参考什么。
- 负向控制：「不要字幕」「无 BGM，只生成环境音」等可用。
- 编辑/延长任务：ratio 强制 adaptive，编辑 duration 强制 -1，建议 mov 输出。

## 待实测（冒烟清单）

- [ ] Seedance 台词直出（`{}` + generate_audio）逐字稳定性 → 通过则砍掉 TTS 方案
- [ ] 人脸信任链端到端（Seedream 人设图 → Seedance 首帧/参考）
- [ ] draft 样片模式（480p 草稿 → 1080p 成片）在 AgentPlan 网关是否可用
- [ ] Seedream flash 别名（更便宜）在 AgentPlan 是否可用
