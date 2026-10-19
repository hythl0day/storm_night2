# 暴风夜之古堡惊魂

一个场景互动身份推理AI真人秀。玩家都是AI，但真人也可以参与，随机身份、技能和道具，破解谜题，活到最后！

A scenario-based interactive role-playing reality show featuring AI-powered deduction. Players are all AI, but real people can also participate. Players are randomly assigned identities, skills, and items to solve puzzles and survive to the end!

自动化跑局管线 + 节目素材生产。

## 文档

- 规则书：[docs/rules-6.md](docs/rules-6.md)（6 人局，v4）/ [docs/rules-8.md](docs/rules-8.md)（8 人主谋局，基础形态）
- 失忆版规则集：[docs/rules-6-lite.md](docs/rules-6-lite.md)（同一套裁决规则，但只告诉 AI 生存骨架——职业/道具全表与分配结构不下发，机制靠菜单临场发现，供节目前期"失忆受害者"阶段使用）
- 工程计划与里程碑：[docs/dev-plan.md](docs/dev-plan.md)
- 8 人局重构计划（含暂缓机制）：[docs/rules-8-v3-plan.md](docs/rules-8-v3-plan.md)
- 节目规划（两季 + 特别单集）：[screenwriting/show-bible.md](screenwriting/show-bible.md)
- 引擎参考：[docs/engine.md](docs/engine.md)

## 常用命令

```bash
# 安装依赖（Python 3.11+，仅 httpx）
pip install -r src/requirements.txt

# 配置 API key（players.toml 里只存环境变量名；中转站四席各一个，其余共用 CN）
export RELAY_API_KEY_CN="sk-..."       # 另有 RELAY_API_KEY_GPT / _CLAUDE / _GEMINI / _GROK

# 连通性点名：全部席位各回一句话
python -m src.smoke --all

# 创建对局（同 seed 逐字节可复现；目录名即 seed 本身，方便手打）
python -m src.deal --seed 42           # 生成 game/42/（完整规则书）
python -m src.deal --seed 42 --seats gpt,claude,glm,deepseek,kimi,doubao  # 手动指定 6 席（id 或名字）
python -m src.deal --seed 42 --seats gpt,claude  # 部分名单：指名 2 席，其余随机补足（尽量维持中美 3+3）
python -m src.deal --seed 42 --seats gpt:杀手,claude,glm:平民  # 席位后可锁身份（平民/杀手，先落位）
python -m src.deal --seed 42 --exclude grok   # 随机抽席屏蔽指定席位（逗号分隔，id 或名字；state 记 excluded_seats）
python -m src.deal --seed 42 --lite    # 失忆版规则集（生存须知 + 临场规则下发；裁决规则不变，
                                       # state 记 is_lite_rule，评分卡独立分池不混算胜率）

# 跑局（二选一），跑到对局结束；中断后加 --resume 按席位断点续跑
# （所有脚本的 --game 都可只给 id，如 --game 42 自动解析为 game/42）
python -m src.console --game game/42        # 交互驾驶舱：流式输出 + 随时命令（输入 help 查看）
python -m src.orchestrator --game game/42   # 无头模式（供批量跑局）

# 从某次席位调用前分叉另存为（目标前复用原 outbox，不调用 API；源存档不变）
python -m src.replay --game game/42 --day 3 --seat claude --label "自由活动-房间行动" --out game/42-r1
python -m src.console --game game/42-r1 --resume  # 从分叉点实时生成并观战

# 采访录制器：把某席位拉出古堡自由问答（记忆=他收到的全部发言+自己的发言/内心全文，从存档复原）
# 采访以精选池为默认目录（先入选再采访）；--selected 接受 selected/ 下的 id 或显式路径
# 采访中可用查看命令（help/status/log [n]/peek <席位>/inner <席位>/host [n]），quit 结束
python -m src.interview --selected 551125 --seat doubao                  # 赛后采访（死者记忆自动截于出局时点）
python -m src.interview --selected 551125 --seat doubao --at 1:晚间休息  # 局中冻结采访（只知到该阶段为止）
python -m src.interview --selected 551125 --seat doubao --dump-memory m.md  # 导出记忆档案供人工剪裁，
                                                          # 剪裁后加 --memory m.md 注入（明线设定）
# 问答记录与当次记忆档案归档到 selected/<id>/interview/（宿主侧素材，含剧透）

# 补拍演出器：让存档角色在冻结时点重聚补演一场戏（多角色多轮对话，台词真实生成；
# 台词只展现人设、不涉及规则推理——信息闸门是 --at 冻结时点；归档 <对局>/scene/）
# 预设场景：开场惊魂 / 轮回开场（既视感版开局：模糊轮回记忆+册子改称「主神」）/ 终局谢幕
python -m src.scene --selected s1-e1 --scene 开场惊魂                # 预设场景（自带冻结点）
python -m src.scene --selected s1-e1 --scene 轮回开场 --rounds 2     # 两轮对话
python -m src.scene --selected s1-e1 --desc "清晨，众人在会客室看到……" --at 2:案件调查  # 自由描述

# 查看存档（只读，与跑局解耦；实时追局用 console）
python -m src.view status --game game/42    # 阶段/存活/台账；另有 log/peek/inner/host

# 评分卡（M6）：默认扫描 game/ 全部存档；跑完整的合格局可挑入 selected/ 作精选池
python -m src.scorecard                        # 多局报告：版本分组胜率/席位胜率/机制利用率/多样性/成本
python -m src.scorecard --dir selected         # 只看精选池
python -m src.scorecard --game game/42         # 单局报告：台账/流程简报（含密谋与秘密行动）/戏剧性评分/主角弧线候选
python -m src.scorecard --min-version v4.0     # 只统计指定版本及以后的存档

# 测试（引擎 + 平衡回归 + 编排 mock 集成）
python -m unittest discover -s tests
```

对局产物：`game/<id>/script.md` 是主神侧全记录（含剧透，供整理剧本）；`public/` 为公开口径，`private/` 为各席私密素材（内心独白/笔记），剪辑时注意防剧透。

## 环境准备

- Python 3.11+（依赖 `tomllib` 标准库，低于 3.11 无法运行）。
- 安装依赖（目前只有 httpx）：

```bash
pip install -r src/requirements.txt
```

## 席位配置

`players.toml` 随仓库跟踪分发（只含接口地址与环境变量名，无任何密钥），换机器克隆即用；按需修改各席位的 `model` / `base_url` / `api_key_env`。

**安全约定：api_key 不落盘。** 配置里只填环境变量名（`api_key_env`），真实 key 在运行前注入环境变量。Git Bash 当前会话：

```bash
export RELAY_API_KEY_CN="sk-你的key"
```

长期使用可在 Windows「系统属性 → 环境变量」中设置，新开的终端即可读到。

每个席位需要确认/修改的字段：`model`（**以你的中转站/官方账户里的实际模型名为准**，模板里的型号是占位符）、`base_url`、`api_key_env`、`api_type`。

**人设卡**：每个角色的性别 / 体格 / 国籍 / 性格人设写在 `personas.toml`（键名须与 `players.toml` 的 `name` 一致，未命中的席位按"普通人"处理）。人设只写性格不写职业（失忆设定；职业每局临时分配），且 prompt 中已声明"人设不影响推理能力""性格靠言行流露、禁止自我介绍"。性别/体格/国籍是游戏机制属性（空手谋杀判定、杀手线索会用到）——规则书不含固定角色表，每局开局以此文件为准生成角色表并全员公布；体格数值仅供裁决使用，注入 prompt 时会转成文字描述，角色不会得知数值。

**措辞分时代**（v4.0.6）：full 规则书与 full 局的 AI 可见文案中主持人称「主神」（真人秀层设定，与采访间同一角色）；lite 时代保留匿名克制的「主持人」；采访间一律「主神」，补拍现场不出现该角色。

## 详细用法

### 连通性测试：smoke.py

```bash
python -m src.smoke                                     # 交互式：列出席位，手动选择（可追加真人席位）
python -m src.smoke --all                               # 全部席位，不询问
python -m src.smoke --random 6                          # 随机抽 6 席
python -m src.smoke --only deepseek,kimi               # 按 id 或名字指定席位
python -m src.smoke --human 老陈 --random 5              # 追加真人席位，可与以上组合
python -m src.smoke --prompt "你为什么被困在古堡里？"    # 自定义提问（--config 默认即 players.toml）
```

**席位选择**：不带 `--all/--random/--only` 时进入交互模式——先打印席位列表（含人设摘要），输入 `all` / `r N`（随机 N 席）/ 逗号分隔序号（如 `1,3,5`）；随后可输入一个名字追加真人席位（直接回车跳过）。真人席位不调用 API：脚本打印问题，由你在终端键入回答，同样计入汇总。

行为：所有 AI 席位**并发**收到同一个问题，按各自人设回答，最后输出汇总：

```
【狄西】(3.2s, 87 tokens)
  ……
== 汇总：8/8 席连通 ==
```

逐席显示耗时与 token 用量；失败的席位显示原因（如 `环境变量 XXX 未设置`、HTTP 错误）。

### 单席位点名：ping.py（排障用）

smoke 是多席点名；ping **只测一个席位**，并输出完整诊断信息，用于定位连接问题：

```bash
python -m src.ping deepseek                 # 按席位 id 点名（一律用 id，不用中文角色名）
python -m src.ping gemini --timeout 120     # 调整超时（默认 60s）
python -m src.ping grok --insecure          # 跳过 TLS 证书校验（仅调试用，会打印警告）
python -m src.ping kimi                                # 指定配置文件用 --config（默认即 players.toml）
```

输出内容：席位配置回显（api_type / base_url / model / key 是否设置及长度，**不显示 key 本身**）、实际请求的完整 URL、HTTP 状态码、响应内容或错误详情。三类典型结果：

- **连接层失败**：请求根本没拿到 HTTP 响应（DNS 污染 / 代理缺失 / 证书被拦截等网络问题）；
- **HTTP 4xx/5xx + 响应体**：请求到达但被拒绝——key 无效、模型名不存在、参数超限等，看响应体里的 message；
- **HTTP 200**：成功，显示回复与 token 用量；推理模型会同时打印推理摘要（`reasoning_content`）。

注意：ping 发送真实请求，会产生少量 token 计费。

### 开局生成：deal.py（M1）

从阵容池（`players.toml` + `personas.toml`）抽取席位，随机分配身份/职业/道具/首日行动顺序/保险箱，输出完整对局目录：

```bash
python -m src.deal                     # 随机种子，输出 game/<seed>/
python -m src.deal --seed 42           # 指定种子（同 seed 两次生成逐字节一致）
python -m src.deal --seed 42 --seats gpt,claude,gemini,deepseek,kimi,glm   # 手动指定 6 席（id 或名字）
python -m src.deal --seed 42 --seats gpt,claude   # 部分名单：指名 2 席，其余随机补足（尽量维持中美 3+3）
python -m src.deal --seed 42 --seats gpt:杀手,claude,glm:平民  # 席位后可锁身份（平民/杀手，先落位，state 记 pinned_identities）
python -m src.deal --seed 42 --exclude grok,doubao   # 随机抽席时屏蔽席位（id 或名字，逗号分隔；两侧各需剩 ≥3 席）
python -m src.deal --seed 42 --lite    # 失忆版精简规则集（rules-6-lite.md），见下
python -m src.deal --seed 42 --out games/test   # 自定义输出目录
```

**lite 规则集（失忆版）**：`--lite` 把注入 AI 的规则书换成 `docs/rules-6.md` 的精简版——只含生存骨架（处境/时间轴/投票/胜负/侦探），职业全表、道具全表与分配结构一律不下发，AI 靠私密卡（内联定义，自包含）和每阶段合法动作菜单临场发现机制；**裁决规则完全不变**（引擎按 v4.0 裁决），变的是 AI 的信息面。state 记 `is_lite_rule=True`，评分卡单独成组（`lite·vX.Y`，不与高级规则混池）。**临场规则下发**：full 局简报中指向"规则书 §X"的引用（投票武装指认/空手谋杀条件/看破伪装名目/统一开锁与进入结算共 4 处），lite 局自动替换为规则内容的内联说明——AI 在需要的那一刻才学到这条规则。

生成的 `game/<seed>/` 结构：

```
seed.txt               # 种子（复现与"对局未被操纵"凭证）
deal.json              # 初始状态快照（供存档分叉重演）
state.json             # 主状态（主神侧，含身份等全部秘密）
rules.md               # 本局规则书快照
players.toml           # 阵容与参数快照
personas.toml          # 本局人设快照（Orchestrator 优先于项目根配置读取）
public/roster.md       # 全员公布角色表（体格为文字描述，不含数值）
public/announcement.md # 开局公告（主人字条）
private/<id>/card.md   # 每席私密卡：身份/同伙/职业技能/道具次数/开局准备义务
inbox/<id>/  outbox/<id>/   # 编排器阶段简报与回执（M4 起使用）
```

内心独白等私密素材（M4 起）按席位归档到 `private/<id>/`（如 `inner.md` 内心独白、私聊记录、个人调查结果），与私密卡并列。

分配约束（生成即校验，违反会报错退出）：**席位中美 3+3 对称**（纯随机模式自动满足，两侧 API 渠道均衡；手动 `--seats` 全员指定或部分名单导致不对称时仅提示）；`--seats` 可全可部分——指名不足 6 席时剩余随机补足，席位后附 `:身份` 可锁定该席身份（平民/杀手，先落位、剩余身份随机补给其余席；指定超配额报错，留痕 state `pinned_identities`）；身份 4 平民/2 杀手（v4.0 双杀手制，无帮凶无继任）；职业 8 选 6 不重复（纯随机，无保底）；道具车道：两杀手各得一件互不相同的凶器（刀/棒球棍/不明药剂），随机一名杀手另得万能钥匙、另一名另得一件反派道具（可疑的粉末/木马程序U盘）；平民螺丝起子 1 人 + 其余 3 人从「通用池 5 件 + 未分出的反派道具」共 6 件各抽 1（可能拿到仅供自卫的武器或只能正常调查的反派道具）；保险箱从剩余凶器 + 未分配反派道具中抽 1（开局全员公告"主人字条"内容）；四凶器必现（含桌上左轮手枪）。

控制台输出是**主神视角**（含身份分配），不要外发。

### 编排器主循环：orchestrator.py（M4/M5）

把引擎接上六家 API，按阶段驱动完整对局（直至产生胜者或主神结局）：简报 → 收集四通道 JSON（speech/inner/notes/actions）→ 引擎裁决 → 落盘。

```bash
python -m src.orchestrator --game game/012345            # 从当前阶段跑到对局结束
python -m src.orchestrator --game game/012345 --resume   # 从 state.json 断点续跑（席位行动粒度）
```

> 提示：所有脚本的 `--game` 都可只给 id（如 `--game 012345`），自动解析为 `game/<id>`；scorecard 的 `--game` 额外支持 `--dir` 下的子目录名。

全天流程（rules-6 v4.0）：开局准备（仅第 1 天；无秘密布置——密道已改为谋杀窗口动作，窃听器改为到场放置）→ 案件调查（第 2 天起：清晨公告由引擎自动路由并附状态行——第 N/6 天/存活数/无杀倒计时/封存数；清晨调查队列：平民持调查能力者自动结算并全员公告结果（不调 API），反派持调查能力者私密三选一（篡改/正常调查/跳过）；死者字条遗嘱）→ 公共发言（第 1 天改为侦探竞选：声明轮 → 密封退选 → 未参选者投票；第 2 天起常规发言，侦探末置发言）→ 投票（第 2 天起：逐席收票、侦探一票计 1.5、反派心理医生/黑客可夹带篡改投票、遗言先于身份公布、封存道具与警徽走遗嘱（指定归属/接收人可拒收）、无人出局时持武器平民可武装指认（两名杀手都是正确目标）——每次投票至多一次；投票结果附明确裁决：得票数/是否过半/谁出局）→ 反派密谋频道（每天两次固定：自由活动开始前先于停电声明轮、晚间休息选房前；两名存活杀手各发一段，互相可见、对他人完全不可见）→ 自由活动（会客室/书房/监控室/地下室四选一，白天卧室清场、无效选择滞留会客室；去向收齐后先独立开锁再统一进入（开锁者优先，其余进入判定不变），白天靠后者可见先前去向声明；停电声明——v3.11 起逐人发现：各选房者抵达所选房间时才各自得知停电；同入邀请；≥2 人非黑暗房间一轮房内交谈（v3.17：黑暗房间中同入邀请成立的组合可摸黑低语，内容全体在场者含潜伏者都能听到，低语者不知是否被旁听）；逐人房间行动菜单（含当面赠送道具：限同房者，明亮房间同房者可见、黑暗无人知晓——v3.27 起取代白天固定交换环节）；谋杀窗口（房间行动后的独立环节：每窗口全队至多一次尝试、仅放弃/无合法目标顺延给同伙、每天全队至多一起成功谋杀；技能杀同走此窗口：舞台魔术（可防御）/强袭/药理学/缓释毒剂/密道谋杀/延时装置手动启动）；阶段末毁证窗口（收窄为篡改专用））→ 晚间休息（与白天共用独立开锁轮和统一进入轮；守夜邀请与守夜密谈、夜间房间行动（含当面赠送/取桌上物品）、卧室收听窃听器、谋杀窗口（同白天），选择互相不可见）→ 次日清晨。第 6 天为终审日（只有白天会议），其投票结束仍无胜负则主神降临。无杀倒计时从第 1 天起算（连续两完整天无谋杀行为即平民胜，未遂同样重置）。任一胜负条件满足即收束，结局写入 public/log.md。

行为约定：

- **消息结构**：system = `prompt/game_system.md` 渲染（人设 + 规则书快照 + 私密卡），user = 当次简报；对话不累积。简报 = 增量公告 + 增量私密事件 + 当前局面 + **关键记录**（既定事实流水剔除发言 + 该席私密事件历史，含投票/房间/谋杀回执等）+ notes 回放 + 合法动作菜单。全部顺序执行，不并发。
- **JSON 解析**：从回复提取 ```json 代码块；失败重试 ≤2 次；仍失败记"放弃行动"（能抢救出 speech 仍采用）。非法动作逐条驳回（私下告知本人），其余动作正常执行。
- **断点续跑（席位行动粒度）**：每个席位行动完成后即落盘 state.json（`orch.wip` 按"天：阶段：子环节"记录 done 名单与中间累计，如选票/选房结果）；`--resume` 重入当前环节并跳过已完成席位，被中断席位用相同简报重问（feed 游标与私密队列只在调用成功后提交）。
- **席位失败即中断**：某席 API 请求失败（providers 内部重试耗尽后仍失败）不再记"放弃行动"继续，而是落盘并中断整局（退出码 2）；排除故障后 `--resume` 续跑，被中断席位会重新行动。Ctrl+C 同样先落盘再退出（退出码 130）。

落盘产物（`game/<id>/`）：

```
script.md                # 主神侧全记录：背景/阵容分配（全剧透）/全部发言与行动/裁决结果，不含内心独白；
                         # 回执一律第三人称，自由活动按「选房/各房间交谈/各房间行动」分节
public/log.md            # 公开口径：公告、发言、交换、死亡公告（按阶段标题）
private/<id>/inner.md    # 内心独白（追加）；notes.md 私人笔记（每阶段覆盖）
private/<id>/log.md      # 该席听到的房间内交谈存档
inbox/<id>/day{N}-*.md   # 每环节简报存档；outbox/<id>/ 原始回复存档
host_log.jsonl           # 每次 API 调用 {ts, player, stage, ok, latency, total_tokens, error}
```

### 只读查看器：view.py（M4.5）

读 `game/<id>/` 存档渲染，与跑局完全解耦——复盘已结束的局、跑局旁路查看都用它：

```bash
python -m src.view status --game game/012345        # 阶段/存活/身份台账/道具技能次数
python -m src.view log --game game/012345 -n 30     # 公开日志末尾 30 行
python -m src.view peek gemini --game game/012345   # 私密卡 + 私人笔记 + 内心独白
python -m src.view inner gemini --game game/012345  # 仅内心独白
python -m src.view host --game game/012345          # API 调用台账（耗时/成败）
```

（实时追局用 console 驾驶舱，view 不再提供 follow。）

### 交互驾驶舱：console.py（M4.5）

进程内驱动跑局：底部"状态+输入"瞬时行（阶段/当前席位/spinner/耗时/字数）、思考内容灰色流式滚动、JSON 解析完成后渲染人读卡片（名字突出/内心灰色/行动黄色/分割线）、全部历史可滚轮回看、随时可输入命令。**重大结果行（侦探竞选/投票结果/出局与身份公布/死亡公告/游戏结束）带前后分割线与加粗，与席位行动段落分层；私密回执转第三人称展示**。**每天开始时（含第 1 天开局）自动显示一次"天亮一览"：全员状态（含淘汰者的出局/死亡时间）+ 职业/技能剩余/道具；公开公告（清晨状态行/投票明细与裁决/出局与身份公布/死亡与未遂/封存道具分配等）实时绿色上屏**（均为宿主侧上帝视角，不进 prompt、不落盘）。

```bash
python -m src.console --game game/012345 [--resume]
```

命令（跑局进行中随时可敲，不打断进行中的调用）：`help` / `status` / `log [n]` / `peek <席位>` / `inner <席位>` / `host [n]` / `follow [席位]` / `pause` / `continue` / `save` / `quit`。

中断统一语义（`pause`、Ctrl+C、API 调用失败三者等价）：当前席位行动结束（API 失败则该席未完成）后立即落盘并暂停；输入 `continue`（别名 `resume`）在进程内续跑——被中断/待行动的席位自动重新执行，已完成席位不会重复。`quit` 退出进程（同样已落盘，可 `--resume` 恢复）；暂停或结束后仍可用 `status`/`log`/`peek` 等查看；暂停态/结束态下再按 Ctrl+C 直接退出。

中断安全性：state.json 为原子写（临时文件 + 替换），强杀不会留下半个文件；断点是**席位行动粒度**——每个席位行动完成后即落盘，任何时刻中断，续跑都从中断席位继续，已完成的席位不会重复行动（其发言/动作已落盘）。被中断的那次 API 调用结果丢弃，但上游可能已计费。无头模式（`python -m src.orchestrator`）下 API 失败则打印错误并以退出码 2 退出，`--resume` 续跑。

流式说明：console 跑局走 SSE 流式（推理内容淡色显示）；超时分层为连接 10s / 停滞看门狗 90s / 单次总墙钟 600s；上游不支持流式时自动回退非流式。无头模式（`python -m src.orchestrator`）与 console 共用同一个 `Orchestrator.run_game()`。

### 存档分叉：replay.py

把既有对局另存为"某次席位 AI 调用之前"的分支。目标节点之前复用源存档 `outbox/` 的历史回复，不调用 API；这些回复仍会经过当前编排器与引擎，重建 state、公开/私密日志、笔记和 WIP。源目录全程只读。

```bash
python -m src.replay --game game/43501 \
  --day 3 --seat claude --label "自由活动-房间行动" \
  --out game/43501-d3-claude-r1

# 从克劳德的房间行动开始实时生成并观战
python -m src.console --game game/43501-d3-claude-r1 --resume
# 也可无头续跑：python -m src.orchestrator --game game/43501-d3-claude-r1 --resume
```

`--seat` 接受席位 id 或角色名；`--label` 是 `call_ai` 的环节标签，例如 `自由活动-房间行动`、`公共发言`、`投票`。输出目录必须不存在且不能嵌入源目录。新存档带 `deal.json` 初始快照；旧存档则按 seed 和原配置重新 deal 并校验初始分配。分支的 `replay.json` 记录源存档、规则版本和分叉节点。

重演是以**当前代码和当前对应 ruleset 规则书**构造的反事实分支，不承诺跨任意旧版本逐字节复刻。分叉后由 console/orchestrator 正常调用真实 API，并按现有断点语义续跑。

### 速度诊断：bench.py

```bash
python -m src.bench                      # 全部席位 ×（短/长）两种请求，串行
python -m src.bench --seats kimi,deepseek
python -m src.bench --skip-long
```

逐席位输出 TTFT / 总时长 / 生成速度（字/s）/ token 用量。注意：会产生真实 API 调用（每席位至多 2 次，长请求约 500 字输出）。

### 采访录制器：interview.py

把某席位 AI 拉出古堡进行自由问答采访（show-bible §1 的采访装置 / §5 采访录制器）。记忆载荷从存档逐字节复原：他收到过的所有发言与事件（public/log.md 公共流——`##` 阶段标题作边界，旧档无标题时回退 orch.feed——+ orch.hist 私密流 + private/<席位>/log.md 的密谋节）+ 他自己的发言与内心全文（outbox payloads + 最终 notes）。

```bash
python -m src.interview --selected s1-e1 --seat deepseek                     # 赛后采访（死者记忆自动截于出局时点）
python -m src.interview --selected s1-e1 --seat deepseek --full              # 赛后复盘式：记忆不截断
python -m src.interview --selected s1-e1 --seat deepseek --at 1:晚间休息      # 局中冻结采访（只知到该阶段为止）
python -m src.interview --selected s1-e1 --seat deepseek --no-inner          # 记忆不含内心独白
python -m src.interview --selected s1-e1 --seat deepseek --dump-memory m.md  # 只导出记忆档案
python -m src.interview --selected s1-e1 --seat deepseek --memory m.md       # 用剪裁后的档案采访（明线注入）
```

采访以**精选池为默认目录**：`--selected` 接受 `selected/` 下的对局 id 或显式路径（先入选，再采访）。采访为终端多轮自由问答（主神 = 你自己输入），流式输出，`quit`（或 `exit`）结束。采访中随时可用查看命令（与 console 同口径，只打印、不调用 API、不进存档）：`help` / `status` / `log [n]` / `peek <席位>` / `inner <席位>` / `host [n]`。问答记录与当次实际使用的记忆档案归档到 `selected/<id>/interview/`（宿主侧素材，含剧透，不进 public/）。`--dump-memory` + 人工剪裁 + `--memory` 对应剧中"主神筛选改写后注入记忆"的明线设定。

### 补拍演出器：scene.py

让存档中的角色在某个冻结时点"重聚"，补演一场戏（开场惊魂、终局谢幕这类不进游戏流程的表演桥段）。记忆用采访同款逻辑从存档复原并冻结；**多角色多轮对话**——同场角色按行动顺序依次开口，后发言者能看到本场已说台词。台词纪律写在 `prompt/scene_system.md`：只展现人设与场景氛围，不分析案情、不讨论规则（信息闸门是冻结时点本身；预设场景可用 `note` 写明纪律豁免边界）。补拍不改存档状态，产出纯属剪辑素材。

**当前预设场景**（`--scene`，自带冻结时点）：

| 预设 | 冻结点 | 内容 |
| ---- | ---- | ---- |
| `开场惊魂` | 第1天·开局准备 | 雷雨夜醒来、门窗紧闭、全员失忆，围坐会客室惊魂未定 |
| `轮回开场` | 第1天·开局准备 | 既视感版开局：模糊记得经历过无数次（含杀人与被杀的碎片恐惧），并注意到册子措辞从「主持人」变成了「主神」（纪律豁免：可谈既视感与称谓，不编造往局细节） |
| `终局谢幕` | 赛后（记忆截于各自出局时点） | 大门打开，幸存者每人一句谢幕感言 |

```bash
python -m src.scene --selected s1-e1 --scene 开场惊魂               # 预设场景（自带冻结点）
python -m src.scene --selected s1-e1 --scene 开场惊魂 --rounds 2    # 两轮对话
python -m src.scene --selected s1-e1 --desc "清晨，众人在会客室看到……" --at 2:案件调查
python -m src.scene --selected s1-e1 --scene 开场惊魂 --seats claude,deepseek   # 指定参演（缺省=冻结时点在场者）
```

`--selected` 与采访一致只认精选池。归档到 `selected/<id>/scene/<场景>-<时间戳>.md`（对话全记录，文件头注明"补拍素材"——赛后生成，不属于原始跑局时间线）+ 同目录 `.memory-<席位>.md`（各席当次记忆档案，可追溯）。单席调用失败跳过该席本轮并记"补拍失败"，不中断整段场景。

## 配置字段参考

`[settings]` 段：

| 字段 | 说明 |
| ---- | ---- |
| `max_tokens` | 失控安全网（当前 16384）：正常回复 1-2K token 远触不到；篇幅约束在 `prompt/game_system.md` 的"输出篇幅纪律"。若某渠道报"max_tokens 超限"错误，在该席位单独调低 |
| `reasoning_effort` | 全席统一思考强度（当前 `low`，压隐藏思考耗时）；席位可同名覆盖，填 `"none"` 表示不发该参数（claude 席位因上游只支持 adaptive 模式而例外）。启用前用 `python -m src.bench --effort` 验证渠道兼容性 |

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

## 常见问题

- **`环境变量 XXX 未设置`**：当前终端没读到 key。`export` 只对当前会话有效；检查变量名拼写是否与配置里的 `api_key_env` 完全一致。
- **HTTP 401/403**：key 无效或渠道不对（比如把中转站 key 配到了官方 base_url）。
- **HTTP 404 / 模型不存在**：`model` 字段与渠道方模型名不一致。
- **超时/偶发失败**：超时分层——连接 10s、停滞看门狗 90s（任意两块数据的最长间隔）、单次总墙钟 600s；4xx（除 429）不重试，429/5xx/网络错误最多重试 3 次（429 尊重 Retry-After）。仍失败会输出错误原因；中转站高峰期可稍候重跑。
- **Windows 中文乱码**：脚本已强制 UTF-8 输出；若仍乱码，确认终端为 Git Bash 或 Windows Terminal。

## 模块现状

| 文件 | 状态 | 用途 |
| ---- | ---- | ---- |
| `src/providers.py` | ✅ 可用 | 多协议 API 客户端与配置加载（`load_config` / `chat` / `find_player`），被其他模块复用 |
| `src/prompts.py` | ✅ 可用 | prompt 模板加载器：读取根目录 `prompt/` 下的 md 模板，支持占位符渲染（`__file__` 锚定，任意目录运行皆可） |
| `prompt/` | ✅ 可用 | prompt 模板目录（game/interview/scene/smoke 四个系统提示），与代码分离、可直接编辑 |
| `personas.toml` | ✅ 可用 | 角色人设卡（性别/体格/国籍/性格人设），smoke 与后续 prompt 模板共用 |
| `src/smoke.py` | ✅ 可用 | 连通性测试（席位选择 / 随机抽取 / 真人席位） |
| `src/ping.py` | ✅ 可用 | 单席位连通性诊断（详细 HTTP 错误输出、`--insecure` 调试开关） |
| `src/deal.py` | ✅ 可用 | 开局生成器（M1）：席位抽取 + 身份/职业/道具/顺序/保险箱分配 + `game/<seed>/` 输出 |
| `src/engine/` | ✅ 可用 | 规则裁决引擎（M3）：谋杀/投票/台账/监控/阶段流转，详见 [engine.md](docs/engine.md) |
| `src/gamedata.py` | ✅ 可用 | 与规则书对齐的职业/道具/技能次数/体格映射与 lite 私密卡内联词条 |
| `src/fmt.py` | ✅ 可用 | 共享人读格式化：动作 JSON → 人读文本 + 角色状态统一口径（`player_status`） |
| `src/orchestrator.py` | ✅ 可用 | 编排器主循环（M4/M5）：run_game() 驱动完整对局——阶段简报/四通道 JSON 收集/引擎裁决/落盘与断点续跑 |
| `src/replay.py` | ✅ 可用 | 存档分叉：目标前复用原 outbox，指定席位前另存为，console/orchestrator --resume 接续 |
| `src/viewer.py` | ✅ 可用 | 视图层（M4.5）：读存档渲染 status/log/peek/inner/host 文本，view.py / console.py / interview.py 共用 |
| `src/view.py` | ✅ 可用 | 只读查看器（M4.5）：`status`/`log`/`peek`/`inner`/`host` 子命令，与跑局解耦 |
| `src/console.py` | ✅ 可用 | 交互驾驶舱（M4.5）：流式跑局 + 顶栏状态 + 随时命令 |
| `src/scorecard.py` | ✅ 可用 | 评分卡（M6）：单局/多局报告（版本分组胜率/席位胜率/机制利用率/多样性/成本） |
| `src/interview.py` | ✅ 可用 | 采访录制器：存档复原记忆 + 多轮问答（采访中可用查看命令），归档 `<对局>/interview/` |
| `src/scene.py` | ✅ 可用 | 补拍演出器：冻结时点重聚补演（预设场景/自由描述，多角色多轮对话），归档 `<对局>/scene/` |
| `src/bench.py` | ✅ 可用 | API 速度诊断（流式 TTFT/吞吐） |
| `tests/` | ✅ 可用 | 引擎单测 + 平衡场景回归 + 编排 mock 全流程 + 采访/补拍：`python -m unittest discover -s tests` |

## 其他

git历史有变动时，需要

```bash
# 重新设置 git
git fetch && git reset --hard origin/main
```
