# 交接文档（新 session 从这里开始）

> 写给下一个接手开发的 session/人：项目全貌读 `AGENTS.md`，工程计划读 `rules/dev-plan.md`，节目规划读 `rules/show-bible.md`。本文档只讲三件事：**现在到哪了、下一步干什么、哪些事还没拍板**。

## 1. 当前进度（2026-10 时点）

| 里程碑 | 状态 | 说明 |
| ---- | ---- | ---- |
| M0 脚手架 | ✅ 完成并实测 | providers 双协议 + players.toml 八席 + smoke/ping；八席连通 8/8 |
| M1 开局生成器 | ✅ 完成并实测 | `deal.py`：对称选席/身份/职业/道具/保险箱/行动顺序；同 seed 逐字节复现；分配约束生成即校验 |
| M2 providers 适配 | ✅ 被 M0 覆盖 | 跳过 |
| M3 引擎核心 | ✅ 完成，测试 66 绿 | `src/engine/` 五模块；单测覆盖 §7 全部分支 |
| **M4 编排器** | ⚠️ **代码已写完，未实测** | `src/orchestrator.py` + `prompt/game_system.md` 存在；因 API 速度慢被打断，**第 1 天全天全流程与 `--resume` 均未实测跑通** |
| M4.5 CLI 查看器 | 未开始 | `status` / `log` / `peek` / `follow` |
| M5 完整对局 | 未开始 | 含两个平衡回归场景（平民扎堆、卧室蹲守） |
| M6 批量跑局 | 未开始 | |
| M7 人机模式 | 未开始 | smoke 的 `--human` 是最小雏形 |
| M8 看海模式 | 未开始 | |

## 2. 下一步（按序）

1. 跑通 M4：`python -m src.deal --seed 12345` 生成新局 → `python -m src.orchestrator --game game/012345`，确认第 1 天全天无人工干预跑完、落盘齐全（public/log.md、private/<id>/inner.md 与 notes.md、inbox/outbox、host_log.jsonl）、state.phase 进入第 2 天；中途 kill 一次再 `--resume` 验证断点续跑。
2. M4 验收后进 M4.5 CLI 查看器。
3. M5 完整对局。

## 3. 待拍板事项（用户决定）

1. **自卫武器是否消耗次数**：当前实现不消耗（自卫是被动触发）。要改只需翻 `src/engine/murder.py` 的 `_defense` + 对应测试。
2. **延时装置的"拆除"**：规则书提到"被拆除"但从未定义动作，引擎未实现。需要补规则才实现。
3. **第一季 6 席名单**：阵容池 8 席每局随机选 6（中美 3+3 对称）——狄西是预定主角、奇米是"千问消失"剧情关键席，这两席建议锁定；是否再锁其他席位？
4. 主神呈现方式（形象/配音/台词人格，建议本人声音）；轮回记忆泄漏节奏；播出平台与单集时长。（详见 show-bible §6）

## 4. 环境/基建备忘

- 需要 5 个环境变量：`RELAY_API_KEY_GPT` / `RELAY_API_KEY_CLAUDE` / `RELAY_API_KEY_GEMINI` / `RELAY_API_KEY_GROK` / `RELAY_API_KEY_CN`。
- 已知渠道坑：apinebula 直连 IPv4 被 DNS 污染，需 IPv6 或代理；其 Claude 渠道不支持自定义 temperature（故全局不传）；Claude 走 openai 兼容格式（`api_type = "openai"`）。
- `src/players.toml` 已入库（无密钥）；`game/` 被 gitignore；`.agents/`（本机 AI 技能）被 gitignore。
- 新机器自检：`pip install -r src/requirements.txt` → `python -m unittest discover -s tests` → `python -m src.smoke --all`。

## 5. 未提交的遗留

无。本次提交已包含全部工作。如之后看到 game/000042 不在仓库里，属正常（gitignore），`python -m src.deal --seed 42` 可重新生成。
