# ARTEMIS 上下文与记忆重构：显式状态、按 agent 可见域、统一记忆内核

状态：提案 v2（2026-08-31，经"不损伤"审计细化，待终审）

> **2026-08-31 更新**：Phase 0/1 已实施；Phase 4（§6 方案 D 记忆内核）由
> `docs/plans/history-module-redesign.md`（统一方案 v3）取代并修订——其中
> "两 lens 按 profile 分立"修订为"步级视觉 lens + 段级胶囊 lens 按高度分立"
> （该修订已于 2026-08-31 获用户拍板采纳），
> StepMemoryService 运行时与 ContextPolicy 策略表保留。Phase 2/3 不受影响。
>
> **最终状态（2026-09-01）**：统一方案 v3 已全部实施（M0-M5，worktree
> `zealous-gates-4626b1`，未提交）——StepMemoryService/ContextPolicy 均已按
> 该方案落地；附录 B 中"保留、移除权属姊妹文档 §3.3"的 `short_term_memory`
> 台账行已在 M5 核销移除（全部消费者先核销后删，台账纪律照本文档执行）。
> 各里程碑实施标注见 history-module-redesign.md §7。

范围：State / ArtemisContext 的类型化与生命周期分域；感知数据通道；Flash 压缩机制上提为通用记忆内核。

底线与目标：**不损伤为最低要求**——每一处删除/搬迁都在附录 B 台账中核销其全部消费者，
任何信息流不得静默中断；**优雅处理所有信息为目标**——每份数据有唯一属主、显式通道、
声明的可见域，重复实现收敛为单一入口。

姊妹文档：`docs/design/pro-context-memory-redesign.md`（2026-08-25）深度覆盖了 Pro 侧
"压缩什么、怎么压"（视觉摘要、历史分块、召回工具、token 预算）。本提案覆盖它没有处理的
另一半——**数据放在哪、谁能看见什么、通道是否显式**——并在 §6 把两份文档的交叠处收敛为
同一个记忆运行时。

---

## 1. 调查结论：现状与证据

### 1.1 实际存在四个上下文平面，只有一个有主人

| 平面 | 载体 | 生命周期 | 现状 |
|---|---|---|---|
| P1 图通道 | `State`（pydantic + `extra="allow"` + reducer） | 跨节点、跨回合 | 声明字段与实际使用严重脱节（见 1.2） |
| P2 就地突变 | 同一个 State 实例被节点/工具直接赋值 | **节点内**（经 `InjectedState` 同实例） | 无声明的隐式契约；经核验其全部消费都是节点内的（见 5.3） |
| P3 ctx 动态属性 | `ArtemisContext`（`extra="allow"`） | 进程/运行期 | `latest_perception_data` 等动态挂载，无 schema |
| P4 持久存储 | DataEngine（步骤/截图/摘要）+ notes 文件 | 持久 | **唯一健康的平面**：单一写入路径，多消费者 |

Pro 的真实架构其实很好：Operator 每回合从 P4 **重新编译**全量 prompt
（`operator.py:624` → `build_plan_and_history`），不靠累积消息。问题不在架构方向，
而在 P1–P3 三个平面无 schema、无属主、互相兜圈子。

### 1.2 `extra="allow"` 已经吞掉的真 bug（本次调查实证）

**B1 幽灵键 `ui_tree`：4 个读取点，0 个写入点。**
`operator.py:995`、`validator/tool_declarations.py:477`、`mcp/action_executor.py:401`、
`tools/mobile/exec_tools.py:548` 均 `getattr(state, "ui_tree", None)`，但全仓无人写入
（真实数据在 `latest_ui_hierarchy`）。后果：`utils/coordinates.py:185-215` 的
"从层级树找主滚动容器"分支**永远走不到**，smart swipe 静默降级。

**B2 异步 Planner 驳回反馈被静默丢弃。**
`graph.py:195` 把驳回理由写进 `validator_messages` 通道，但该通道**没有任何节点读取**
（`operator.py:388` 和 `tool_wrapper.py:175` 只是当场拆 `Command.update` 信封，从不读
State 通道）。后果：计划被回滚了，Operator 下一回合看到回滚后的计划，却永远看不到
驳回理由。同时 `validator_messages` 经 `add_messages` 单调累积整轮所有 ToolMessage，
无人消费、无人裁剪。

**B3 `ctx.latest_perception_data` 孤儿写。**
`perception.py:187` 每轮把整张截图 b64 挂上 ctx，注释声称"给 Checker 零延迟复用"；
但工作区的 checker 重写已改为自己抓屏（`checker.py:620-652`）+ 从 DataEngine 取历史
证据。全仓**零读取点**，纯内存驻留。

**B4 `indexed_elements` 只写实例不回通道。**
`operator.py:649-650` 就地写两个字段，但 `operator.py:766` 的 update 只回传
`indexed_points`，漏了 `indexed_elements`。审计确认跨节点消费实际不存在（validator 侧
自行重推导，`action_executor.py:173-174`），但两键不对称是持续的误导源。

**B5 更多幽灵/半死键与坏路径。** 全表见附录 B。要点：
约 9 个只初始化/只读不写的死键；`graph_runner.py:48` 的
`getattr(self.ctx, "artemis_context", None)` 恒 None → 构造出丢失 data_engine 的空
ArtemisContext；且 `reactive_runner.py` 的 `State(messages=[])` 缺必填字段构造即抛
——engine 双 runner 的真机路径今日均必炸；`_active_driver` 未声明为 `PrivateAttr`。

（v2.1 更正：初版审计将 `operator_agent.py:63` 的 `task_goal` 与
`failure_analyzer_agent.py:48-50` 的 `failure_context` 列为图 State 幽灵键，实为
engine/BaseAgent 栈自己的 state 协议——`ExecutionContextState.task_goal` 存在
（core/state.py:48），`.get()` 有 `isinstance(state, dict)` 防护。两处均无需修改。）

**B6 感知数据同屏最多 4 份拷贝、抓屏三处重复实现。**
同一帧 b64 同时在 `ctx.latest_perception_data`（死）、`state.operator_raw_data`
（走图通道且随 `astream` values 流每超步复制，`sdk/agent.py:694-696`）、operator 局部、
prompt data-URL。抓屏管线在 perception_node（settle+OCR+融合）、validator 动作后轮询
（`validator.py:117-140`）、checker 终检（`checker.py:620-652`）三处各写一遍，
planner 首帧还有第四处自拍（`planner.py:211-236`，注意 planner 在图中先于 perception
运行，这是常规路径不是边角）。

### 1.3 Flash 与 Pro 的压缩机制现状

| | Flash | Pro |
|---|---|---|
| 上下文模型 | 累积消息 + 每轮幂等原地压缩（`compress_flash_messages`） | 每回合从 DataEngine 重编译（`build_plan_and_history`） |
| 步骤摘要 | `VisualStepSummarizer`：前后截图 + 动作红标叠加，客观中立措辞契约，异步无阻塞，无界重试 | `SummarizerNode`：**纯文本**（不加载 pre/post 图），3 次退避重试，无 flush 收尾语义 |
| 摘要落点 | `data_engine.update_step_summary`（同一个 API！） | `data_engine.update_step_summary` |
| 未就绪降级 | 摘要 pending → 保留原图（lossless pending） | 无 summary → detailed 渲染（同构原则的另一形态，`task_tree.py:917-921`） |
| 历史可见性策略 | 无（30 轮上限内全量） | 三种互斥策略 + 6 个调用点各自硬编码 kwargs |

关键事实：**两条路径的摘要已经写进同一个 DataEngine 字段**。但两者的摘要语义结构性
不同（见 6.1），统一的只能是运行时，不是语义。

---

## 2. 设计原则

1. **上下文是编译出来的，不是累积出来的。** Pro 已经如此；把它升格为正式模型，
   Flash 的消息列表只是同一份记忆的另一种渲染。
2. **一份情景记忆，多种视图。** DataEngine 是唯一事实源；每个 agent 看到的是按其
   角色声明的"最高价值密度视图"。agent 的智能上限由输入质量决定。
3. **可见域靠声明与类型约束。** `extra="forbid"` 拦截幽灵**写**与非法构造；幽灵**读**
   （`getattr` 带默认值不会因 forbid 报错）由节点 READS/WRITES manifest 的测试期
   代理拦截。两者缺一不可。
4. **重物不过通道。** 截图 b64 不进 LangGraph 通道；通道里只放引用，重物放进有属主、
   有生命周期的 store。
5. **就地突变不消灭，而是正名。** P2 平面的全部用途经审计都是"节点内工具通信"——
   给它一个声明的、节点局部的载体（TurnWorkspace），而不是假装它不存在。
6. **后台增强不可成为依赖。**（继承姊妹文档 §4.6）摘要未就绪必有无损降级。
7. **不损伤：删除必先核销。** 每个字段/属性/通道的处置都对照附录 B 台账，列明全部
   消费者与同步改动点；无消费者证据不删。

---

## 3. 方案 A：State 显式化与按 agent 可见域

### 3.1 State 收敛为四组显式字段，`extra="forbid"`

```python
class State(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    # ── 控制面（跨回合、路由与信号）──────────────────────
    initial_goal: str
    injected_instruction: str | None          # take_last
    user_stop_requested: bool                 # sticky_or
    checker_success: bool | None              # take_last
    operator_feedback: list[str] | None       # take_last；见 3.3
    exit_settlement_route: str | None         # take_last
    run_outcome: dict | None                  # take_last

    # ── 回合产物（上一回合的决策与结果）───────────────────
    current_step_id: str | None
    structured_decisions: str | None
    operator_raw_thinking: str | None
    operator_native_thinking: str | None
    short_term_memory: str | None             # 活跃字段；移除权属于姊妹文档 §3.3
    last_execution_result: dict | None
    subagent_calls: list[str]
    operator_tool_limit_exceeded: bool | None

    # ── 感知引用（重物不进通道，见方案 C）─────────────────
    perception: PerceptionRef | None          # take_last

    # ── 节点局部工作区（声明但非通道语义，见 5.3）─────────
    workspace: TurnWorkspace | None = None    # 不进 update、不跨节点
```

**处置总表见附录 B。** 与 v1 的差别：`short_term_memory` 保留（它是活跃读写对，
operator.py:743-765 写 → prompts.py:331 读；删除属姊妹文档 Phase 1 的事，本方案
不越权）；`indexed_*` 不再声称"并入冻结快照"（见 5.3）；`messages` 删除需同步改
三处消费代码（`sdk/agent.py:1182-1187` 兜底输出、`sdk/agent.py:1321-1325` stderr
打印、`engine/graph_runner.py:57` 入参——三处今日均为空转/坏路径，删除无信息损失，
但必须同 PR 内改掉）。

`asanitize_update`（`state.py:152-159` 空壳直通）删除；其在 14 处工具里的调用
随信封迁移（3.3）一并消失。

### 3.2 可见域：节点声明读/写集，测试期强制

每个节点类声明清单，运行期零开销，测试期强制：

```python
class OperatorNode:
    READS  = frozenset({"initial_goal", "perception", "operator_feedback",
                        "short_term_memory", "subagent_calls", "current_step_id",
                        "operator_tool_limit_exceeded", "injected_instruction",
                        "structured_decisions"})
    WRITES = frozenset({"structured_decisions", "operator_raw_thinking",
                        "operator_native_thinking", "short_term_memory",
                        "current_step_id", "subagent_calls",
                        "operator_tool_limit_exceeded"})
```

- 新增 `artemis/graph/visibility.py`：`StateView(state, reads)` 代理，未声明字段访问
  即抛 `VisibilityError`；`check_update(update, writes)` 校验返回键。
- 仅在测试与 `ARTEMIS_STRICT_STATE=1` 下启用；生产路径直接传 State。
- 附录 B 的实证读写清单即初始 manifest；上线先按现状放宽，再逐步收紧。
- **幽灵读的防线在这里**，不在 `extra="forbid"`（forbid 管不住 `getattr(x, k, None)`）。

### 3.3 工具信封与反馈通道拆分（修 B2）

`validator_messages` 承担两个不相干职责，拆开：

**1）工具返回信封 → 显式返回类型。**
审计确认全部 14 个工具写入点（`exec_tools.py` 5 处、`scratchpad.py` 6 处、
`launch_app.py:191`、`ocr.py:185`、`video_tool.py:161`、`object_detector.py:210,227`）
的 `Command.update` **只含**这一个键（以 `exec_tools.py:215-221` 为样本核验），
迁移是纯机械的：工具直接返回 `ToolOutcome(content, status)`，
`tool_wrapper.get_tool_result_content`（`tool_wrapper.py:175-176`）与 operator 循环
（`operator.py:388-391`）的拆信封逻辑同步简化。`graph.py:526` 的计划驳回信封同理
（它经 `_process_plan_write` 返回给 operator 循环当场消费，属节点内信封）。
通道及 `add_messages` 无界累积随之消失。

**2）跨回合反馈 → 统一 `operator_feedback` 通道。**
`check_feedback` 更名并扩容为 `operator_feedback`：来源包括 checker harvest
（`graph.py:198,230`）、exit settlement 弹回（`graph.py:367`）、以及**新接入的**
异步 Planner 驳回（`graph.py:195` 改写到这里，条目带 `[planner]` 前缀，与现有
`[final check]`/`[verify failed]` 前缀惯例一致）。渲染组件更名
`FeedbackPromptComponent`，提示语从"来自独立 Checker"改为按条目前缀中性表述。
现有读者同步改名：operator prompt（`prompts.py:243`）与 diagnoser
（`diagnoser.py:178`）——diagnoser 因此也能看到 planner 驳回上下文，属增益。

---

## 4. 方案 B：ArtemisContext 分解为组合根

ctx 保留为唯一入口（避免全仓签名大改），字段按属主聚类成显式子对象，
`extra="forbid"`：

```python
class ArtemisContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    # 基础设施（启动后只读）
    device: DeviceContext
    llm_config: LLMConfig | None
    model_router: Any | None
    agent_config: Any
    adb_client / ui_adb_client / actuator / action_session / mcp_*

    # 运行服务（有生命周期、有属主）
    execution_setup: ExecutionSetup | None
    data_engine: DataEngine | None
    perception: PerceptionStore            # 方案 C，取代 latest_perception_data
    step_memory: StepMemoryService | None  # 方案 D，统一摘要运行时
    background: BackgroundTaskGroup        # 吸收 background_tasks/jobs + 宽限期

    # 编排状态（从散字段聚成对象）
    checkpoints: CheckpointLedger          # 吸收 pending_checkpoints/checkpoint_tasks/
                                           #      attempt_seq/repairs/final_check_attempts/assert_halt
    plan_governor: PlanGovernor            # 吸收 task_plan_content_before/
                                           #      last_validated_plan/pending_validated_plan/planner_task

    run_outcome: dict | None = None        # 显式化（今日为 sdk/agent.py:721 动态挂载）
    package_cache: dict[str, str | None]
    _active_driver: Any | None = PrivateAttr(default=None)
```

`CheckpointLedger` 与 `PlanGovernor` 不是新逻辑，只是把 `graph/checkpoints.py` 和
`graph/graph.py::_process_plan_write` 已在操作的字段簇搬进属主类型。
`ExecutionSetup` 审计后可直接上 `forbid`（唯一运行期赋值 `app_lock_status` 为已声明
字段，`sdk/agent.py:997`）。`graph_runner.py:48` 的 `artemis_context` 恒 None 坏路径
在 Phase 0 修复（显式传入或显式报错，禁止静默构造空 ctx）。

---

## 5. 方案 C：感知走显式通道（PerceptionStore + TurnWorkspace）

### 5.1 数据模型

```python
class PerceptionSnapshot(BaseModel):
    """一帧感知的产物。frozen：每次捕获生成新快照，绝不就地改。"""
    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str | None               # 关联的 DataEngine step（轮询帧可为 None）
    image_name: str                   # sha256，DataEngine 图片主键
    screenshot_path: str | None
    screenshot_b64: str               # 重物：只存在于 store，不进图通道
    width: int
    height: int
    captured_at: float
    kind: Literal["decision", "post_action", "probe"]
    # 完整管线产物（raw 帧为 None）
    raw_xml: list[dict] | None
    fused_elements: list[dict] | None  # OCR+XML 融合
    ocr_results: list | None
    minimal_list: str | None           # 派生物在此算一次，消费者不再各自重算

class PerceptionRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    step_id: str | None
    image_name: str
    width: int
    height: int

class PerceptionStore:
    """ctx.perception。环形缓存最近 N=4 帧；更早的按 image_name/step_id 从
    DataEngine 回捞重建（b64 懒加载）。"""
    async def capture_full(self, *, settle: bool, kind: str) -> PerceptionSnapshot: ...
    async def capture_raw(self, *, kind: str) -> PerceptionSnapshot: ...
    def put(self, snap: PerceptionSnapshot) -> PerceptionRef: ...
    def latest(self, *, full_only: bool = False) -> PerceptionSnapshot | None: ...
    def get(self, ref: PerceptionRef | str) -> PerceptionSnapshot | None: ...
```

**抓屏统一收口**：现有三处重复管线 + planner 自拍全部改走 store——

| 现调用点 | 改为 | 备注 |
|---|---|---|
| `perception_node`（settle+OCR+融合，`perception.py:116-168`） | `capture_full(settle=..., kind="decision")` | settle 启发式与 OCR/融合逻辑原样搬入 store，行为不变 |
| validator 动作后轮询（`validator.py:117-140`） | 轮询保持轻量私有；**最终帧** `capture_raw(kind="post_action")` 入 store | 轮询中间帧不入 store，避免噪音；最终帧成为可引用证据（今日被就地写进 `state.latest_screenshot` 后即失名） |
| checker 终检自拍（`checker.py:620-652`） | `capture_full(kind="probe")` | 其 OCR+裁剪逻辑与 perception 重复，删除 |
| planner 首帧自拍（`planner.py:211-236`） | `capture_full(kind="decision")` | planner 先于 perception 运行是常规路径，必须显式支持 |

由此**每一帧都自动进入环形缓存与 DataEngine**，异步消费者（Checker 检查点）持
`PerceptionRef`/`image_name` 而非活对象——缓存被挤掉也能从 DataEngine 重建，
天然解决"异步任务引用过期感知"的悬挂问题。

### 5.2 通道改造

- `perception_node`：`capture_full` → `put()` → update 只含
  `{"perception": ref, "injected_instruction": ..., "user_stop_requested": ...}`。
  删除 `ctx.latest_perception_data`（B3）与 `operator_raw_data` 过通道（B6：
  b64 不再随 `astream` values 每超步复制）。
- 消费者一律 `ctx.perception.get(state.perception)` 或 `latest()`：
  - **Operator**：快照直取 `minimal_list`/`fused_elements`，不再自算；
  - **Validator 内工具**（object_detection 兜底 `object_detection_tool.py:93` 今日读
    validator 就地写的 `latest_screenshot`）：改读 `latest()`——validator 每次动作后
    `capture_raw` 入 store，时序语义与今日一致；
  - **Diagnoser/Committee/Explorer/FA**：同一入口，删除各自的文件重读与 getattr 链；
  - **Outputter**：今日读最终 State 的 `operator_raw_data`（最后决策帧）。改为
    `latest()`（最后 post_action 帧，比决策帧更新，属增益）；若需保持逐字节兼容，
    改读 `get(final_state.perception)` 即决策帧——落地时二选一并记录；
  - 幽灵键 `ui_tree` 的 4 个读取点改读 `snapshot.fused_elements`（B1 终态修复；
    Phase 0 先直改 `latest_ui_hierarchy` 止血）。

### 5.3 TurnWorkspace：给 P2 平面正名（修 B4 的正确姿势）

审计结论：`indexed_points`/`indexed_elements` 是**回合内可变工作集**——
explorer 增量 append 候选（`explorer_tool.py:311-315`），validator/FA 在动作后
整体重推导（`action_executor.py:173-174`）；且全部消费都发生在**产生它的节点内部**
（operator 在返回前就把索引翻译成坐标；validator 侧自给自足）。跨节点传递从未
被需要——B4 之所以没被发现，正因为如此。

因此它们**不属于冻结快照，也不属于图通道**，属于节点局部工作区：

```python
class TurnWorkspace(BaseModel):
    """节点局部的可变工作集。经 InjectedState 对同节点工具可见；
    不进 update、不跨节点、每节点从快照重播种。"""
    model_config = ConfigDict(extra="forbid")
    indexed_points: list[list[int]] = []
    indexed_elements: list[dict] = []

    @classmethod
    def seed_from(cls, snap: PerceptionSnapshot) -> "TurnWorkspace": ...
    def reseed(self, snap: PerceptionSnapshot) -> None: ...      # validator/FA 重观察
    def extend_candidates(self, cands: list[dict]) -> None: ...  # explorer 增量
```

- `state.workspace` 为声明字段但**永不出现在任何节点的 update 里**（visibility
  manifest 强制），每个需要它的节点入口自行 `seed_from(store.get(state.perception))`。
- 隐式契约（"恰好同一实例"）就此变成显式契约（"声明的节点局部容器 + 三个具名操作"）。
- `state.indexed_points`/`indexed_elements`/就地写 `latest_screenshot`/`operator_raw_thinking`
  等全部 P2 突变点随之收编（thinking 就地写是给工具内 `record_step` 读的，同为
  节点内通信，归入 workspace 或显式传参，落地时按触点少者取舍）。

---

## 6. 方案 D：记忆内核统一（Flash 压缩上提）

### 6.1 一个运行时，两份摘要契约

两个摘要器的差异是**结构性的**，不能合并语义：

- Flash 的摘要是**证据替身**——消息流保留了模型全部推理原文，压缩器只摘图，
  摘要因此必须是纯客观的视觉转换描述，掺入意图即是污染；
- Pro 的摘要是**整步胶囊**——`build_plan_and_history` 丢弃旧步骤的全部思考，
  一行摘要+精确动作+Validator 结果就是该步的全部遗留，因此必须压缩意图/策略/
  进度（这正是姊妹文档 §8.2 坚持 `summarizer.md` 为语义唯一权威的原因）。

因此统一的是**机制层**，分离的是**语义层**。新建
`artemis/memory/step_memory.py`：

- **共享运行时 `StepMemoryService`**（合并两边的调度骨架）：
  - 键：`step_id`（Flash 的 `tool_call_id` 保留为别名映射；`dispatch` 已有
    `data_engine_step_id` 参数，接线即可）；
  - 调度：零阻塞 dispatch + **有界**重试 + 显式降级态（替换 Flash 的无界重试与
    Pro 的 3 次退避；此为有意的行为变更，见 §8）+ `flush(timeout)` 收尾语义
    （Pro 目前 run 结束时未完成摘要行为未定义，直接补上）；
  - 落点：`data_engine.update_step_summary`；状态机（pending/ready/failed）与
    版本化按姊妹文档 §7.1；lossless-pending 原则由消费侧共同遵守；
  - 素材来源：pre/post 图直接以 `PerceptionRef` 引用（方案 C 的红利：不再在
    dispatch 参数里搬运 bytes，`summarizer.py:92-100` 的 `_step_inputs` 图字节
    缓存与手动置 None 回收逻辑消失）。
- **两份摘要契约（lens），各 profile 只跑自己的**：
  - `VisualTransitionLens`（Flash）：输入 pre/post 截图（动作红标叠加）+ 动作 +
    Controller 结果；产出客观视觉转换，`flash_summarizer.md` 禁判定词契约不变；
  - `StepCapsuleLens`（Pro）：输入按姊妹文档 §8.1（决策图 + 可选后图 + 动作 +
    Validator 结果 + FA 记录 + 当步思考 + 少量近期摘要）；产出意图/策略/进度
    胶囊，`summarizer.md` 为语义权威，视觉仅作接地、仍禁自行判定成败
    （姊妹文档 §4.5 事实优先级）。
  - 单 profile 单 lens，`update_step_summary` 单字段无冲突；未来若 Pro 需同时保留
    视觉转换记录，按姊妹文档 §7.1 版本化元数据加 lens 标记，不在本期范围。
- Pro 图中 `SummarizerNode` 退化为 `dispatch(StepCapsuleLens)` 一行调用；
  Flash `runner.py` 同理换成 `dispatch(VisualTransitionLens)`。

### 6.2 两个渲染器，一张策略表

渲染器保持各自形态（合理的方言差异，不强行同构）：

- `compress_flash_messages`：消息列表原地压缩，摘要后端换成共享运行时；
- `build_plan_and_history`：编译式渲染，无改动即消费到视觉接地摘要。

把 6 个调用点散落的硬编码 kwargs 收拢为**每 agent 一条声明**，新建
`artemis/memory/context_policy.py`：

```python
CONTEXT_POLICIES: dict[str, ContextPolicy] = {
    "operator":  ContextPolicy(strategy="strict_milestone", last_n_detailed=1, recent_window=3, chronological_last=True),
    "planner":   ContextPolicy(strategy="strict_milestone", last_n_detailed=2, recent_window=5, chronological_last=True),
    "failure_analyzer": ContextPolicy(strategy="strict_milestone", last_n_detailed=1, recent_window=3, for_failure_analyzer=True),
    "checker":   ContextPolicy(strategy="milestone_whitelist"),
    "diagnoser": ContextPolicy(strategy="milestone_whitelist", window_from_config="history_window_steps"),
    "committee": ContextPolicy(strategy="milestone_whitelist", last_n_detailed=3),
    "outputter": ContextPolicy(strategy="full", last_n_detailed=0),
    "summarizer": ContextPolicy(strategy="sliding_window", last_n_detailed=1, min_summaries=10),
}
```

这张表与 3.2 的 READS/WRITES manifest 互为镜像：一个声明"看 State 的哪些字段"，
一个声明"看历史的哪个投影"。合起来就是"按 agent 划分可见域"的完整答案，
也是今后调优各 agent 输入质量的唯一改动点。收拢时**逐调用点对拍**：策略表渲染
输出与现 kwargs 输出必须一致（对拍测试进 CI）。

### 6.3 与姊妹文档的配置收敛

姊妹文档 §14 主张 Pro 单独 `agent.pro.memory`、不复用 Flash `step_summarizer`。
本方案在语义层同意（lens 分离），仅在**运行时层**共享：

```jsonc
"agent": {
  "memory": {                       // 共享运行时：重试/flush/并发 + 策略表覆盖
    "runtime": { "max_concurrency": 1, "retry_limit": 3, "flush_timeout_s": 30 },
    "policies": { "operator": { "recent_window": 3 } }
  },
  "flash": { "step_summarizer": { /* VisualTransitionLens：模型、开关（现有键保持兼容） */ } },
  "pro":   { "memory": { /* StepCapsuleLens 模型/输入项 + token 预算等，per 姊妹文档 §14 */ } }
}
```

姊妹文档的 L3 历史分块、`recall_history`、token 预算不变，全部建立在同一个
`StepMemoryService` 产物之上。

---

## 7. 分阶段落地（已按依赖重排，消除 v1 的顺序隐患）

**Phase 0 — 修 bug（不动架构，可独立合入，零行为风险）** ✅ 已实施 2026-08-31
1. B1 止血：4 处 `ui_tree` → `latest_ui_hierarchy`（终态在 Phase 2 改快照）。✅
2. B2：`graph.py` 驳回反馈改走现有 `check_feedback` 通道（条目带 `[planner]`
   前缀，弃写无人读的 validator_messages），`CheckFeedbackPromptComponent`
   提示语按前缀中性化（标题保持 "Verification Findings" 不变，兼容既有断言）。✅
3. B3：删 `ctx.latest_perception_data` 写入与过期注释。✅
4. B4 止血：operator update 补回 `indexed_elements`（消除两键不对称；终态在
   Phase 2 收编进 workspace）。✅
5. B5：`_active_driver` → `PrivateAttr`；新增 `State.initial()` 工厂统一初始状态
   构造（sdk `_get_graph_state` 与 engine 双 runner 三处收拢单源）；
   `ExecutionContext.artemis_context` 显式声明；`graph_runner`/`reactive_runner`
   真机路径缺 ctx 时显式 RuntimeError（替代静默构造空 ctx 后期炸）。
   （task_goal / failure_context 两项经复核为误报，见 §1.2 v2.1 更正。）✅

验证：目标子集 99/99 通过；全量 `tests/unit + tests/tools` 1072 通过、
5 失败均为真实 LLM 联网测试的 API key 环境问题（与改动无关）。

**Phase 1 — State 显式化（仅动经台账核销的死键与信封）** ✅ 已实施 2026-08-31

实施要点（与下述计划的差异）：信封迁移最终形态为**工具直接返回 ToolMessage**
（承载 content/status/tool_call_id，未传 state 时仍返回纯字符串），未引入独立的
ToolOutcome 类型——ToolMessage 已是 LangChain 原生形态，语义完全覆盖；
`_process_plan_write` 的拒绝路径返回 status="success" 的 ToolMessage 以逐字节保持
旧信封行为（SystemMessage 无 status 属性 → 旧路径视为 success）。visibility
manifest 已接线至全部 7 个节点入口（`strict_state`，默认关闭零开销），
契约测试 tests/unit/graph/test_state_contract.py 11 项覆盖往返/forbid/代理/清单。
验证：tests/unit 全量 1067 通过 0 失败；tests/tools 仅余 5 个既有 API key
环境失败（与 Phase 0 前完全一致）。测试改写含 test_operator.py 12 处
asanitize 断言 → 返回值断言、15 个工具测试文件 Command→ToolMessage。
- 删真死键：`messages`（同步改 `sdk/agent.py:1182-1187`、`:1321-1325`、
  `graph_runner.py:57`、`_get_graph_state`）、`remaining_steps`、`focused_app_info`、
  `device_date`、`subgoal_plan`、`operator_tactical_plan`、`complete_subgoals_by_ids`
  （同步改 `operator.py:767` 与 `_get_graph_state`）、`current_agent`（scratchpad
  两处读改固定 `"operator"` 或显式传参）、`operator_replan_reason`（committee 去引用）。
- **不删**：`short_term_memory`（活跃；移除并入姊妹文档 Phase 1 落地时执行）、
  感知相关键（归宿在 Phase 2）。
- 信封迁移：14 个工具写入点 → `ToolOutcome` 直返；`operator.py:388-391` 与
  `tool_wrapper.py:175-176` 拆信封逻辑简化；删 `validator_messages` 通道与
  `asanitize_update`。
- `check_feedback` → `operator_feedback` 更名（operator prompt + diagnoser 两读者
  同步）。
- `extra="forbid"` 上 State + `State(**values)` 往返测试（`sdk/agent.py:696` 路径）；
  visibility manifest + StateView 测试强制（先按附录 B 现状放宽，再收紧）。

**Phase 2 — PerceptionStore + TurnWorkspace**
- store/快照/引用/环形缓存；四个抓屏点收口（§5.1 表）；消费者逐个切换；
- 删 `operator_raw_data`、`latest_ui_hierarchy`、`latest_screenshot`、
  `indexed_points`、`indexed_elements`（State 通道侧）；新增 `perception` ref 与
  `workspace`；
- outputter 帧选择决策（post_action vs 决策帧）落地并记录。

**Phase 3 — ctx 分解**
`CheckpointLedger` / `PlanGovernor` / `BackgroundTaskGroup`；`run_outcome` 显式化；
`extra="forbid"` 上 ArtemisContext / DeviceContext / ExecutionSetup / AppLaunchResult。

**Phase 4 — 记忆内核**
`StepMemoryService` 统一运行时、双 lens 分立语义（Pro 侧 `StepCapsuleLens` 即
姊妹文档 Phase 2 的实现载体）；策略表收拢（逐点对拍）；配置收敛。之后按姊妹文档
继续 L3 分块与 recall。

每阶段独立可回滚。Phase 0/1 以台账+现有单测护航；Phase 2 起加对拍测试。

---

## 8. 验收

**不损伤（最低要求）**
- 附录 B 台账全表核销：每个字段/属性的每个消费者都有对应改动与测试；CI 中
  visibility manifest 测试保证幽灵读为零。
- `State(**values)` 往返（`astream` values 流）在 `forbid` 下全绿。
- Flash：成功路径下压缩器输出与现版**逐字节对拍一致**；重试上界与 flush 属有意
  变更，单独测试覆盖（失败注入下有界退出 + 降级态可见）。
- Pro：`build_plan_and_history` 策略表收拢后与现 6 处调用点输出逐点对拍一致。
- 性能红线沿用姊妹文档 §18（P50/P95 输入与延迟回归 ≤3–5%，关键路径零摘要等待）。

**优雅处理所有信息（目标）**
- 全部 `extra="forbid"`；抓屏管线唯一实现；图通道序列化不再含任何 b64
  （对照：当前 `operator_raw_data` 每超步随 values 流复制）。
- Planner 驳回后，Operator 与 Diagnoser 的下一 prompt 可见驳回理由（新增回归测试）。
- validator 的 post-action 帧从"就地写路径后失名"变为可引用证据（store + DataEngine）。
- Pro 长任务（>30 步）历史出现视觉接地摘要；每个 agent 的输入面由 manifest +
  ContextPolicy 两张表完整声明。

---

## 附录 A：agent → State 读/写实证清单（2026-08-31 工作区）

（visibility manifest 初始值；来源为本次全仓扫描。）

| 节点 | 读 | 写 |
|---|---|---|
| perception | structured_decisions | latest_screenshot, latest_ui_hierarchy, operator_raw_data, injected_instruction, user_stop_requested |
| planner | latest_screenshot, initial_goal | （无通道写；就地突变 latest_screenshot） |
| operator | operator_raw_data, latest_ui_hierarchy, indexed_elements, subagent_calls, current_step_id, initial_goal, check_feedback, short_term_memory, operator_tool_limit_exceeded, injected_instruction | structured_decisions, operator_raw_thinking, operator_native_thinking, short_term_memory, indexed_points, complete_subgoals_by_ids, current_step_id, subagent_calls, operator_tool_limit_exceeded |
| execution_check | operator_raw_data, structured_decisions, operator_*_thinking, user_stop_requested, initial_goal | checker_success, validator_messages, structured_decisions, current_step_id, check_feedback, subagent_calls |
| validator | structured_decisions, latest_screenshot, current_step_id, operator_raw_data, operator_*_thinking, indexed_points, indexed_elements | last_execution_result（+就地突变 latest_screenshot, indexed_*） |
| summarizer | current_step_id, structured_decisions, operator_*_thinking, last_execution_result | （无；异步写 DataEngine） |
| exit_settlement | user_stop_requested, initial_goal | exit_settlement_route, check_feedback, run_outcome |
| checker | （不读 State） | （不写 State；ctx.assert_halt + ledger） |
| diagnoser | initial_goal, check_feedback, latest_screenshot, latest_ui_hierarchy | （无） |
| outputter | initial_goal, operator_raw_data | （无） |
| explorer 工具 | operator_raw_data, latest_screenshot, latest_ui_hierarchy, indexed_* | （就地 append indexed_*） |
| failure_analyzer | latest_ui_hierarchy, initial_goal | （就地重推导 indexed_*） |
| committee 工具 | latest_screenshot, initial_goal | （无） |
| video/object_detector/log/history_analyzer | （不读或仅透传；object_detection 兜底读 latest_screenshot） | validator_messages（信封用法） |

## 附录 B：信息处置台账（"不损伤"核销表）

### B.1 State 字段

| 字段 | 消费者（证据） | 处置 | 阶段 | 同步改动点 |
|---|---|---|---|---|
| initial_goal | 多节点读 | 保留 | — | — |
| injected_instruction | perception 写 / operator prompt 读 | 保留 | — | — |
| user_stop_requested | perception 写 / gate、checkpoints、plan 守卫读 | 保留 | — | — |
| checker_success | execution_check 写 / 两处 gate 读 | 保留 | — | — |
| check_feedback | graph.py:198,230,367 写 / prompts.py:243、diagnoser.py:178 读 | 更名 operator_feedback，扩容纳 planner 驳回 | P0(接入)+P1(更名) | 写 3 处、读 2 处、组件提示语 |
| run_outcome | graph.py:367 写 / sdk/agent.py:720 读 | 保留；ctx 侧动态挂载显式化 | P3 | sdk/agent.py:721 |
| exit_settlement_route | exit_settlement 写 / gate 读 | 保留 | — | — |
| current_step_id / structured_decisions / operator_*_thinking / last_execution_result / subagent_calls / operator_tool_limit_exceeded | 活跃读写对（附录 A） | 保留 | — | — |
| short_term_memory | operator.py:743-765 写 / prompts.py:331 读 | ~~**保留**；移除权属姊妹文档 §3.3~~ **已移除（M5，2026-09-01）**：全链核销——State 字段/operator 提取写块/prompt 组件/manifest 读写集/operator.json 指令段；渲染侧旧标签剥除保留 | — | — |
| messages | 写：无（Pro）；读：sdk/agent.py:1182(空转)、:1321(空转)；graph_runner.py:57 写(坏路径) | 删除 | P1 | 读 2 处、写 1 处、_get_graph_state |
| validator_messages | 14 工具信封写 + graph.py:195 跨回合写；通道 0 读 | 信封→ToolOutcome；:195→operator_feedback；删通道 | P0(:195)+P1(信封) | 14 工具 + tool_wrapper:175 + operator:388 + graph:526,595 |
| remaining_steps | 仅 init（sdk:1201）；上限实际由 recursion_limit 承载（sdk:674） | 删除 | P1 | _get_graph_state |
| focused_app_info / device_date | 仅 init（sdk:1197-1198） | 删除 | P1 | _get_graph_state |
| subgoal_plan / operator_tactical_plan | 仅 init / 零引用 | 删除 | P1 | _get_graph_state |
| complete_subgoals_by_ids | operator:767 写 []、init、0 读 | 删除 | P1 | operator:767、_get_graph_state |
| current_agent | scratchpad:166,260 等读（恒回退 "operator"），0 写 | 删除 | P1 | scratchpad 固定值或显式传参 |
| operator_replan_reason | committee_tool:203 读（恒 None），0 写 | 删除 | P1 | committee 去引用 |
| latest_ui_hierarchy | perception 写 / operator:614、diagnoser:193、FA:419、explorer:1595 读 | → snapshot.fused_elements | P2 | 4 读者切 store |
| latest_screenshot | perception/planner/validator/flash 写 / validator:98、diagnoser:186、committee:138、explorer:268、object_detection:93 读 | → snapshot + capture 收口 | P2 | §5.1 表全部触点 |
| operator_raw_data | perception 写 / operator:610、validator:618、explorer:290、outputter:139 读 | → PerceptionRef + store | P2 | 4 读者切 store；outputter 帧选择决策 |
| indexed_points / indexed_elements | 节点内写读（operator/validator/FA/explorer/action_executor，附录 A） | → TurnWorkspace（节点局部） | P2 | §5.3 全部触点 |
| （幽灵）ui_tree | 4 读 0 写 | P0 止血→latest_ui_hierarchy ✅；P2 终态 | P0/P2 | operator:995、tool_declarations:477、action_executor:401、exec_tools:548 |
| （更正）failure_context | engine/BaseAgent 栈自有协议，`.get()` 有 isinstance 防护 | 误报，不动 | — | — |
| （更正）task_goal | `ExecutionContextState.task_goal` 存在（core/state.py:48） | 误报，不动 | — | — |
| （幽灵）metadata | sdk/types/task.py:223 对 finalize 快照 dict.get（恒 None） | 无害；随 finalize 清理顺手删 | P1 | 1 处 |

### B.2 ArtemisContext 属性

| 属性 | 消费者 | 处置 | 阶段 |
|---|---|---|---|
| latest_perception_data | 0 读（HEAD 版 checker 曾读，工作区已删） | 删除写入 + 注释 | P0 |
| run_outcome（动态） | sdk:1107 读 → trace 后缀 | 显式字段 | P3 |
| _active_driver | drivers/factory:75-78、tools/base:104、mock:55、4 处测试 | → PrivateAttr | P0 |
| pending_checkpoints / checkpoint_tasks / checkpoint_attempt_seq / checkpoint_repairs / final_check_attempts / assert_halt | graph/checkpoints.py 族 | → CheckpointLedger | P3 |
| task_plan_content_before / last_validated_plan / pending_validated_plan / planner_task | graph.py:_process_plan_write / execution_check | → PlanGovernor | P3 |
| background_tasks / background_jobs / grace_period | ctx.__aexit__、summarizer:66、diagnoser:234 | → BackgroundTaskGroup | P3 |
| 其余声明字段 | 基础设施 | 保留（分组） | P3 |
| （engine 侧）ExecutionContext.artemis_context | graph_runner:48、reactive_runner:57 读，0 写 | 显式接线或显式报错 | P0 |
