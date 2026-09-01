# Checker 全流程验证重构（计划驱动检查点 + 乐观异步 + 出口结算）

> 交接文档：本计划自包含，执行者不需要任何前置对话上下文。
> 日期：2026-08-30（v2.1 实施版）。文中行号以当日工作区为准，**执行前一律先按内容搜索确认，行号可能漂移**。

---

## 0. 工作区纪律（先读）

- 本工作区可能有**多个并发 Claude 会话**在改代码，工作树里有大量未提交改动（部分不是你产生的）。
  - **严禁 `git stash`**（会卷走别人的半成品）。
  - 需要对比基线用 `git show HEAD:<path>` 或临时 worktree，不要动工作区里已有的未提交修改。
- 不要 commit/push，除非用户明确要求。
- 每个 Work Package 完成后跑对应的单测（见 §8），基础设施类测试可用 `ARTEMIS_FAKE_LLM=1` 环境变量走假 LLM。

---

## 1. 背景与结论（已定，不要重新设计）

### 1.1 旧中途 Checker 的病根（仍然成立，全部要摘除）

1. **Checker 失败的 turn 不写入 DataEngine 历史**——`graph.py` `execution_check_node` 里 `if checker_success:` 才 `record_step`。
2. **notes 目录全量快照回滚**——Operator 每轮前全量快照，checker 失败整体恢复。
3. **Prompt 整体切换**——存在 checker feedback 时换 `troubleshooter_template` + verification chat 多轮辩论。
4. **僵尸 checker**——`ctx.checker_task` 单槽位覆盖不 cancel，副作用在任务体内。

**关键认知：病根是"回滚过去、切换模板、副作用失控"，不是异步验证本身。**

### 1.2 目标架构与能力边界

系统承担**测试任务**，短暂状态只能在发生时刻附近取证，所以中途检查点必须存在，以**乐观异步**方式运行。架构分四层：

- **配置层**：`中途检查` 与 `结尾终审` 两个独立开关（§6）。用户关闭的检查一律尊重；**未执行的断言报告为 unchecked，绝不计为通过**。
- **计划层**：Planner 把检查项声明进 task_plan.md（`- verify:` / `- assert:`，带时机，§4）。计划文件是唯一权威载体（锚定子目标、随重写存活、受守护覆盖）；但**相关 prompt 注入全部条件化**（§4.4）——检查全关时 Planner 不生成 check 行、语法说明不进任何 prompt，零上下文污染。
- **能力层**：Checker 享有与 Operator 等价的观察能力（历史、截图、当前屏幕），但工具权限独立——只读检索 + 受约束探测，零副作用，只出裁决（§5.4）。
- **控制层**：Graph 识别检查点、spawn 任务、收割裁决、执行副作用、出口结算（§5.1-5.3）。

**能力边界（必须如实声明，写进 planner prompt 与最终报告语义）**：
- 本期的中途检查是**事后核验**：它能在事后发现流程违规并留下证据，**不能阻止**违规发生。"必须确认 A 之后才允许执行 B"这类**强制前置关卡本期不支持**——Planner 遇到此类用户要求时不得静默接受：断言照常生成（保留用户原语义），但计划与报告中标注"顺序核验为事后判定"。
- 异步检查只能核验**已被采集的证据**。从未被记录的瞬时提示无法找回——瞬态断言的证据来源是执行历史中的前后截图；Planner 应优先把预期表述为可持久探测的状态。

**三条铁律**：
1. Operator 的历史与上下文 **append-only**——不换 prompt、不回滚 notes、不丢 step、不开平行对话。
2. 验证影响未来（打回状态、注入 finding），**不修改过去**。
3. **放行、裁决、任务收尾三者分离**：fail-open 只影响放行决策，裁决原值（inconclusive）永远入账；assert 失败不修复但必须产生机器可读的测试失败结果并传导到 SDK 收尾（§5.5）。

**裁决 append-only**：每次检查有独立 `attempt_id`，所有裁决（含被 supersede 的）永久入账；断言首次失败不被覆盖；报告呈现每条断言 passed / failed / inconclusive / unchecked 与完整裁决序列。

### 1.3 横切原则：Prompt 与工具按配置装配（No Dead Instructions）

本次全部改动遵守同一条装配纪律，**任何 agent 的 prompt 段与工具注册都是"配置 × 当前场景"的函数**：

1. 配置关闭的能力，不得在任何 prompt 中被提及，其工具不得注册——不存在"指令还在、机制已关"的死指令；
2. 反向同样成立：prompt 中出现的每条规则，必须对应一个当前激活的机制（如"删除会被恢复"只在守护生效时可说）；
3. 装配点集中在代码里的组件列表/工具注册表（PromptComponent 列表、工具 dict 构造函数），**不在 prompt 模板内部堆条件文本**；
4. 场景也参与装配：同一 agent 的不同入口（如 checker 的中途/终审）按各自的证据纪律装配不同的工具与指南段。

落点：Planner（§4.1/§4.2 条件语法段与生成指令）、Operator（§4.4 说明段、§3.1 feedback 组件按内容渲染）、Checker（§5.4 两入口装配表）、SDK 返回结构（§5.5 test summary 仅在存在 check 项时出现）。

---

## 2. 代码地图（改动涉及的锚点）

Graph 拓扑（`artemis/graph/graph.py` `get_graph`，~622）：

```
START → planner → convergence
perception → operator → execution_check ─(execute_decisions)→ validator → summarizer → convergence
                                        └(review_subgoals)→ convergence
convergence ─(continue)→ perception
            └(end)→ END
```

注意时序：Operator 在自己的 turn 内通过 note 工具写计划（含完成标记）；该 turn 的设备动作在**其后的 validator 节点**才执行；`execution_check_node` 的 `record_step` 记录的是本 turn 的观察（pre 截图）与决策。因此"子目标完成"的证据 = 截至本 turn step 的历史（上一 turn 的 post 截图 + 本 turn 的 pre 截图）。

| 锚点 | 位置（按内容搜索确认） | 处置 |
|---|---|---|
| `execution_check_node` | `graph.py` ~88-289 | 重构（§3.2、§5.1、§5.2） |
| checker 中途触发 ①（chat_path 分支） | ~100-132 | 删除 |
| checker 单槽消费段 | ~134-153 | 删除 |
| planner validation 消费段 + 拒绝提前 return | ~155-211，**return 在 ~190** | 保留机制；拒绝分支补 record_step（§3.2） |
| record_step（`if checker_success:` 门控） | ~213-250 | 无条件化（§3.2） |
| notes 快照 commit/rollback 段 | ~252-280 | 删除 |
| checker 中途触发 ②（`new_completions`） | `_process_plan_write` 末段 ~499-506 | 改为排队 pending checkpoint（§5.1） |
| planner validation 漂移检测 | `_process_plan_write` `milestones_changed`（~452） | 保留；旁加确定性 check 行守护（§4.3） |
| `convergence_gate` | ~625 | 扩为三值路由（§5.3） |
| notes 快照创建 / troubleshooter 分支 / verification chat 机制 | `operator.py` | 删除（§3） |
| `CheckerFeedbackPromptComponent` | `operator/prompts.py` ~231 | 改写为读 state 的 append 注入组件 |
| checker 本体 | `agents/checker/checker.py` `run_async_check`（~80）、`CheckerResult`（~68） | 重写（§5.4） |
| context 字段 | `context.py`：`checker_task`、`disable_checker=True`（~108）、`checker_max_chat_rounds`（~110）、`disable_planner_validation=True`（~111，**注意默认关**）、`task_plan_snapshot` | §3、§6 |
| state 字段 | `graph/state.py`：`checker_success`、`initial_goal` | 保留；新增字段见 §5.2/§5.5 |
| plan 语法 | `utils/plan_grammar.py`：`CHECKBOX_LINE_RE`（~49）、`parse_plan`（~149）、`PlanSnapshot`（~100）、`subgoal_hash`（~56）、`milestones_changed`（~167）、`new_top_level_completions`（~172）、`PLAN_GRAMMAR_SPEC`（~199） | 扩展（§4.1） |
| DataEngine | `engine.py`：`record_step`（~491，存 pre/post 截图）、`get_step_number`（~922）、`get_agent_friendly_steps`（~1045）、`end_session(status)`（~300）；`storage.py` `get_steps`（~651） | 只加只读检索辅助 + 收尾状态核对（§5.5） |
| SDK 收尾 | `sdk/agent.py`：图完成后无条件 success 日志 + `end_session("completed")`（~718-721）、task.status 收敛（~1035-1040）、trace 命名 `_PASS/_FAIL`（~1061） | 接入 run outcome（§5.5） |
| verification 工具函数 | `utils/verification.py` | 删除 |
| 快照工具函数 | `utils/file.py` `create_snapshot`/`restore_snapshot` | 删前 grep 确认无其他调用方 |
| SDK 配置透传 | `sdk/types/agent.py`、`sdk/builders/agent_config_builder.py`、`sdk/agent.py` | §6 |

---

## 3. WP1 — 摘除旧干扰源（先做）

### 3.1 Prompt 不再切换
- `operator.py` `_build_prompt`：删 `has_feedback` 分支，恒用 `main_template`。
- `CheckerFeedbackPromptComponent` 改写为 `CheckFeedbackPromptComponent`：读 state 的 `check_feedback`（§5.2 写入），无内容不渲染，加入恒定组件列表。
- 删 `_has_checker_feedback`、`_get_verification_chat_rounds`、`_get_reply_to_checker_tool` 及工具注册逻辑；`operator.json` `troubleshooter_template` 相关键清理（grep 确认无他处引用）。

### 3.2 每个 operator turn 都必须 record_step（含 planner 拒绝的 turn）
- 仅移除 `if checker_success:` 门控**不够**：planner 拒绝分支在 ~190 提前 `return`，同样跳过记录。
- 做法：把现有 record_step 代码抽成节点内局部函数 `_record_turn(extra_metadata: dict) -> str | None`：
  - 正常路径原位调用（extra 不变）；
  - planner 拒绝分支在 `return` 前调用，extra 加 `{"planner_rejected": True}`（该 turn 的 terminal actions 被拦截未执行，记录如实反映决策与拦截）。
- 保留"planner 拒绝的 turn 不清零 subagent 循环计数"语义（`checker_success` 今后仅由 planner 拒绝路径置 False，维持现有 `if checker_success: update["subagent_calls"] = []` 写法）。

### 3.3 删除 notes 快照/回滚
- `operator.py` `create_snapshot` 段、`ctx.task_plan_snapshot` 赋值：删。
- `execution_check_node` "Handle optimistic execution rollback/commit" 整段（~252-280）：删。
- `context.py` `task_plan_snapshot`：删。`utils/file.py` 快照函数 grep 无调用方后删。
- planner validation 拒绝时的 task_plan.md 单文件回滚（`ctx.task_plan_content_before`）**保留**。

### 3.4 删除单槽位与 chat 机制
- `execution_check_node`：删 chat_path 触发段与 `await ctx.checker_task` 消费段。
- `context.py`：删 `checker_task`、`checker_max_chat_rounds`。`planner_task` 保留。
- `_process_plan_write` `new_completions` 触发段：暂时整段删除（§5.1 重建）。
- `utils/verification.py`、`checker_status.json` 写入、相关 import：删。

WP1 完成后系统处于"无任何语义验证"的干净中间态，可独立运行与测试。

---

## 4. WP2 — 计划语法、Planner 与检查标准保护

### 4.1 plan grammar 扩展（`utils/plan_grammar.py`）
- 新语法：顶层子目标下缩进附属行，关键字带可选时机后缀：
  ```
  - [ ] 在时钟应用创建 7:30 AM 闹钟
    - verify: 闹钟列表应出现 7:30 AM 且开关为开
    - assert: 创建后应弹出"已设定闹钟"提示
  - [ ] …
  （任务级，可置于计划末尾游离段）
  - assert@end: 全程不应出现应用崩溃对话框
  ```
- 解析为：
  ```python
  @dataclass(frozen=True)
  class CheckItem:
      kind: Literal["verify", "assert"]
      when: Literal["on_complete", "at_end"]   # 无后缀=on_complete；@end=at_end
      text: str
      parent_key: str | None                    # 挂靠子目标的 key；任务级为 None
  ```
  `when` 语义：`on_complete` 在锚定子目标完成时刻用**当时证据**判定；`at_end` 只在出口用**最终状态**判定。**禁止**在出口用当前状态重判 `on_complete` 项，也禁止在中途提前执行 `at_end` 项。
- 新增 `CHECK_LINE_RE`；先确认 `parse_plan` 对非 checkbox 缩进行的容忍度（`CHECKBOX_LINE_RE` 只匹配 checkbox 行），确保 check 行**不影响** `milestones_changed`/`subgoal_hash`/`new_top_level_completions` 现有行为。
- `PlanSnapshot` 新增 `check_items_of(item)`、`task_level_check_items`、`all_check_items`；新增模块函数 `check_items_changed(before, after)`（比较 check 行多重集合）。
- **语法说明条件化**：`PLAN_GRAMMAR_SPEC` 从模块常量拆为"基础语法 + check 扩展"两段，新增 `render_plan_grammar_spec(include_checks: bool) -> str`。check 扩展段内容：两关键字语义（verify=完成度，失败触发修复；assert=测试断言，失败如实记录不修复）、`@end` 后缀、§1.2 能力边界声明（事后核验、不支持强制前置关卡、瞬态断言的证据约束）。三个现有注入点改用该函数：`planner.py` ~105/~178、`operator/prompts.py` ~206；`include_checks = not（两开关全关）`。全关时任何 prompt 里都不出现 check 语法。

### 4.2 Planner 生成检查项（`planner.json`）
- **生成指令是 planner prompt 的条件段**：检查项相关的全部指令（下述各条 + §4.1 的 check 语法扩展）只在 `include_checks` 为真时注入。两开关全关 → Planner 的 prompt 与产出和无此功能时完全一致，不生成任何 check 行。
- 用户目标里的显式预期 → `- assert:` 行，逐字保留用户语义；属于全程/最终状态的预期用 `@end`。
- `- verify:` 行**只为确需验收的关键节点**生成——判断标准：不可逆操作完成点、对最终目标有直接贡献的里程碑。**不是**每个子目标一条；克制是明确要求（成本与 check 项数量成正比，见 §6）。
- 不可验证的子目标不硬造断言；瞬态预期优先改写为可持久探测的状态断言。
- 用户提出"必须先确认 A 才能做 B"类强序要求时：断言照常生成，但按 §1.2 标注"事后核验"，不得静默承诺前置拦截。

### 4.3 检查标准保护（确定性守护，不依赖 LLM）
- **前提事实**：planner validation 默认关闭（`disable_planner_validation=True`），不能作为 check 行不可删的唯一保障。
- **确定性守护**（`_process_plan_write` 内，Operator 来源的写入）：写入后若 `check_items_changed(before, after)` 检出 check 行被删除或改写，Graph **把缺失的 check 行合并回写**进新计划（锚定其 `parent_key`；父子目标整个被删时，该行转为任务级 `@end` 项）。合并回写是纯文本操作、同步完成，不打断 Operator；Operator 新增 check 行允许保留。Planner 节点自身的计划写入不受此守护（Planner 有权修订检查标准）。
- planner validation **开启时**：check 行差异同样纳入其审计范围（prompt 增加"检查标准被弱化应拒绝"要点）。机制本体（CAS 回滚、ratchet baseline）不动。

### 4.4 Operator 可选说明段（`operator/prompts.py`）
- 新增 `CheckItemsExplainerPromptComponent`，加入 `main_template` 恒定组件列表，**仅当当前 task_plan.md 解析出 check 行时渲染**（以计划实际内容为准，不看开关——续跑的旧计划带 check 行时同样需要解释），否则输出空串。
- 内容（一段，短）：
  - `- verify:` 是该子目标的验收标准，执行时可以参考它确认做到位，但**判定由独立 Checker 执行**，不要自行宣布通过或替 checker 记录结论；
  - `- assert:` 是测试断言，**不需要为它做额外动作**，更不得为满足断言去构造/伪造状态——断言失败是合法的测试结果；
  - check 行**不可删除或改写**（删除会被系统自动恢复）；重写计划时原样保留，新增允许；
  - 正常按语法打完成标记即可，检查在后台异步进行，不阻塞执行。
- 这与 §4.1 注入 operator 的语法扩展分工：语法扩展讲"这些行是合法语法"，本组件讲"你该怎么对待它们"；前者受开关控制，后者受计划内容控制。

### 5.1 触发：排队与 spawn 分离（证据锚点修正）
- **`_process_plan_write` 只排队**：`new_top_level_completions` 检出新完成项后，对每个带 `on_complete` check 项的完成项，追加 `ctx.pending_checkpoints: list[PendingCheckpoint]`（字段：`checkpoint_id=subgoal_hash`、check 项、完成时计划全文、触发时间戳）。**不在此处 spawn**——此刻本 turn 的 step 尚未记录，`get_step_number` 读到的是上一轮，锚点会错位。
- **`execution_check_node` 在 record_step 之后 spawn**：对每个 pending checkpoint 生成 `attempt_id`（`{checkpoint_id}#{递增序号}`），构造证据锚：
  ```python
  @dataclass(frozen=True)
  class EvidenceAnchor:
      anchor_step_id: str      # 刚记录的本 turn step（含 pre 截图；上一 turn 的 post 截图已在库）
      trigger_ts: float
      plan_text: str           # 完成时刻的计划全文
  ```
  然后 `ctx.checkpoint_tasks[checkpoint_id] = (attempt_id, asyncio.create_task(run_checkpoint_check(...)))`。
- **supersede 不丢裁决**：spawn 前若同 `checkpoint_id` 已有旧条目——旧任务 `done()` → **先收割入账**（§5.2）再替换；仍在运行 → `cancel()` 并 `await`（吞 `CancelledError`，完成资源清理），账本记一条 `superseded` 记录（attempt_id、时间），再 spawn 新 attempt。
- 每个新完成项都触发（修复旧"只验 `new_completions[-1]`"bug）。中途开关关闭时不排队不 spawn（相关项出口按 §5.3 处理）。`user_stop_requested` 后不再 spawn 任何新检查。
- **并发上限**：`max_concurrent_checkpoints`（默认 3），超出的 pending 留在队列，下轮 execution_check 再 spawn。

### 5.2 收割（`execution_check_node` 内，非阻塞）
- 每轮遍历 `ctx.checkpoint_tasks`，**只收割 `task.done()` 的**（绝不 await 未完成的），收割后从 dict 移除：
  1. **入账**：所有裁决 append-only 写入 DataEngine 账本（session 元数据或专用 jsonl：attempt_id、checkpoint_id、item_text、kind、when、status、evidence、anchor_step_id、时间）。这是"首次失败永久保留"的唯一载体，任何后续结果只能追加。
  2. **状态副作用的适用性校验**：仅当该 attempt 仍是此 checkpoint 的**当前 attempt**、且锚定子目标在当前计划中文本未变时，才允许改执行状态；过期/失配的裁决只入账不作用。
  3. `verify` FAIL（适用时）→ 子目标 `[x]`→`[/]`（`subgoal_hash` 匹配，直接写文件不走 wrapped note tool）+ finding 追加进 state `check_feedback`；**每 checkpoint 修复配额 `checkpoint_max_repairs`（默认 2）**：超配额不再打回，裁决保持 failed，留待出口如实呈现。`user_stop_requested` 后不再打回。
  4. `assert` FAIL → 只入账，不打回不注入（可配 `assert_failure_policy: "continue" | "halt"`，halt=置停止标记走 §5.3 收尾）。
  5. cancelled 记 `superseded`；异常/超时：verify 记 `inconclusive` + warning（**裁决不改写为 passed**，仅放行决策按通过处理），assert 记 `inconclusive`。
- **单次检查超时**：`checkpoint_timeout`（默认 180s，spawn 时用 `asyncio.wait_for` 包裹）→ 超时按异常处理。
- 副作用只发生在收割点与出口结算，任务体内零副作用。

### 5.3 出口：结算与终审分离
- `convergence_gate` 扩为 `Literal["continue", "exit_settlement", "end"]`：
  - `ctx.data_engine` 为 None → `"end"`；
  - 任务到达终态（`all_top_level_done`，或 continuous+user_stop，或 halt 标记）→ 只要**存在任何 check 项/未决任务/账本记录，或结尾终审开启**，一律 `"exit_settlement"`；两者皆无才 `"end"`。
  - **空计划（`not has_top_level`）且结尾终审开启 → 仍走 `"exit_settlement"`**：没有计划项不等于无可验之物，终审对象是用户原始目标。
- 新增节点 `exit_settlement_node`，两阶段，**阶段一无条件执行，阶段二受结尾开关控制**：
  1. **结算（barrier）**：收齐 `checkpoint_tasks` 全部未决任务并按 §5.2 入账——这是全流程唯一有意的阻塞等待，带**加总超时** `settlement_timeout`（默认 120s）：超时 cancel 剩余任务、对应项记 `unchecked(timeout)`。已启动的检查必须在退出前有账，不论结尾开关。
  2. **终审（`disable_final_check=False` 时）**：运行 `run_final_check`，对照 **`state.initial_goal`（用户原始目标，不是计划）** + 全部 check 项：
     - `at_end` 项：用最终状态判定；
     - `on_complete` 项：**引用账本裁决**（当前 attempt 的 PASS/FAIL 直接采信，FAIL 过的 assert 保持 FAIL）；从未检查过的（中途关闭/配额限制），基于锚点附近的**历史证据**补判，证据不足记 `unchecked`——禁止用当前屏幕重判过程断言；
     - 终审自身超时/异常：fail-open 放行，账记 `inconclusive`。
  3. 路由：终审通过或关闭 → `END`；verify 类未达成（assert 失败**不算**）→ 打回 unmet 项 + `check_feedback` 注入 → `perception`；`final_check_attempts` 达上限（默认 3）或 user_stop/halt → `END`。
- 收尾状态见 §5.5；continuous+user_stop 时 FAIL 不回循环。

### 5.4 Checker 本体（`agents/checker/checker.py` 重写）
- 两个入口共用旧 `run_async_check` 的工具循环骨架（多轮循环、迭代上限 `checker_max_iterations`、结构化兜底，可整体沿用）：
  - `run_checkpoint_check(check_items, anchor: EvidenceAnchor, goal, ...)`
  - `run_final_check(goal, plan_text, ledger, ...)`
- **输入**：用户原始目标、计划、待验项、证据锚、`get_agent_friendly_steps()` 摘要历史。
- **工具表按入口 × 配置装配（§1.3；工具表是唯一权限来源，全部只读）**：

  | 工具 | `run_checkpoint_check` | `run_final_check` | 配置开关 |
  |---|---|---|---|
  | `get_step_detail(step_no)` / `get_step_screenshot(step_no, which: pre\|post)`（`storage.get_steps` + step 目录，新增只读辅助） | ✓ | ✓ | 恒定 |
  | `read_note`（只读） | ✓ | ✓ | 恒定 |
  | `probe_device(kind, params)`（固定探测类型 + 程序生成 argv，见下） | ✓ | ✓ | `disable_device_probes`（默认 False）；关闭则**不注册且 prompt 零提及** |
  | 实时屏幕读取（`UnifiedMobileController.get_screen_data()` + OCR，旧 checker 有现成代码） | **✗ 不装配** | ✓ | 恒定（仅 final 入口） |

  中途检查点**不装配实时屏幕**是刻意的证据纪律：检查点的证据是锚点历史与持久状态探测，当前屏幕早已随 Operator 前进，装配它只会诱导用错证据——纪律靠工具表落实，不靠 prompt 劝告。
  **任何入口都不注册**：设备 action、write_note、子代理。
- **`checker.json` 拆为组件段，按待验内容与入口装配**（不在单一模板里堆条件文本）：

  | prompt 段 | 装配条件 |
  |---|---|
  | 基础裁决规则（逐字引用、证据具体化、inconclusive 语义） | 恒定 |
  | verify 语义段（完成度判定、fail-open 放行规则、suggestion 要求） | 待验项含 verify |
  | assert 语义段（测试断言、失败即结果、证据缺失记 inconclusive 不记 failed） | 待验项含 assert |
  | 锚点取证指南（瞬态/顺序断言必须查锚点附近历史） | `run_checkpoint_check` 入口 |
  | 终审指南（对照原始目标、引用账本、at_end 用最终状态、禁止重判过程断言） | `run_final_check` 入口 |
  | probe 使用指南（持久状态优先探测、探测类型速查） | `probe_device` 已注册 |
- **`probe_device` 设计（不做前缀白名单）**：`dumpsys` 等含状态修改子命令（如 `dumpsys battery set level 100` 可改电量模拟），且前缀判断挡不住参数拼接。改为枚举探测表，argv 列表由代码构造、不经 shell 字符串拼接，参数正则校验（拒绝空白符与 shell 元字符）：
  ```python
  PROBES: dict[str, ProbeSpec] = {
      "alarms":       argv=["dumpsys", "alarm"],                    # 无参数
      "battery":      argv=["dumpsys", "battery"],                  # 无参数（禁 set/unplug 等子命令）
      "foreground":   argv=["dumpsys", "activity", "activities"],
      "notifications":argv=["dumpsys", "notification"],
      "setting":      argv=["settings", "get", <ns>, <key>],        # ns∈{system,secure,global}；key: ^[A-Za-z0-9._-]+$
      "content":      argv=["content", "query", "--uri", <uri>],    # uri: ^content://[A-Za-z0-9./_-]+$
      "packages":     argv=["pm", "list", "packages"],
      "prop":         argv=["getprop", <key>],                      # key: ^[A-Za-z0-9._-]+$
  }
  ```
  未列举的探测类型一律拒绝；扩表需改代码，不接受运行时自由命令。底层复用 `run_adb_command` 的执行通道但以 argv 形式传递。
- **输出**（structured output）：
  ```python
  class CheckVerdict(BaseModel):
      item_text: str                                   # check 行原文，逐字
      kind: Literal["verify", "assert"]
      status: Literal["passed", "failed", "inconclusive"]
      evidence: str                                    # 具体观察，禁止空泛
      suggestion: str                                  # 仅 verify failed 需要

  class CheckReport(BaseModel):
      verdicts: list[CheckVerdict]
      unmet_subgoals: list[str]                        # 仅 final check：逐字引用计划项
  ```
  放行决策（passed 布尔）由 **node 侧**从 verdicts 计算（verify 全 passed/inconclusive → 放行），不由模型直接输出——保证"放行≠裁决改值"。
- **各段共同要点**：failed 必须附具体证据（探测输出或截图元素），空泛否决 node 侧降级 inconclusive；证据从未被采集时输出 inconclusive 并说明"证据缺失"而非 failed；引用计划项逐字。

### 5.5 任务收尾与 SDK 传导（放行 / 测试结果 / 收尾状态三轴分离）
- Graph 在 END 前汇总 **run outcome**（写入 state 新字段 `run_outcome` 与 DataEngine session 元数据）：
  ```python
  class RunOutcome(BaseModel):
      task_status: Literal["completed", "partial", "blocked"]   # 目标达成轴
      tests: TestSummary                                        # passed/failed/inconclusive/unchecked 计数 + failed 明细
  ```
  - verify 未达成且预算耗尽 → `blocked`（或 `partial`）；
  - **assert 有任何 FAIL → `tests.failed > 0`，即使 `task_status="completed"`**——任务做完与测试通过是两回事。
- **SDK 链路必改**（`sdk/agent.py`）：
  - 图完成处（~718-721）：不再无条件打 success 日志 / `end_session("completed")`——读 `run_outcome`：`blocked` → `end_session("failed")`（确认 `data_engine.end_session` 支持的状态集，必要时扩）；`completed` 但 `tests.failed>0` → `end_session("completed")` 且 session 元数据带 test summary，日志明确打出"任务完成、N 条断言失败"；
  - task.status 收敛处（~1035-1040）与 trace 命名处（~1061 `_PASS/_FAIL`）：断言失败的运行 trace 命名用 `_TESTFAIL`（或并入 `_FAIL`，二选一但必须与 `_PASS` 区分）；
  - `mobile_run_task`/`mobile_manage_task` 返回结构透出 test summary，调用方无需翻报告正文即可拿到机器可读结果。
- Outputter 报告呈现完整裁决序列与三态统计；BLOCKED/partial 收尾时最后一次 finding 入元数据。

---

## 6. WP5 — 配置与 SDK 透传

- `ExecutionSetup`（`context.py`）：
  - `disable_midway_checks: bool = False`、`disable_final_check: bool = False`（默认开。成本与计划中 check 项数量成正比——§4.2 已要求 Planner 只在关键节点生成 verify，这是成本上界的来源，**不存在"零成本"**）；
  - `disable_checker` 保留为兼容别名：True 时两者同关；
  - `final_check_max_attempts: int = 3`、`checkpoint_max_repairs: int = 2`、`max_concurrent_checkpoints: int = 3`、`checkpoint_timeout: float = 180.0`、`settlement_timeout: float = 120.0`、`assert_failure_policy: Literal["continue","halt"] = "continue"`、`disable_device_probes: bool = False`；
  - `disable_planner_validation`、`checker_max_iterations` 语义不变。
- SDK 透传链：`sdk/types/agent.py`、`sdk/builders/agent_config_builder.py`（`with_disable_checker` 保留 + 新开关映射）、`sdk/agent.py`。
- 四组合行为：
  | 中途 | 结尾终审 | 行为 |
  |---|---|---|
  | 关 | 关 | 不 spawn、不终审；**已启动的任务仍出口结算**（本组合下无任务）；全部 check 项报 unchecked |
  | 关 | 开 | 出口结算（空）+ 终审：`at_end` 判最终状态，`on_complete` 凭历史证据补判，不足记 unchecked |
  | 开 | 关 | 检查点照跑；出口**只结算不终审**（阶段一无条件），账本与 test summary 照常产出 |
  | 开 | 开 | 检查点 + 结算 + 终审 |

---

## 7. 明确不做（Out of scope，勿顺手扩大）

- **强制前置关卡**（"确认 A 后才允许执行 B"的中途阻塞等待）——本期只提供事后核验；出口 barrier 保证的是**退出前完成裁决结算**，不保证执行顺序约束。此边界必须如实声明（§1.2），不得静默接受此类需求。
- "动作前"检查时机（前置状态是 Operator 感知职责）。
- 逐动作依赖分析。
- 多验证器投票、版本化 VerificationKey、shadow 遥测。
- Validator 拆分、统一 FailureEvent/RecoveryCoordinator、`_check_infinite_loop` 降级。

---

## 8. 测试

现有测试（先跑通再动手，改完逐个修）：
- `tests/unit/graph/test_graph.py` —— execution_check_node / convergence_gate 断言大改。
- `tests/unit/agents/test_plan_mutation_guardrails.py` —— planner validation 主体不动；补 check 行确定性守护用例（§4.3）。
- `tests/unit/utils/test_plan_grammar.py` —— verify/assert/@end 解析、不影响 milestone hash。
- 全仓 grep `verification_chat`、`checker_status`、`task_plan_snapshot`、`create_snapshot`、`troubleshooter`、`ctx.checker_task`、`checker_max_chat_rounds`：零残留。

新增（建议 `tests/unit/graph/test_checkpoints.py` + `test_exit_settlement.py`）：
1. 排队/spawn 分离：`_process_plan_write` 只排队；execution_check 在 record_step 后 spawn，anchor_step_id 等于本 turn 刚记录的 step。
2. 多个同轮完成项全部排队；无 check 项不排队；中途开关关不排队；user_stop 后不 spawn。
3. supersede：旧任务 done 未收割 → 先入账再替换（**失败裁决不丢**）；运行中 → cancel 且 await 完成，账本有 superseded 记录。
4. 收割只取 done；适用性校验：过期 attempt / 子目标文本已变 → 只入账不打回。
5. verify FAIL 打回 + 注入；超 `checkpoint_max_repairs` 后不再打回、裁决保持 failed。
6. assert FAIL 只入账；`halt` 策略触发收尾；账本 append-only（先 FAIL 后 PASS 两条都在）。
7. 超时：单任务超 `checkpoint_timeout` → inconclusive；结算超 `settlement_timeout` → 剩余 cancel + unchecked(timeout)。
8. 出口路由：结算与终审解耦——终审关、有未决任务 → 仍进 exit_settlement 且只走阶段一；空计划 + 终审开 → 进终审；全关全无 → 直接 end。
9. `when` 语义：`at_end` 不在中途执行；`on_complete` 在终审引用账本、无账本凭历史补判、证据不足 unchecked（构造"当前状态已满足但历史证据缺失"的用例，断言**不**判 passed）。
10. record_step 无条件：正常 turn 与 planner 拒绝 turn 都有记录，后者带 `planner_rejected` 标记。
11. check 行守护：Operator 写入删除 check 行 → 合并回写；父目标被删 → 转任务级 `@end`；Planner 写入可修订；planner validation 关闭时守护独立生效。
12. `probe_device`：枚举表内放行、表外拒绝、参数带空白/元字符拒绝、`battery set` 类子命令无法表达。
13. fail-open 三轴分离：verify 异常 → 放行但账记 inconclusive（**不是 passed**）。
14. run outcome / SDK：assert 失败 → `tests.failed>0`、trace 名非 `_PASS`、`end_session` 元数据含 summary；verify 预算耗尽 → `blocked` + `end_session("failed")`。
15. continuous+user_stop+FAIL → 不回循环。
16. 四开关组合矩阵（§6 表逐行）。
17. prompt 条件化：两开关全关 → `render_plan_grammar_spec` 输出不含 check 语法、planner prompt 无生成指令；`CheckItemsExplainerPromptComponent` 仅在计划含 check 行时渲染（含"开关关但旧计划带 check 行仍渲染"的用例）。
18. checker 装配：`run_checkpoint_check` 的工具表**无实时屏幕**、`run_final_check` 有；`disable_device_probes=True` → `probe_device` 未注册且 prompt 无探测指南；待验项全 verify → prompt 无 assert 段（反之亦然）；checkpoint 入口无终审指南段。

运行：`python -m pytest tests/unit/graph tests/unit/agents tests/unit/utils -x`（需要假 LLM 设 `ARTEMIS_FAKE_LLM=1`）。

---

## 9. 验收标准

1. Operator 的 prompt 模板、notes、DataEngine 历史在任何验证结果下不被回滚/切换/丢弃；**每个 operator turn（含 planner 拒绝的）都有 step 记录**；中途路径上不存在对未完成验证任务的等待。
2. verification chat、`checker_status.json`、notes 全量快照、单槽 `ctx.checker_task` 从代码库消失。
3. 检查项由 Planner 声明（verify/assert × on_complete/at_end），Operator 删除/弱化 check 行会被**确定性守护**恢复，不依赖 planner validation 开关。
3a. **§1.3 装配纪律全局成立**：检查全关时 planner/operator prompt 与无此功能时一致（零污染）；计划含 check 行时 Operator 收到行为说明段；checker 的 prompt 段与工具表按入口 × 配置装配（checkpoint 入口无实时屏幕、probe 关闭则工具不注册且 prompt 零提及、无 assert 项则无 assert 段）；不存在指向已关闭机制的死指令。
4. 检查点在 record_step 之后 spawn，证据锚指向正确的 step；裁决全部 append-only 入账，supersede 不丢已完成裁决；检查有单次超时、并发上限、修复配额。
5. 出口结算无条件执行（含加总超时与 unchecked 标记），终审受结尾开关控制；空计划不绕过已开启的终审；终审对照用户原始目标；`at_end`/`on_complete` 判定时机不错位。
6. 放行、裁决、收尾三轴分离：fail-open 不改写裁决值；assert 失败产生机器可读 test summary 并传导至 `sdk/agent.py` 收尾（日志、end_session、trace 命名、返回结构）。
7. `probe_device` 为枚举探测 + 程序生成 argv，无自由命令通道。
8. 计划/报告如实声明"事后核验"边界，强序需求不被静默接受。
9. planner validation（入口卡点）除新增 check 行审计外与现状一致。
10. §8 测试全绿。

---

## 10. 建议执行顺序

WP1（3.2 → 3.3 → 3.1 → 3.4，摘干扰源，得到干净中间态）→ WP2（§4 语法 + Planner + 守护，独立可测）→ §5.4 checker 本体与 probe 表（可单测）→ §5.1 触发 → §5.2 收割 → §5.3 出口 → §5.5 收尾传导 → WP5 配置透传 → 全量测试。

每步完成后系统都应可运行：WP1 后=无验证；WP2 后=计划里有 check 项但无人消费（守护已生效，无害）；§5.4 后=checker 可单测；接线后=全功能。
