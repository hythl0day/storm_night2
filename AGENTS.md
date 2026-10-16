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
- 角色的性别/体格/国籍只写在 `personas.toml`（项目根）；规则书不含固定角色表（开局由 deal.py 生成并全员公布）；**体格数值绝不注入 prompt**——注入时转成文字（魁梧/健壮/普通/瘦弱），prompt 里禁止角色说数值。
- 人设只写性格底色（失忆设定，无传记无职业）；prompt 要求"靠言行流露、禁止自我介绍性格、人设不影响推理"。
- 随机选席为**中美 3+3 对称**（两家 API 渠道均衡）；手动 `--seats` 不对称仅提示。
- deal.py 的随机调用顺序固定（见其 docstring），改顺序 = 改变 seed 语义。
- 文件协议：`game/<id>/` 下 `seed.txt / state.json / rules.md / players.toml / script.md（主持人侧全记录，含剧透，不含内心独白）/ public/ / private|inbox|outbox/<席位id>/`。

## 测试与自检

```bash
python -m src.smoke --all              # 八席连通点名（需 RELAY_API_KEY_* 环境变量）
python -m src.deal --seed 42           # 生成样例局 game/42/（目录名即 seed）
python -m unittest discover -s tests   # 222 测试用例
```

## 当前进度与待办

里程碑总表在 `docs/dev-plan.md` §4。M4/M4.5/M5 已完成；M8（watch 看海模式）已取消——console 驾驶舱跑局时即实时观战，且暂不做多局并发（API 稳定性）。

当前处于**试玩调优期**：反复手动跑局（`python -m src.console --game game/<seed>`，必要时 `--resume` 断点续跑），边跑边检查 bug、修订规则（规则书 `docs/rules-6.md`，引擎 `src/engine/` 同步改）。每局跑完后分析存档（`python -m src.view status --game ...` / `python -m tools.scorecard --game <seed>`），挑合格的完整局入 `selected_games/`。
