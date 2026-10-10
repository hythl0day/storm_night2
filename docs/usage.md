# 脚本使用说明（host/ 跑局工具）

本文档说明 `host/` 目录下 Python 脚本的使用方法。当前已完成 M0（配置 + 多协议接入层 + 连通性测试），后续里程碑（开局生成、引擎、编排器、CLI 等）落地后会在本文档追加对应用法，总体计划见 `v2/dev-plan.md`。

## 1. 环境准备

- Python 3.11+（依赖 `tomllib` 标准库，低于 3.11 无法运行）。
- 安装依赖（目前只有 httpx）：

```bash
pip install -r host/requirements.txt
```

## 2. 配置玩家席位

复制模板并填入你的接口信息：

```bash
cp host/players.example.toml host/players.toml
```

`players.toml` 已被 `.gitignore` 忽略，不会进版本库。

**安全约定：api_key 不落盘。** 配置里只填环境变量名（`api_key_env`），真实 key 在运行前注入环境变量。Git Bash 当前会话：

```bash
export RELAY_API_KEY_CN="sk-你的key"
```

长期使用可在 Windows「系统属性 → 环境变量」中设置，新开的终端即可读到。

每个席位需要确认/修改的字段：`model`（**以你的中转站/官方账户里的实际模型名为准**，模板里的型号是占位符）、`base_url`、`api_key_env`、`api_type`。

## 3. 运行连通性测试

```bash
python -m host.smoke                                     # 交互式：列出席位，手动选择（可追加真人席位）
python -m host.smoke --all                               # 全部席位，不询问
python -m host.smoke --random 6                          # 随机抽 6 席
python -m host.smoke --only 狄西,奇米                     # 按名字指定席位（须与 players.toml 的 name 一致）
python -m host.smoke --human 老陈 --random 5              # 追加真人席位，可与以上组合
python -m host.smoke --config host/players.example.toml  # 指定配置文件（模板无真实 key，用于验证报错路径）
python -m host.smoke --prompt "你为什么被困在古堡里？"    # 自定义提问
```

**席位选择**：不带 `--all/--random/--only` 时进入交互模式——先打印席位列表（含人设摘要），输入 `all` / `r N`（随机 N 席）/ 逗号分隔序号（如 `1,3,5`）；随后可输入一个名字追加真人席位（直接回车跳过）。真人席位不调用 API：脚本打印问题，由你在终端键入回答，同样计入汇总。

**人设卡**：每个角色的性别 / 体格 / 人种 / 性格人设写在 `host/personas.toml`（键名须与 `players.toml` 的 `name` 一致，未命中的席位按"普通人"处理）。人设只写性格不写职业（职业每局临时分配），且 prompt 中已声明"人设不影响推理能力"。性别/体格/人种是游戏机制属性（空手谋杀判定、犯罪侧写会用到）——规则书不含固定角色表，每局开局以此文件为准生成角色表并全员公布。

行为：所有 AI 席位**并发**收到同一个问题，按各自人设回答，最后输出汇总：

```
【狄西】(3.2s, 87 tokens)
  ……
== 汇总：8/8 席连通 ==
```

逐席显示耗时与 token 用量；失败的席位显示原因（如 `环境变量 XXX 未设置`、HTTP 错误）。

## 4. 配置字段参考

`[settings]` 段：

| 字段 | 说明 |
| ---- | ---- |
| `temperature` | 全席统一采样温度（公平性要求，不建议逐席改） |

`[[player]]` 段（每席一条，数量不限，当前模板 8 席）：

| 字段 | 必填 | 说明 |
| ---- | ---- | ---- |
| `name` | 是 | 角色名，用于日志与汇总显示 |
| `model` | 是 | 模型名，以渠道方实际提供的为准 |
| `base_url` | 是 | 接口地址，结尾 `/` 可有可无（程序会自动去掉） |
| `api_key_env` | 是 | 存放真实 key 的**环境变量名** |
| `api_type` | 否 | `openai`（默认，OpenAI 兼容 `/chat/completions`）或 `anthropic`（Claude 原生 `/v1/messages`） |

双协议说明：中转站、DeepSeek、Kimi 官方一般都提供 OpenAI 兼容接口，走 `openai`；Claude 官方或支持原生协议的中转站可走 `anthropic`（该协议会把 system 消息提升为顶层参数，`max_tokens` 固定 4096，token 用量归一化为 `total_tokens`）。两种协议可以按席位混用。

## 5. 常见问题

- **`环境变量 XXX 未设置`**：当前终端没读到 key。`export` 只对当前会话有效；检查变量名拼写是否与配置里的 `api_key_env` 完全一致。
- **HTTP 401/403**：key 无效或渠道不对（比如把中转站 key 配到了官方 base_url）。
- **HTTP 404 / 模型不存在**：`model` 字段与渠道方模型名不一致。
- **超时/偶发失败**：内置超时 120 秒、最多重试 2 次（间隔 2s/4s），仍失败会输出错误原因；中转站高峰期可稍候重跑。
- **Windows 中文乱码**：脚本已强制 UTF-8 输出；若仍乱码，确认终端为 Git Bash 或 Windows Terminal。

## 6. 模块现状

| 文件 | 状态 | 用途 |
| ---- | ---- | ---- |
| `host/providers.py` | ✅ 可用 | 多协议 API 客户端与配置加载（`load_config` / `chat`），被其他模块复用 |
| `host/personas.toml` | ✅ 可用 | 角色人设卡（性别/体格/人种/背景人设），smoke 与后续 prompt 模板共用 |
| `host/smoke.py` | ✅ 可用 | 连通性测试（席位选择 / 随机抽取 / 真人席位） |
| `host/deal.py` 等 | 待开发 | 开局生成器（M1）、引擎（M3）、编排器（M4）、CLI（M4.5 起）——见 `v2/dev-plan.md` |
