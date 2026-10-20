# 引擎文档（src/engine/）

规则裁决引擎（M3），当前对应 `docs/rules-6.md` 的 **v4.0 双杀手规则**。它是纯逻辑库：不调用 AI、不做网络请求、不打印；输入是 `state` 字典与玩家声明的动作，输出是结构化事件。编排器负责收集 AI 输出、调用引擎、按公开/私密口径分发结果并落盘。

规则书是规则的唯一权威来源；本文只说明代码接口与实现边界。lite 规则集与完整规则集共用本引擎，区别仅在 AI 可见信息。v4.0 与 v3.x 存档不兼容，旧档仅供查看，不可续跑；`docs/rules-8.md` 的第二季主谋局不在本引擎当前支持范围内。

## 1. 模块划分

| 模块        | 职责                                                                                                             |
| ----------- | ---------------------------------------------------------------------------------------------------------------- |
| `state.py`  | 状态装载与幂等扩展（`ensure()`）、JSON 读写、房间/门锁/黑暗/监控/道具/技能等共享助手、通用胜负检查               |
| `core.py`   | `Engine` 公共门面、阶段流转、清晨公告、无杀倒计时与主神结局、清晨调查队列、昼夜统一开锁与进入结算                |
| `murder.py` | 普通谋杀与全部反派技能杀、谋杀窗口和每日成功封顶、自卫与未遂、案件/发现/缴获台账、延时装置与密道谋杀             |
| `vote.py`   | 投票与篡改投票、延迟出局、武装指认、侦探竞选与佩枪、道具及警徽遗嘱                                               |
| `ledger.py` | 开锁/保险箱/赠送/手电筒/停电与发电机、监控与远程入侵、公开调查与篡改、看破伪装、封闭空间、窃听器、桌上物品、报案 |

`src/engine/__init__.py` 只导出 `Engine`、`load_state`、`save_state`。

## 2. 事件契约

所有裁决方法返回统一结构：

```python
{
    "ok": bool,
    "type": str,
    "public": dict | None,
    "private": dict,
    # 还可带 case / positions / winner / win_reason 等事件特定字段
}
```

- `public`：可全员公开的信息，通常含 `announcements`，也可含 `monitoring`、`tally`、`out`、`identity` 等。
- `private`：`{玩家名: 仅本人可见的信息}`，由编排器按席位分发，不会自动公开。
- 非法动作返回 `ok=False, public=None`，约定不改变状态，且驳回事实不公开。
- `ok=True` 只表示动作合法并已裁决，不等于击杀成功：被防御的攻击返回 `type="murder_attempt", defended=True`；赠送也可能返回 `accepted=False`。

## 3. 公共 API（Engine 方法）

### 阶段与调查队列

- `advance()` — 推进阶段；处理翻日顺序轮转、缓释毒剂延迟死亡、清晨公告、第 4 天门锁、无杀倒计时和第 6 天终审。`engine.winner` 为 `None` / `"平民"` / `"反派"` / `"主神"`。
- `investigation_queue()` — 返回昨日已公告、尚未完成公开调查的案件及各案持有调查手段者，持有者按行动顺序排列并标注阵营，供编排器驱动清晨队列。

### 谋杀与反派技能

- `murder(killer, target, weapon=None, savior=None)` — 空手、持有凶器、桌上凶器或环境凶器谋杀。
- `pharmacology_kill(user, target, savior=None)` — 医师【药理学】毒杀。
- `strong_attack(user, target)` — 运动员【强袭】。
- `slow_poison(user, target)` — 化学家【缓释毒剂】；写入 `poisoned` 队列，当夜翻页时结算死亡。
- `passage_kill(user, target, weapon=None, savior=None)` — 建筑师【密道谋杀】；出口不含卧室，成功一次后密道损毁。
- `stage_magic(user, target, savior=None)` — 魔术师【舞台魔术】；可被救援系资源防御。
- `set_device(user, room)` — 机械师预设【延时装置】。
- `pending_devices()` — 查询当前满足启动条件、应推送给设置者的装置。
- `device_fire(user)` — 在谋杀窗口手动启动装置；设置者须存活，装置房须恰好只有一名非反派独处。

`savior` 是攻击裁决的低层救援参数；AI 在某个环节实际能声明哪些动作，始终以编排器当前菜单为准。

### 投票、侦探与遗嘱

- `resolve_vote(votes, implant=None, defer_out=False)` — 计票；侦探一票计 1.5，严格超过存活人数一半才出局。`implant` 是内部沿用的字段名，对应当前【篡改投票】；`defer_out=True` 时将出局者写入 `pending_out`，保留最后平民结算前指认窗口。
- `execute_pending_out()` — 指认窗口关闭后执行被延迟的出局。
- `armed_accusation(accuser, suspect, weapon=None)` — 武装指认；两名杀手均为正确目标，武器无论对错都会消耗。
- `elect_detective(ran, stay, votes)` — 第 1 天侦探竞选；处理密封退选、未参选者投票、平票流产与侦探佩枪。
- `bequest(owner, mapping, replies)` — 道具遗嘱；接收人可拒收，接受当天可用，未指定或拒收的道具放回会客室桌上。
- `bequest_badge(owner, to, accept)` — 侦探徽章传承；未指定或拒收则销毁，不补选。

### 开锁与统一进入

- `unlock_for_entry(user, room, assignments)` — 独立开锁轮：只消耗手段并开门，不移动玩家。
- `enter_rooms(assignments, invited=None, unlockers=None)` — 自由活动统一进入：开锁者优先，其余按行动顺序；处理容量、邀请、封闭空间与失败滞留会客室。
- `night_assign(assignments=None, invited=None, unlockers=None)` — 晚间归宿：合法去向含自己/他人卧室；处理夜锁、卧室容量、平民慎入、邀请与失败回自己卧室。
- `choose_room_one(name, room)` / `finish_room_entries()` — 自由活动逐人进入的低层入口与收尾，主要供既有流程和测试使用。

### 房间、道具与信息技能

- `unlock(user, room)` / `open_safe(user)` / `transfer(giver, receiver, item, accept=True)` / `use_flashlight(user)`。
- `declare_blackout(user)` / `fix_generator(user)` — 停电与恢复供电；停电持续到发电机修复，不自动跨日恢复。
- `view_monitoring(user)` — 在监控室查看监控。
- `remote_view_monitoring(user)` / `remote_tamper_monitoring(user, period)` — 【远程入侵】/「木马程序U盘」的远程查看与反派整时段抹除。
- `investigate(user, case_id)` — 一次取得死亡时间、尸体转移、武器伤痕、杀手线索四项结论，并公开调查者与实际消耗的调查手段；每案正常调查一次。
- `tamper_investigation(user, case_id)` — 反派消耗调查手段，随机篡改四项中的两项；同一案件只能篡改一次。毁证窗口只污染案件；清晨队列中则按正常调查的同口径公开假结论与所用手段，并将案件标记为已定型。
- `see_through(user, target, claims)` — 【看破伪装】/「测谎仪」；以真实监控验证职业、道具、技能、行踪声明，监控空白判无法验证。
- `seal_room(user, allowed=None)` / `place_bug(user, room)` / `bug_listen(user)` / `take_table_item(user, item)` / `report(user)`。

## 4. 状态结构

`Engine(state)` 构造时调用 `ensure()`，原地补齐扩展字段；`load_state()` / `save_state()` 是引擎仅有的 JSON 文件读写辅助。

主要字段按职责分组如下：

- 基础状态：`phase={day, stage}`、`order`、`players[]`、`winner`、`win_reason`。玩家含身份、职业、性别/体格/国籍、`alive` 与道具列表。
- 谋杀与案件：`cases`、`attempts`、`poisoned`、`last_murder_day`、`stage_murder_used`（本窗口尝试已用）、`day_kill_capped`（本日成功已封顶）。
- 房间与进入：`positions`、`locks`、`rooms_entered`、`night_assigned`、`bedroom_relock`、`sealed_room`、`flashlight_lit`。
- 监控与停电：`monitoring`（公开口径）、`monitoring_true`（宿主真实记录）、`power_out`、`blackout_days`、`blackout_periods`、`power_restored`、`blackout_news`、`selection_begun`。
- 技能与资源：`skill_uses`、`devices`、`passage`、`passage_stealth`、`bug_room`、`bug_placed`、`safe_open`、`table_items`、`table_events`、`sealed_items`。
- 投票与公开信息：`detective`、`detective_election_done`、`last_vote`、`pending_out`、`accusation_fired_day`、`profession_public`。

案件除死者、凶手、房间、时段和伤痕外，还记录 `clue_trait`（案发时锁定的杀手特征项）、`falsified`（被篡改的调查项及假值）、`investigated`（公开调查已完成）、`announced`、`discovered_by`、`reported_by`、道具封存与缴获信息。

玩家道具从 deal 的单件 `item` 扩展为：

```python
player["items"] = [{"name": "...", "uses_left": 1, "available_day": 1}]
```

`extra_items` 在 `ensure()` 时并入，用于杀手固定持有的万能钥匙和另一件反派道具。道具可因缴获、遗嘱、赠送和桌上取用流转；转手不重置次数。

## 5. 当前裁决边界

1. **双杀手**：`is_villain()` 只认身份为“杀手”的玩家。两人共享完整谋杀权；杀手出局不触发继任。
2. **谋杀频率**：进入自由活动或晚间休息时重置 `stage_murder_used`；任何已发动攻击都会占用本窗口尝试。成功击杀设置 `day_kill_capped`，当天后续窗口不再开放谋杀；未遂不封顶当天。下毒在下毒时即计入谋杀行为和每日成功封顶。
3. **死亡信息**：死亡与未遂不在发生时公开，统一随次日清晨公告；进入现场者可提前私下发现尸体并选择是否报案。清晨公告不直接给死亡时段。
4. **清晨调查**：编排器逐案按行动顺序处理持有者，公告包含调查者与实际消耗的手段。平民自动调查、不调用 API；未污染案件的反派私密选择正常调查、篡改并宣读或跳过。已污染案件不再提供反派菜单，任何身份的首位持有者都自动消耗并公布，提前篡改者也不例外。正常调查与清晨篡改都会标记 `investigated`，首个实际调查者定型。
5. **昼夜进入**：去向收齐后先独立开锁，再统一进入；开锁者优先，其余按行动顺序。卧室夜间黑暗，地下室永久黑暗，会客室永不黑暗；停电期间除会客室外全部黑暗，门锁失效且监控缺失。
6. **投票残局**：平民必须真正杀光或票光才会使反派获胜；两名杀手全部出局则平民胜。第 6 天终审投票后仍无阵营获胜，主神获胜。
7. **驳回与终局**：非法动作不公开、不改状态；一旦产生 `winner`，编排器应收束当前环节，不再进行无意义的 AI 调用。
8. **自卫与回执**：多种防御按武器自卫、急救箱、药用药剂、敏捷闪避、药理学的顺序触发；武器不消耗，其余按实际触发消耗。目标会私密获知实际防御来源，杀手与旁观者仍无法分辨。

## 6. 编排器侧职责（src/orchestrator.py）

引擎不负责 prompt、对话或存档协议。编排器主要负责：

- 按 `run_game()` 驱动开局、侦探竞选/公共发言、投票、自由活动、晚间休息和次日案件调查；第 6 天只运行白天会议。
- 每天组织两次反派密谋：自由活动前的停电声明轮之前，以及晚间休息选房之前。
- 生成阶段简报与合法动作菜单，解析 `speech / inner / notes / actions` 四通道 JSON；非法动作私下驳回并允许重报。
- 谋杀窗口按行动顺序询问杀手，仅放弃或无合法目标才顺延给同伙；每日成功封顶后跳过谋杀窗口。
- 收集去向与邀请、执行独立开锁和统一进入，随后依次组织手电筒、房间交谈、单个房间行动、谋杀窗口和毁证窗口。
- 处理侦探竞选、公共发言、投票、武装指认、遗言、死者字条、道具与警徽遗嘱。
- 将公开事件写入 `orch.feed`，私密事件写入 `orch.hist`；同步维护 `public/`、`private/`、`inbox/`、`outbox/`、`script.md` 和 `host_log.jsonl`。
- 用 `orch.wip` 记录“天：阶段：子环节”的断点，在席位行动完成后落盘；API 失败、暂停和 Ctrl+C 都从断点续跑。

## 7. 测试

```bash
python -m unittest discover -s tests -v
```

引擎与编排测试覆盖：双杀手谋杀权、窗口尝试与每日封顶、自卫矩阵、毒剂/强袭/药理学/手动装置/密道/舞台魔术、投票与武装指认、侦探与遗嘱、昼夜进入、黑暗与停电、监控抹除、调查队列与篡改、清晨公告、发现与报案、信息隔离、驳回重报和断点续跑；`test_balance.py` 另覆盖 deal 道具池、seed 确定性与典型平衡场景。
