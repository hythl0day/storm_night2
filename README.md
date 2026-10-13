# 暴风夜之古堡惊魂

一个场景互动身份推理AI真人秀。玩家都是AI，但真人也可以参与，随机身份、技能和道具，破解谜题，活到最后！

A scenario-based interactive role-playing reality show featuring AI-powered deduction. Players are all AI, but real people can also participate. Players are randomly assigned identities, skills, and items to solve puzzles and survive to the end!

自动化跑局管线 + 节目素材生产。

## 常用命令

```bash
# 安装依赖（Python 3.11+，仅 httpx）
pip install -r src/requirements.txt

# 配置 API key（players.toml 里只存环境变量名；中转站四席各一个，其余共用 CN）
export RELAY_API_KEY_CN="sk-..."       # 另有 RELAY_API_KEY_GPT / _CLAUDE / _GEMINI / _GROK

# 连通性点名：全部席位各回一句话
python -m src.smoke --all

# 创建对局（同 seed 逐字节可复现；目录名即 seed 本身，方便手打）
python -m src.deal --seed 42           # 生成 game/42/

# 跑局（二选一），跑到对局结束；中断后加 --resume 按阶段续跑
python -m src.console --game game/42        # 交互驾驶舱：流式输出 + 随时命令（输入 help 查看）
python -m src.orchestrator --game game/42   # 无头模式（供批量跑局）

# 查看存档（只读，与跑局解耦）
python -m src.view status --game game/42    # 阶段/存活/台账；另有 log/peek/inner/host/follow

# 评分卡（M6）：把跑完整的存档挑入 selected_games/ 后扫描
python -m tools.scorecard                   # 多局报告：版本分组胜率/机制利用率/多样性/成本
python -m tools.scorecard --game 42         # 单局报告：台账/流程简报/戏剧性评分/主角弧线候选

# 测试（引擎 + 平衡回归 + 编排 mock 集成，140 用例）
python -m unittest discover -s tests
```

对局产物：`game/<id>/script.md` 是主持人侧全记录（含剧透，供整理剧本）；`public/` 为公开口径，`private/` 为各席私密素材（内心独白/笔记），剪辑时注意防剧透。

## 文档

- 脚本使用说明（环境准备、席位配置、连通性测试、跑局与查看器的完整用法）：[docs/usage.md](docs/usage.md)
- 规则裁决引擎文档：[docs/engine.md](docs/engine.md)
- 项目约定与当前进度（新 session 入口）：[AGENTS.md](AGENTS.md)
- 节目规划（两季 + 特别单集）：[screenwriting/show-bible.md](screenwriting/show-bible.md)
- 规则书：[docs/rules-6.md](docs/rules-6.md)（6 人局，v3）/ [docs/rules-8.md](docs/rules-8.md)（8 人局）
- 工程计划与里程碑：[docs/dev-plan.md](docs/dev-plan.md)
