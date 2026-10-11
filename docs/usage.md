# 脚本使用说明（src/ 跑局工具）

本文档说明 `src/` 目录下 Python 脚本的使用方法。当前已完成 M0（配置 + 多协议接入层 + 连通性测试），后续里程碑（开局生成、引擎、编排器、CLI 等）落地后会在本文档追加对应用法，总体计划见 `docs/dev-plan.md`。

## 1. 环境准备

- Python 3.11+（依赖 `tomllib` 标准库，低于 3.11 无法运行）。
- 安装依赖（目前只有 httpx）：

```bash
pip install -r src/requirements.txt
```

## 2. 配置玩家席位

`players.toml` 随仓库跟踪分发（只含接口地址与环境变量名，无任何密钥），换机器克隆即用；按需修改各席位的 `model` / `base_url` / `api_key_env`。

**安全约定：api_key 不落盘。** 配置里只填环境变量名（`api_key_env`），真实 key 在运行前注入环境变量。Git Bash 当前会话：

```bash
export RELAY_API_KEY_CN="sk-你的key"
```

长期使用可在 Windows「系统属性 → 环境变量」中设置，新开的终端即可读到。

每个席位需要确认/修改的字段：`model`（**以你的中转站/官方账户里的实际模型名为准**，模板里的型号是占位符）、`base_url`、`api_key_env`、`api_type`。

## 3. 运行连通性测试

```bash
python -m src.smoke                                     # 交互式：列出席位，手动选择（可追加真人席位）
python -m src.smoke --all                               # 全部席位，不询问
python -m src.smoke --random 6                          # 随机抽 6 席
python -m src.smoke --only deepseek,kimi               # 按 id 或名字指定席位
python -m src.smoke --human 老陈 --random 5              # 追加真人席位，可与以上组合
python -m src.smoke --prompt "你为什么被困在古堡里？"    # 自定义提问（--config 默认即 players.toml）
```

**席位选择**：不带 `--all/--random/--only` 时进入交互模式——先打印席位列表（含人设摘要），输入 `all` / `r N`（随机 N 席）/ 逗号分隔序号（如 `1,3,5`）；随后可输入一个名字追加真人席位（直接回车跳过）。真人席位不调用 API：脚本打印问题，由你在终端键入回答，同样计入汇总。

**人设卡**：每个角色的性别 / 体格 / 国籍 / 性格人设写在 `personas.toml`（键名须与 `players.toml` 的 `name` 一致，未命中的席位按"普通人"处理）。人设只写性格不写职业（失忆设定；职业每局临时分配），且 prompt 中已声明"人设不影响推理能力""性格靠言行流露、禁止自我介绍"。性别/体格/国籍是游戏机制属性（空手谋杀判定、犯罪侧写会用到）——规则书不含固定角色表，每局开局以此文件为准生成角色表并全员公布；体格数值仅供主持人判定，注入 prompt 时会转成文字描述，角色不会得知数值。

行为：所有 AI 席位**并发**收到同一个问题，按各自人设回答，最后输出汇总：

```
【狄西】(3.2s, 87 tokens)
  ……
== 汇总：8/8 席连通 ==
```

逐席显示耗时与 token 用量；失败的席位显示原因（如 `环境变量 XXX 未设置`、HTTP 错误）。

### 3.1 单席位点名：ping.py（排障用）

smoke 是多席点名；ping **只测一个席位**，并输出完整诊断信息，用于定位连接问题：

```bash
python -m src.ping deepseek                 # 按席位 id 点名
python -m src.ping 狄西                     # 按席位名点名
python -m src.ping 杰米娜 --timeout 120     # 调整超时（默认 60s）
python -m src.ping 格罗克 --insecure        # 跳过 TLS 证书校验（仅调试用，会打印警告）
python -m src.ping kimi                                # 指定配置文件用 --config（默认即 players.toml）
```

输出内容：席位配置回显（api_type / base_url / model / key 是否设置及长度，**不显示 key 本身**）、实际请求的完整 URL、HTTP 状态码、响应内容或错误详情。三类典型结果：

- **连接层失败**：请求根本没拿到 HTTP 响应（DNS 污染 / 代理缺失 / 证书被拦截等网络问题）；
- **HTTP 4xx/5xx + 响应体**：请求到达但被拒绝——key 无效、模型名不存在、参数超限等，看响应体里的 message；
- **HTTP 200**：成功，显示回复与 token 用量；推理模型会同时打印推理摘要（`reasoning_content`）。

注意：ping 发送真实请求，会产生少量 token 计费。

### 3.2 开局生成：deal.py（M1）

从阵容池（`players.toml` + `personas.toml`）抽取席位，随机分配身份/职业/道具/首日行动顺序/保险箱，输出完整对局目录：

```bash
python -m src.deal                     # 随机种子，输出 game/<seed>/
python -m src.deal --seed 42           # 指定种子（同 seed 两次生成逐字节一致）
python -m src.deal --seed 42 --seats gpt,claude,gemini,deepseek,kimi,glm   # 手动指定 6 席（id 或名字）
python -m src.deal --seed 42 --out games/test   # 自定义输出目录
```

生成的 `game/<seed>/` 结构：

```
seed.txt               # 种子（复现与"对局未被操纵"凭证）
state.json             # 主状态（主持人侧，含身份等全部秘密）
rules.md               # 本局规则书快照
players.toml           # 阵容与参数快照
public/roster.md       # 全员公布角色表（体格为文字描述，不含数值）
public/announcement.md # 开局公告（主人字条）
private/<id>/card.md   # 每席私密卡：身份/同伙/职业技能/道具次数/开局准备义务
inbox/<id>/  outbox/<id>/   # 编排器阶段简报与回执（M4 起使用）
```

内心独白等私密素材（M4 起）按席位归档到 `private/<id>/`（如 `inner.md` 内心独白、私聊记录、个人调查结果），与私密卡并列。

分配约束（生成即校验，违反会报错退出）：**席位中美 3+3 对称**（随机模式自动满足，两侧 API 渠道均衡；手动 `--seats` 不对称时仅提示）；身份 4 平民/1 帮凶/1 杀手；职业 9 选 6 不重复；道具 14 选 6 不重复，杀手与帮凶各得一件互不相同的凶器；平民从剩余道具池抽取（可能拿到仅供自卫的武器或无权的反派道具）；保险箱固定 1 件凶器（开局全员公告"主人字条"，但不公布具体哪件）。

控制台输出是**主持人视角**（含身份分配），不要外发。

### 3.3 编排器主循环：orchestrator.py（M4/M5）

把引擎接上六家 API，按阶段驱动完整对局（直至产生胜者或主神结局）：简报 → 收集三通道 JSON（speech/inner/notes/actions）→ 引擎裁决 → 落盘。

```bash
python -m src.orchestrator --game game/012345            # 从当前阶段跑到对局结束
python -m src.orchestrator --game game/012345 --resume   # 从 state.json 断点续跑（席位行动粒度）
```

全天流程（rules-6 v3）：开局准备（仅第 1 天；反派建筑师确认密道、窃听器持有者放置，其余跳过）→ 案件调查（第 2 天起：清晨公告由引擎自动路由并附状态行——存活数/无杀倒计时/封存数；调查自荐现场调查/犯罪侧写；侦探仅在有案件的日子存在，有额外监控特权）→ 公共发言（侦探最后发言）→ 交换环节（先问赠送者，再单独问接收者）→ 投票（第 2 天起：逐席收票、反派心理医生可夹带记忆植入、遗言先于身份公布、封存道具逐件多数决（平票取侦探票，无侦探日平票即销毁）、无人出局时持武器平民可武装指认——每次投票至多一次）→ 自由活动（会客室/书房/监控室/地下室四选一，白天卧室清场、无效选择滞留会客室；同入邀请；≥2 人非黑暗房间一轮房内交谈；逐人房间行动菜单；阶段末毁证窗口）→ 晚间休息（留宿邀请与同宿密谈、夜间房间行动、密道、卧室收听窃听器、杀手夜袭与夜间谋杀窗口，选择互相不可见）→ 次日清晨。任一胜负条件满足即收束，结局写入 public/log.md。

行为约定：

- **消息结构**：system = `prompt/game_system.md` 渲染（人设 + 规则书快照 + 私密卡），user = 当次简报；对话不累积。简报 = 增量公告 + 增量私密事件 + 当前局面 + **关键记录**（主持人档案：公开事实流水剔除发言 + 该席私密事件历史，含投票/房间/谋杀回执等）+ notes 回放 + 合法动作菜单。全部顺序执行，不并发。
- **JSON 解析**：从回复提取 ```json 代码块；失败重试 ≤2 次；仍失败记"放弃行动"（能抢救出 speech 仍采用）。非法动作逐条驳回（私下告知本人），其余动作正常执行。
- **断点续跑（席位行动粒度）**：每个席位行动完成后即落盘 state.json（`orch.wip` 按"天：阶段：子环节"记录 done 名单与中间累计，如选票/选房结果）；`--resume` 重入当前环节并跳过已完成席位，被中断席位用相同简报重问（feed 游标与私密队列只在调用成功后提交）。
- **席位失败即中断**：某席 API 请求失败（providers 内部重试耗尽后仍失败）不再记"放弃行动"继续，而是落盘并中断整局（退出码 2）；排除故障后 `--resume` 续跑，被中断席位会重新行动。Ctrl+C 同样先落盘再退出（退出码 130）。

落盘产物（`game/<id>/`）：

```
script.md                # 主持人侧全记录：背景/阵容分配（全剧透）/全部发言与行动/裁决结果，不含内心独白
public/log.md            # 公开口径：公告、发言、交换、死亡公告（按阶段标题）
private/<id>/inner.md    # 内心独白（追加）；notes.md 私人笔记（每阶段覆盖）
private/<id>/log.md      # 该席听到的房间内交谈存档
inbox/<id>/day{N}-*.md   # 每环节简报存档；outbox/<id>/ 原始回复存档
host_log.jsonl           # 每次 API 调用 {ts, player, stage, ok, latency, total_tokens, error}
```

### 3.4 只读查看器：view.py（M4.5）

读 `game/<id>/` 存档渲染，与跑局完全解耦——复盘已结束的局、批量跑局时旁路查看都用它：

```bash
python -m src.view status --game game/012345        # 阶段/存活/身份台账/道具技能次数
python -m src.view log --game game/012345 -n 30     # 公开日志末尾 30 行
python -m src.view peek 杰米娜 --game game/012345   # 私密卡 + 私人笔记 + 内心独白
python -m src.view inner 杰米娜 --game game/012345  # 仅内心独白
python -m src.view host --game game/012345          # API 调用台账（耗时/成败）
python -m src.view follow --game game/012345 --interval 5   # 轮询刷新追局，Ctrl+C 退出
```

### 3.5 交互驾驶舱：console.py（M4.5）

进程内驱动跑局：底部"状态+输入"瞬时行（阶段/当前席位/spinner/耗时/字数）、思考内容灰色流式滚动、JSON 解析完成后渲染人读卡片（名字突出/内心灰色/行动黄色/分割线）、全部历史可滚轮回看、随时可输入命令。**每天开始时（含第 1 天开局）自动显示一次"天亮一览"：存活人员及其职业/技能剩余/道具**（宿主侧上帝视角，不进 prompt、不落盘）。

```bash
python -m src.console --game game/012345 [--resume]
```

命令（跑局进行中随时可敲，不打断进行中的调用）：`help` / `status` / `log [n]` / `peek <席位>` / `inner <席位>` / `host [n]` / `follow [席位]` / `pause` / `continue` / `save` / `quit`。

中断统一语义（`pause`、Ctrl+C、API 调用失败三者等价）：当前席位行动结束（API 失败则该席未完成）后立即落盘并暂停；输入 `continue`（别名 `resume`）在进程内续跑——被中断/待行动的席位自动重新执行，已完成席位不会重复。`quit` 退出进程（同样已落盘，可 `--resume` 恢复）；暂停或结束后仍可用 `status`/`log`/`peek` 等查看；暂停态/结束态下再按 Ctrl+C 直接退出。

中断安全性：state.json 为原子写（临时文件 + 替换），强杀不会留下半个文件；断点是**席位行动粒度**——每个席位行动完成后即落盘，任何时刻中断，续跑都从中断席位继续，已完成的席位不会重复行动（其发言/动作已落盘）。被中断的那次 API 调用结果丢弃，但上游可能已计费。无头模式（`python -m src.orchestrator`）下 API 失败则打印错误并以退出码 2 退出，`--resume` 续跑。

流式说明：console 跑局走 SSE 流式（推理内容淡色显示）；超时分层为连接 10s / 停滞看门狗 90s / 单次总墙钟 600s；上游不支持流式时自动回退非流式。无头模式（`python -m src.orchestrator`，供 M6 批量跑局）行为不变，与 console 共用同一个 `Orchestrator.run_game()`。

### 3.6 速度诊断：tools/bench.py

```bash
python tools/bench.py                      # 全部席位 ×（短/长）两种请求，串行
python tools/bench.py --seats kimi,deepseek
python tools/bench.py --skip-long
```

逐席位输出 TTFT / 总时长 / 生成速度（字/s）/ token 用量。注意：会产生真实 API 调用（每席位至多 2 次，长请求约 500 字输出）。

## 4. 配置字段参考

`[settings]` 段：

| 字段 | 说明 |
| ---- | ---- |
| `max_tokens` | 失控安全网（当前 16384）：正常回复 1-2K token 远触不到；篇幅约束在 `prompt/game_system.md` 的"输出篇幅纪律"。若某渠道报"max_tokens 超限"错误，在该席位单独调低 |
| `reasoning_effort` | 全席统一思考强度（当前 `low`，压隐藏思考耗时）；席位可同名覆盖，填 `"none"` 表示不发该参数（claude 席位因上游只支持 adaptive 模式而例外）。启用前用 `python tools/bench.py --effort` 验证渠道兼容性 |

注意：**temperature 不发送**——所有席位统一走渠道默认值（部分中转站上游只接受默认温度，发送会报 400），配置里不要再写 `temperature`。

`[[player]]` 段（每席一条，数量不限，当前模板 8 席）：

| 字段 | 必填 | 说明 |
| ---- | ---- | ---- |
| `id` | 否 | 席位短 id（如 `deepseek`），ping / smoke 点名时用，比输入中文名方便 |
| `name` | 是 | 角色名，用于日志与汇总显示 |
| `model` | 是 | 模型名，以渠道方实际提供的为准 |
| `base_url` | 是 | 接口地址，结尾 `/` 可有可无（程序会自动去掉） |
| `api_key_env` | 是 | 存放真实 key 的**环境变量名** |
| `api_type` | 否 | `openai`（默认，OpenAI 兼容 `/chat/completions`）或 `anthropic`（Claude 原生 `/v1/messages`） |
| `max_tokens` | 否 | 该席位单独的输出上限，覆盖 `[settings]` 的全局值 |

双协议说明：中转站、DeepSeek、Kimi 官方一般都提供 OpenAI 兼容接口，走 `openai`；Claude 官方或支持原生协议的中转站可走 `anthropic`（该协议会把 system 消息提升为顶层参数，`max_tokens` 固定 4096，token 用量归一化为 `total_tokens`）。两种协议可以按席位混用。

## 5. 常见问题

- **`环境变量 XXX 未设置`**：当前终端没读到 key。`export` 只对当前会话有效；检查变量名拼写是否与配置里的 `api_key_env` 完全一致。
- **HTTP 401/403**：key 无效或渠道不对（比如把中转站 key 配到了官方 base_url）。
- **HTTP 404 / 模型不存在**：`model` 字段与渠道方模型名不一致。
- **超时/偶发失败**：超时分层——连接 10s、停滞看门狗 90s（任意两块数据的最长间隔）、单次总墙钟 600s；4xx（除 429）不重试，429/5xx/网络错误最多重试 3 次（429 尊重 Retry-After）。仍失败会输出错误原因；中转站高峰期可稍候重跑。
- **Windows 中文乱码**：脚本已强制 UTF-8 输出；若仍乱码，确认终端为 Git Bash 或 Windows Terminal。

## 6. 模块现状

| 文件 | 状态 | 用途 |
| ---- | ---- | ---- |
| `src/providers.py` | ✅ 可用 | 多协议 API 客户端与配置加载（`load_config` / `chat` / `find_player`），被其他模块复用 |
| `src/prompts.py` | ✅ 可用 | prompt 模板加载器：读取根目录 `prompt/` 下的 md 模板，支持占位符渲染（`__file__` 锚定，任意目录运行皆可） |
| `prompt/` | ✅ 可用 | prompt 模板目录（如 `smoke_system.md` 系统提示），与代码分离、可直接编辑 |
| `personas.toml` | ✅ 可用 | 角色人设卡（性别/体格/国籍/性格人设），smoke 与后续 prompt 模板共用 |
| `src/smoke.py` | ✅ 可用 | 连通性测试（席位选择 / 随机抽取 / 真人席位） |
| `src/ping.py` | ✅ 可用 | 单席位连通性诊断（详细 HTTP 错误输出、`--insecure` 调试开关），见 §3.1 |
| `src/deal.py` | ✅ 可用 | 开局生成器（M1）：席位抽取 + 身份/职业/道具/顺序/保险箱分配 + `game/<seed>/` 输出，见 §3.2 |
| `src/engine/` | ✅ 可用 | 规则裁决引擎（M3）：谋杀/投票/台账/监控/阶段流转，详见 [engine.md](engine.md) |
| `tests/` | ✅ 可用 | 引擎单测 + 平衡场景回归（test_balance，v3 会客室版）+ 编排 mock 全流程（test_orchestrator_game，含中断续跑/API 失败/关键记录/v3 夜间特性）共 92 用例：`python -m unittest discover -s tests` |
| `prompt/game_system.md` | ✅ 可用 | 对局系统提示模板（扮演设定 + 输出契约 + 规则书 + 私密卡），占位符 {name}/{profile}/{card}/{rules} |
| `src/orchestrator.py` | ✅ 可用 | 编排器主循环（M4/M5）：run_game() 驱动完整对局——阶段简报/三通道 JSON 收集/引擎裁决/落盘与断点续跑，见 §3.3 |
| `src/viewer.py` | ✅ 可用 | 视图层（M4.5）：读存档渲染 status/log/peek/inner/host 文本，view.py 与 console.py 共用 |
| `src/view.py` | ✅ 可用 | 只读查看器（M4.5）：`status`/`log`/`peek`/`inner`/`host`/`follow` 子命令，与跑局解耦，见 §3.4 |
| `src/console.py` | ✅ 可用 | 交互驾驶舱（M4.5）：流式跑局 + 顶栏状态 + 随时命令，见 §3.5 |
| `tools/bench.py` | ✅ 可用 | API 速度诊断（流式 TTFT/吞吐），见 §3.6 |
