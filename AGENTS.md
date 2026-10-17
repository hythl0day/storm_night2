# AGENTS.md

## 这是什么项目

《暴风夜》：多家顶尖 AI 参与的"暴风雪山庄"身份推理游戏节目工程。核心是一条**自动化跑局管线**：自研编排器直连各家 AI 的 API，让它们扮演失忆后被困古堡的角色互相推理、欺骗、谋杀，产出结构化、防剧透、可直接用于剪辑的对局素材。节目规划（两季 + 彩蛋单集）见 `screenwriting/show-bible.md`。

## 目录结构

```
src/          Python 代码（跑局管线）
  providers.py    多协议 API 客户端与配置加载（load_config / chat / find_player）
  gamedata.py     游戏静态数据（职业/道具/体格映射，与规则书对齐）
  deal.py         开局生成器（M1）→ game/<seed>/
  engine/         规则裁决引擎（M3）：state / murder / vote / ledger / core
  orchestrator.py 编排器主循环（M4/M5）：run_game() 驱动完整对局 + 观察者钩子；无头模式与 console 共用
  replay.py       存档分叉：目标前复用原 outbox 重建到指定席位前并另存为，console/orchestrator --resume 接续
  viewer.py       视图层（M4.5）：读存档渲染 status/log/peek/inner/host，与跑局解耦
  view.py         只读查看器薄壳（M4.5）：status/log/peek/inner/host/follow 子命令
  console.py      交互驾驶舱（M4.5）：流式跑局 + 顶栏状态 + 随时命令；跑局时即实时观战（M8 已取消），M7 的载体
  smoke.py        连通性测试（席位选择/随机/真人席位）
  ping.py         单席位连通性诊断
  prompts.py      prompt 模板加载器（__file__ 锚定项目根）
players.toml  席位配置（项目根，无密钥，随仓库跟踪；api_key 只填环境变量名）
personas.toml 角色人设卡（项目根；性别/体格/国籍/性格）——角色属性的唯一权威来源
prompt/       prompt 模板（md，{占位符} 渲染；game_system.md 含输出篇幅纪律）
screenwriting/ 节目计划 show-bible.md（两季 + 彩蛋单集）/ video_script.md
docs/         规则书 rules-6.md（6人局）/ rules-6-lite.md（失忆版精简规则集：AI 只知生存须知，机制靠菜单临场发现）/ rules-8.md（8人局）+ dev-plan.md 工程计划 + usage.md（脚本用法）/ engine.md（引擎参考）
tools/        bench.py（API 速度诊断：流式 TTFT/吞吐）
game/<seed>/  对局存档（gitignore；seed 可复现 deal，AI 输出不可复现）
tests/        unittest（python -m unittest discover -s tests）
```

## 架构设计原则

1. **自研编排器，不接入各家官方 CLI**：六家共用同一套工具与 prompt 脚手架，差异只来自模型本身（公平性）；通用 shell 无法做信息隔离（AI 只能看到编排器按阶段下发的内容）。
2. **机器可裁决**：规则全部编码进 `src/engine/`；AI 的自由文本只管"说"，状态变更只认结构化动作 JSON（`speech`/`inner`/`notes`/`actions` 四通道）。非法动作驳回且不公开。
3. **可复现**：每局一个 seed；同 seed 的 deal 逐字节一致。
4. **素材即存档**：`public/`（公告/发言/投票）与 `private/<id>/`（私密卡/内心独白/笔记）天然分线，剪辑时防剧透。
5. **断点续跑**：每个席位行动完成后 state.json 落盘（席位行动粒度，`orch.wip` 按"天:阶段:子环节"记断点）；API 失败 / console 的 pause / Ctrl+C 三者统一在当前席位断点暂停并落盘（API 失败时无头模式直接退出打印错误）；console 内 `continue` 进程内续跑，或 `--resume` 从被中断席位续跑。

## 硬性约定（改动前先读这里）

- 环境：Windows + Git Bash，Python 3.11+（tomllib），依赖只有 httpx。
- 所有脚本入口头部 reconfigure stdout/stdin 为 UTF-8（GBK 控制台会炸）。
- API 请求**不发送 temperature**（apinebula 的 Claude 上游只接受默认温度，全席统一不传）；`max_tokens` 由 `[settings]` 统一定义，席位可覆盖。`reasoning_effort` 同理全席统一（当前 low），claude 因上游限制例外不发（见 players.toml 注释）。
- 席位以 `id` 为主键（gpt/claude/gemini/grok/deepseek/kimi/doubao/glm）；存档目录一律用 id，不用中文名。
- 角色的性别/体格/国籍以项目根 `personas.toml` 为新局权威来源，生成时随局快照；规则书不含固定角色表（开局由 deal.py 生成并全员公布）；**体格数值绝不注入 prompt**——注入时转成文字（魁梧/健壮/普通/瘦弱），prompt 里禁止角色说数值。
- 人设只写性格底色（失忆设定，无传记无职业）；prompt 要求"靠言行流露、禁止自我介绍性格、人设不影响推理"。
- 随机选席为**中美 3+3 对称**（两家 API 渠道均衡）；手动 `--seats` 不对称仅提示。
- deal.py 的随机调用顺序固定（见其 docstring），改顺序 = 改变 seed 语义。
- 文件协议：`game/<id>/` 下 `seed.txt / deal.json（初始快照）/ state.json / rules.md / players.toml / personas.toml（人设快照，旧档缺则回退项目根）/ script.md（主持人侧全记录，含剧透，不含内心独白）/ public/ / private|inbox|outbox/<席位id>/`；分叉存档另有 `replay.json`（源存档与分叉节点，普通旧档可无）。
- 规则书（`docs/rules-6.md` / `rules-6-lite.md` / `rules-8.md` 及开局快照 `game/<id>/rules.md`）是 AI 的**唯一规则输入**：书内禁止修订记录、版本变更叙述（如"v3.27 起…""原…取消"）、对外部文件或其他版本规则书的引用——"见 §X"式内部章节互引可以。修订历史一律写在引擎代码注释（vX.Y 前缀）与 git 提交信息里。改规则时同步改：规则书 + `src/engine/` + `src/orchestrator.py`（菜单/简报/提示）+ 测试，并把 `deal.py` 的 `RULES_VERSION` +0.01。

## 测试与自检

```bash
python -m src.smoke --all              # 八席连通点名（需 RELAY_API_KEY_* 环境变量）
python -m src.deal --seed 42           # 生成样例局 game/42/（目录名即 seed）
python -m unittest discover -s tests   # 运行测试用例
```

## 当前进度与待办

里程碑总表在 `docs/dev-plan.md` §4。M4/M4.5/M5 已完成；**v4.0 规则重做已落地**（规格书 `docs/v4.0-plan.md`，里程碑 M5.1）：双杀手制（删帮凶/继任，4 平民 + 2 杀手）、职业 9→8、道具 14→12、反派密谋频道（每天两次：自由活动前先于停电声明轮、晚间休息选房前）、谋杀窗口每窗全队一次尝试 + 每日成功封顶一起、现场调查四项全给且全员公告 + 清晨调查队列（平民自动/反派篡改抢先）、延时装置手动化、密道改谋杀窗口动作、舞台魔术可防御、无杀倒计时从第 1 天起算。**v4.0 与 v3.x 存档不兼容：旧档仅供查看，不可续跑。**

当前回到**试玩调优期**（v4.0 规则面）：反复手动跑局（`python -m src.console --game game/<seed>`，必要时 `--resume` 断点续跑），重点观察 plan §11 的平衡风险点（杀手线索公开化后的反派胜率、双向 buff 净效果、未遂不当场封顶的防御道具贬值、技能杀利用率）。每局跑完后分析存档（`python -m src.view status --game ...` / `python -m tools.scorecard --game <seed>`），挑合格的完整局入 `selected_games/`。
