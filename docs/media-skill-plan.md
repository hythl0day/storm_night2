# storm-media skill 计划

> 状态：**已实施**（2026-10）。交付于 `.agents/skills/storm-media/`（SKILL.md + scripts/ark.py + gen_image.py + gen_video.py）；
> 图片链路已冒烟通过（media/canon/characters/claude-sheet-v1.png，Seedream 5.0 pro 2K 四视图）；
> 视频链路待冒烟（台词直出 / 人脸信任链 / draft 模式，见 SKILL.md 冒烟清单）。
> 前置条件：Key 复用 `RELAY_API_KEY_CN`（CN 四席的"中转站"实为火山方舟 AgentPlan 直连，
> base_url 即 `https://ark.cn-beijing.volces.com/api/plan/v3`，ark- 前缀，与图片/视频接口同体系）；
> media/ 已清空，作为素材归档根目录。
> 关键约束：含人脸素材必须走"Seedream 文生图自产 → Seedance 参考"的信任链（见补充 §1），人设图不可外部导入。

## 背景与目标

- 官方 AgentPlan skill（seedream/seedance）是 Node.js 通用件；本项目全 Python 技术栈，且需求高度特化（固定 8 角色卡司、固定场景清单、台词逐字来自对局存档）。自建项目级 skill，把官方 skill 的 API 规格与 byted-drama 的流程纪律消化进来。
- 已备条件：`ARK_API_KEY`（ark- 前缀）已入环境变量；media/ 已清空，作为素材归档根目录。
- API 规格已从官方 skill 源码完整提取（见下"API 规格摘要"）；实施中若遇参数报错，用 FetchURL 拉取火山官方文档补全。

## 交付物

新建 `.agents/skills/storm-media/`：

```
storm-media/
  SKILL.md            # 工作流纪律（agent 侧）
  scripts/
    ark.py            # AgentPlan HTTP 客户端（httpx）：key/请求/轮询/下载
    gen_image.py      # Seedream CLI：文生图/参考图生图/组图
    gen_video.py      # Seedance CLI：首帧/首尾帧/参考图视频，异步轮询
```

### 1. `scripts/ark.py`（共享客户端）

- Key：读 `RELAY_API_KEY_CN` 环境变量（项目既有约定，与 CN 四席同一把 AgentPlan key），`ARK_API_KEY` 作兜底；都未设置时报错提示。
- `POST https://ark.cn-beijing.volces.com/api/plan/v3/images/generations`（同步，httpx 超时 120s）
- `POST .../contents/generations/tasks` 创建视频任务；`GET .../tasks/{id}` 轮询（间隔 5s，总上限 20 分钟，终态 succeeded/failed/expired/cancelled）
- 图片/视频 URL 拿到即下载落盘（官方 URL 视为临时）；下载前校验公网地址。
- 不重试生成请求（避免重复扣费）；网络层错误最多重试 2 次。
- 入口头部 reconfigure stdout 为 UTF-8（项目惯例）。

### 2. `scripts/gen_image.py`（Seedream 薄封装）

- 参数：`--prompt`（必填）、`--ref <路径/URL>`（可重复，映射请求体 `image` 字段；本地文件转 base64 data URI）、`--model lite|pro`（默认 lite；pro 用于交互编辑/高精度单图）、`--size`（默认 2K）、`--out <输出路径>`（默认 media/ 下按时间戳）、`--no-watermark`、`--group N`（Lite 连贯组图）。
- 组图只发 `sequential_image_generation: "auto"`，张数由 prompt 语义引导，**严禁** num_images/count 字段。
- 参考图上限：Lite ≤14 / Pro ≤10。

### 3. `scripts/gen_video.py`（Seedance 2.5 薄封装）

- 只做 2.5（`doubao-seedance-2.5`）；参数：`--prompt`、`--first-frame <路径>`、`--last-frame <路径>`、`--ref`（可重复）、`--duration 4-30`、
  `--ratio`（默认 adaptive；首尾帧任务强制 adaptive）、`--resolution 480p|720p|1080p`（默认 720p）、`--audio/--no-audio`（generate_audio）、`--out-dir`。
- content 数组组装：text + image_url(role=first_frame/last_frame/reference_image)。
- 创建任务后轮询至终态，成功下载 mp4；失败打印 error.code/message。
- 视频素材不走 base64（2.5 限制），图片可 base64。

### 4. `SKILL.md`（工作流纪律，消化 byted-drama 方法论）

- **canon 优先**：角色图（单张含面部特写+正/侧/背全身，白底）→ 场景图 → 道具图 → 分镜首帧 → 视频；禁止跳过基础素材直接出分镜。
- **目录约定**：`media/canon/characters|scenes|props/`（视觉 canon，跨集复用）；每集产出 `media/<episode>/frames|videos/`；canon 图作为参考图传入后续所有生成。
- **参考图引用规则**：prompt 内用"图1/图2"命名、末尾注释说明、传参顺序一致（byted-drama 纪律）。
- **成本闸**：视频生成批量操作前必须先报价确认；单张图片可直接生成。
- **台词铁律**：成片台词逐字来自 `game/<id>/` 存档；TTS 暂缓，先实测 Seedance `--audio` 直出台词能力。
- 风格基线待定（真人 vs 动漫/漫画试点对比），SKILL.md 留 `media/canon/style.md` 槽位。

## 实施步骤

1. 写 `ark.py` + `gen_image.py` + `gen_video.py`。
2. 冒烟测试（图片，成本约零）：Seedream 文生图一张克劳德人设草图（真人风格，白底四视图），验证 key、链路、落盘。
3. 写 `SKILL.md`。
4. （需用户确认成本后）用冒烟图作首帧生成一段 5s 720p 视频，一次验证三件事：gen_video 全链路、**人脸信任链**（Seedream 自产人脸图作参考是否免审核）、**台词直出**（prompt 内 `{台词}` + generate_audio 的对嘴效果）；顺带实测 draft 样片模式在 AgentPlan 网关是否可用。

## 验证

- `python .agents/skills/storm-media/scripts/gen_image.py --prompt "..." --out media/canon/characters/test.png` 成功落盘；
- 视频步骤 4 输出 mp4 落盘且可播放；
- 全程不碰 src/ 与 game/（游戏管线零改动）。

## API 规格摘要（已从官方 skill 源码提取，实施依据）

**端点（用户确认，AgentPlan /api/plan/v3 体系）**：
- 图片生成：`POST /api/plan/v3/images/generations`
- 创建视频任务：`POST /api/plan/v3/contents/generations/tasks`
- 查询视频任务：`GET /api/plan/v3/contents/generations/tasks/{id}`
- 查询任务列表：`GET /api/plan/v3/contents/generations/tasks?page_num=&page_size=&filter.status=&filter.task_ids=&filter.model=`
- 取消/删除任务：`DELETE /api/plan/v3/contents/generations/tasks/{id}`

- 图片 body `{model,prompt,size,response_format:"url",image:[refs],watermark,...}`；响应 `data[i].url`。
  - model：`doubao-seedream-5.0-lite`（组图/多参考 ≤14）/ `doubao-seedream-5.0-pro`（精准编辑/拆图层/参考 ≤10）；size：Lite `2K/3K/4K`，Pro `1K/1.5K/2K` 或 `"宽x高"`（总像素 921600–4624220）。
  - 参考图字段名为 `image`（单张字符串/多张数组），支持 http(s) URL 或 `data:image/<type>;base64,...`（格式名小写）；每张 ≤30MB。
- 视频：`POST /api/plan/v3/contents/generations/tasks`；body `{model:"doubao-seedance-2.5",content:[{type:"text",...},{type:"image_url",image_url:{url},role}],resolution,ratio,duration,generate_audio,...}`；响应 `id`；`GET tasks/{id}` 轮询（5s 间隔，上限 20 分钟），终态 succeeded/failed/expired/cancelled，成功取 `content.video_url`（尾帧 `content.last_frame_url`）。
  - 2.5：duration 4–30 或 -1；resolution 480p/720p/1080p；首尾帧任务 ratio 必须 adaptive；视频参考只能公网 URL（不支持 base64）；图片/音频可 base64；素材合计 ≤50（图 30/视频 10/音频 10）、请求体 ≤64MB。

## 官方文档补充（2026-10 阅读，五条来源见下）

来源：[Seedance 2.5 教程](https://console.volcengine.com/ark/region:cn-beijing/docs/seedance-2-5) / [提示词指南](https://console.volcengine.com/ark/region:cn-beijing/docs/seedance-2-5-prompt-guide) / [人脸审核与肖像素材](https://console.volcengine.com/ark/region:cn-beijing/docs/seedance-portrait-asset-guide) / [Seedream 5.0 pro](https://console.volcengine.com/ark/region:cn-beijing/docs/seedream-5-0-pro) / [Seedream 5.0 lite 教程](https://console.volcengine.com/ark/region:cn-beijing/docs/seedream-4-0-5-0)。文档索引：`https://ark.volcengine.com/llms.txt`（各页有 .md 版本可直取）。

### 1. 人脸审核（决定整条管线的硬约束）

Seedance 2.5/2.0 **不支持直接上传含真人人脸的参考图/视频**，三条合规通路：
- **信任模型产物**（我们走这条）：本账号近 30 天内由 **Seedance 2.5/2.0 生成的含人脸视频及其尾帧**、**Seedream 5.0 lite/pro/flash 文生图生成的含人脸图片**，可作为输入素材二次创作不触发审核。→ 人设图必须 Seedream 文生图自产，再喂给 Seedance，链路闭环。**注意 30 天有效期**：canon 人设图超期需重新生成或经 Seedream 图生图刷新。
- 预置虚拟人像库（`asset://<asset ID>` 传入）；已授权真人素材（真人认证入库）——均不适用于我们（角色是虚构 AI 形象）。

### 2. 台词直出（实测后可砍掉 TTS）

`generate_audio: true` 时 Seedance 原生生成人声配音，**台词写在 prompt 里用 `{}` 括起**，模型按词对口型（官方说唱示例演示了多语言逐字对嘴）。声音标注法：音乐 `()`、音效 `<>`、台词 `{}`、字幕 `【】`；非中文台词需注明语言。→ 我们的台词铁律（逐字来自存档）的落地方式 = 分镜 prompt 中 `{存档原文}`。待实测：长台词/多角色对白的稳定性。

### 3. 提示词规则（写进 SKILL.md 的分镜规范）

- 基础公式：主体 + 动作/事件 + 场景环境 + 视觉风格 + 运镜/切镜 + 声音；可按时间戳（0-3s…）或「镜头 N」切分；负向控制支持字幕/BGM/对白（"不要字幕"）。
- 素材指代：prompt 里用 `@图片1`/`@视频1`/`@音频1`（按 content 数组内同类素材的顺序编号），逐一写明每个素材参考什么、不参考什么；不要在图片上写名字做映射。
- 主体数量：单镜头 1–5 个主体最稳；超过 5 主体用单视图参考图而非多视图合一图。→ byted-drama 的"四合一角色图"作 canon 存档可以，但多人群戏镜头可能需要给每个角色拆单视图。
- 宫格分镜参考：≤15 分镜，推荐线稿/火柴人分镜，分镜图上少写字。

### 4. 时效与限流

- **产物 URL 只保存 24 小时**（视频下载上限 100 次），任务记录 7 天 → 拿到立即下载落盘（计划已含）。
- 限流（个人用户）：视频 RPM 180、最大并发 3；图片按 IPM。批量生产时并发 ≤3。

### 5. 待实测的文档/AgentPlan 差异

- ~~官方文档用 `/api/v3` 端点 + 日期版模型 ID；AgentPlan 走 `/api/plan/v3` + 别名 ID~~ → **已由用户确认端点清单（见"API 规格摘要"），走 `/api/plan/v3`**；模型 ID 用别名（`doubao-seedance-2.5`、`doubao-seedream-5.0-lite/pro`），冒烟验证。
- 文档另载更便宜更快的 `doubao-seedream-5-0-flash`（日期版 ID `doubao-seedream-5-0-flash-260915`，`/api/v3` 体系）；AgentPlan 侧是否可用别名待冒烟时顺验，可用则大批量抽卡走 flash 省钱。
- **draft 样片模式**（先 480p 草稿确认、再基于草稿任务 ID 出 1080p 成片，参数复用规则严格）：官方文档支持，AgentPlan skill 声称不支持 → 冒烟时实测，若可用则是**视频抽卡的省钱神器**，写进 SKILL.md 成本闸。
- 视频编辑/延长任务：ratio 强制 adaptive、编辑 duration 强制 -1、建议 mov 输出（H.264+yuv444p+PCM，后期调色友好）。
- 文档另有官方"提示词优化 skill"（sd25-pe，npx 安装）可选参考，不依赖。
