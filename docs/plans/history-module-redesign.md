# ARTEMIS 历史模块重构：增量转写 + 分层压缩（统一方案 v3）

状态：v3 已实施并通过最终真机验收（2026-09-01，M0-M5 全部落地；最终验收五项全过，见 `docs/plans/history-acceptance-2026-09-01.md`；仅 git 提交待用户决定）

范围：Operator 执行历史的表示、累积、压缩与召回；Flash / Pro 双 profile 收敛为同一记忆运行时。

取代关系：
- **取代** `docs/design/pro-context-memory-redesign.md`（2026-08-25）的 §6/§11/§12（分层历史、token 预算、缓存布局）——其"每回合重编译 + 预算选择"的前提被本方案的"增量转写"取代；其 §7（数据模型）、§8（视觉接地中立摘要）、§9（稳定分块）、§10（recall）被**吸收并保留**。
- **修订** `docs/plans/context-visibility-redesign.md` 的 §6（方案 D 记忆内核）——StepMemoryService 统一运行时保留，但"两 lens 按 profile 分立"修订为"两 lens 按高度分立"（见 §5，决策变更已获用户确认，2026-08-31）。该文档的 Phase 0/1（State 显式化）已实施，Phase 2（PerceptionStore）/Phase 3（ctx 分解）不受本方案影响，Phase 4 由本方案取代。

---

## 0. 用户提案复述与结论

提案三条（2026-08-31）：

1. **每步与 Flash 一致，仅做增量构建**；后台用 Gemini 3.5 Flash-Lite 做图片压缩（视觉摘要替代截图）。
2. **里程碑边界压缩**：不再每步压缩，而是在**切换里程碑时**把上一段任务的历史压缩掉；保留现方案的滑动窗口底线（永远保留最近几步原始消息）。
3. **上限兜底**：长期不切换里程碑时，按最大上下文窗口限制触发压缩。
4. **（补充约束，2026-08-31/09-01 两轮明确）段级胶囊要特别详细、尽量维持原状、按步压缩，且定形为三段式**：① 梗概与效果（在做什么/做了什么/有什么效果，含留下的笔记）→ ② 压缩步摘要（"Steps xx–xx 做什么、Step xx 做什么"区间叙事）→ ③ 逐步动作账本（每步一行：步号+时间+动作）。理由：视频分析模块按步号/时间戳对齐视频时间轴，且大模型对任务整体的时间感知必须精确到步。落点见 §3.3。

**结论：方向成立，且与业界最优实践收敛。** Claude Code、Codex、Gemini CLI 全部采用"累积 + 阈值/边界触发压缩"，无一采用 ARTEMIS Pro 现在的"每回合全量重编译"（后者的直接代价：跨回合零消息连续性、零提示缓存命中、思考签名丢失）。姊妹文档把"真多轮消息"排在 Phase 5，本提案把它提前为核心架构——这是正确的提前。

在采纳的同时做**四点修正/补强**（正文展开）：

| # | 提案原文 | 修正 | 理由 |
|---|---|---|---|
| 1 | "后台每步做图片压缩" | 摘要**每步后台准备**，但替换**延迟到固定深度批量应用**（尾部倒数第 K 步才把图换成摘要） | 就地改写第 i 字节起的缓存前缀全部失效；固定深度擦洗让前缀单调增长（Anthropic computer-use 官方同款权衡，见 §2.5） |
| 2 | "切换里程碑时压缩之前的任务" | 里程碑切换当**触发器**，压缩块的**身份**按 step_id 范围锚定，里程碑 hash 仅作标签元数据 | 计划是动态索引不是历史键（姊妹文档 §9 实证结论）：计划改写/回滚/重命名不得破坏已压缩历史 |
| 3 | "按最大上下文窗口限制压缩" | 需要真实计量——现在全仓**没有任何 token 计量**（探索报告 §8）；用**上一轮 `usage_metadata.prompt_tokens` 作为当前上下文的实测值** + 增量启发式，零额外成本 | 字符估算对图片/多模态误差大；上一轮实测输入即本轮上下文基数 |
| 4 | （未提及） | 补两层：**紧急全量快照**（阈值兜底也爆时，Codex/Gemini CLI 式结构化快照重启）与 **recall_history 无损回捞**（DataEngine 冷历史，压缩永远可逆） | 无界任务（`[Loop:continuous]`）会耗尽任何有限分层；无损回捞是"敢压缩"的前提 |

---

## 1. 现状基线（2026-08-31 工作区实证）

### 1.1 Flash：append-only + 每轮就地压缩

- 消息列表建一次、只追加，**无任何消息驱逐**，唯一边界是 `max_turns=30`（`runner.py:166,177`）。
- `compress_flash_messages`（`context_compressor.py:43`）每轮 LLM 调用前全量重扫：只保留**最后一张**图（keep-last-1，`:62-72`）；历史 UI XML 从 `--- UI Element List ---` 标记截断（`:133-139`）；图片"摘要未就绪则保留"（lossless-pending，`:141-146`）；摘要以 `--- Historical Visual Transition ---` 前缀块注入 ToolMessage，幂等重建（`:125-128`）。
- 已知缺口：摘要**迟到回填**会突变任意深度的历史消息（缓存敌意）；图已被丢而摘要仍未到时该步**既无图也无摘要**（race，`:144` 只保护仍在场的图）。
- `VisualStepSummarizer`（`flash/summarizer.py:39`）：flash-lite、前图叠加红色动作标记+后图、**无界重试**（`_retry_delays` 末档 3s 永续循环，`:105`）、单次 25s 超时、成功后写 `data_engine.update_step_summary`（`:205`）。
- token 仅做事后记账（char/4 + 258/图启发式，`runner.py:293-315`），**从不驱动压缩**。

### 1.2 Pro：每回合全量重建，零跨回合消息

- `_build_prompt` 每回合新建 PromptBuilder，产出恰好 2 条消息（System + Human），回合结束整表丢弃（`operator.py:161-218,705-715`；`prompts.py:139-154`）。**跨回合连续性只剩 `plan_and_history` 渲染串 + `short_term_memory`**；思考签名、原生多轮全部丢失；提示缓存对易变尾部零命中（网关无任何 cache_control/cached_content plumbing，`llm.py` 全文）。
- 历史可见性 4 策略散布 8 个调用点硬编码 kwargs（`task_tree.py:797-942` + 探索报告 §2.4 全表）；`strict_milestone` 是硬悬崖——活跃里程碑外且最近 N 步外的历史**完全消失，无摘要残留**（`:859-876`）。
- 摘要未就绪的降级：Flash 保图，Pro 回退整步 detailed 渲染（`task_tree.py:920`）——两者都是"多花 token 等摘要"，但均不可观测。
- 两个 live bug：Diagnoser 传不存在的 `window_steps=` 参数 + 不存在的 `history_window_steps` 配置，异常被吞，**Diagnoser 的历史每次都渲染成错误串**（`diagnoser.py:160-174`）；`build_plan_and_history` 两个死参数（`chronological_last_step`/`detailed_subgoal_hashes`）4 个调用点仍在传。

### 1.3 存储与事件面

- `StepRecord` 无 `summary_status`/`version`/`chunk` 概念；`update_step_summary` 是后台盲覆写，无版本无序保证（`engine.py:974-1005`）。扩展逃生口是 `extra_metadata`（已承载 `subgoal_hash`/`width`/`height`/`token_usage`）。
- 里程碑切换的现成检测点：① 每步已盖章 `extra_metadata["subgoal_hash"]`（`graph.py:128`），相邻步 hash 变化即切换；② `_process_plan_write`（`graph.py:505-615`）是全部计划写入的单一漏斗，已在计算 `milestones_changed`/`new_top_level_completions`。重命名韧性由 `subgoal_hash_chain.json` 别名链保证（`task_tree.py:991`）。
- 现成接缝：operator 模板里 `unified_history=""` 是**已接线的空渲染槽**（`prompts.py:213`）；`PromptComponent` 是干净插件点；`MAX_MESSAGES_IN_HISTORY=25` 是死常量（`constants.py:16`）。

---

## 2. 业界机制要点（2026-08 调研，取数字用于定标）

### 2.1 Claude Code：三层递进

① **微压缩**（每次调用前、无模型参与）：清掉旧 tool_result，只留最近 3–5 个，占位符替代，原文落盘可回捞；② **服务端 context editing**（`clear_tool_uses`）：默认 100K 触发、keep 3，关键参数 `clear_at_least`——**不值得为小清理打破缓存就不清**（批量摊销失效成本的官方形态）；③ **全量压缩**：~95% 用量触发，9 节结构化摘要（意图/技术要点/文件/错误与修复/用户消息/待办/当前工作/下一步），压缩后重注入最近读过的 ~5 个文件。另有 memory tool：清理阈值临近时警告模型先把重要结果写进记忆文件。

### 2.2 Codex CLI：极简两件套 + 原生压缩

阈值 ≈ 有效窗口 − 13K（用户自设值被钳制在窗口 90% 以内）。压缩后**只剩两样**：一条摘要消息（进度/关键决策/约束/剩余工作）+ 最近 ~20K 原始用户消息；旧摘要被折叠进新摘要，**永不堆叠**。压缩后自动重读最近编辑的 ≤5 个文件（50K 预算）。GPT-5.1-Codex-Max 起模型**原生训练了跨窗口压缩**。UI 明示"多次压缩会降低准确性"。

### 2.3 Gemini CLI：70/30 + 结构化快照

历史达模型窗口 **70%** 触发；**最近 30% 原样保留**（切分点保证不劈开 tool-call/response 对）；更旧的压成 `<state_snapshot>` XML（overall_goal / key_knowledge / file_system_state / recent_actions / current_plan），压缩率 5–15%。

### 2.4 Manus KV 缓存法则（生产指标）

agent 输入:输出 ≈ 100:1，缓存命中是**第一生产指标**（cached $0.30 vs uncached $3.00/MTok）。法则：前缀字节级稳定（系统提示禁时间戳）、**append-only**（永不修改既往动作/观察、序列化确定性）、工具 mask 不删除、**文件系统即外部记忆**（可恢复地压缩：丢网页留 URL，丢文件内容留路径）、todo 复诵对抗 lost-in-the-middle、错误留在上下文里。

### 2.5 截图历史（computer-use 官方指引，与本仓直接同构）

Anthropic：截图每张 ~1,000–1,800 token，单请求 ≤20 张；**推荐保留最近 3 张，更旧的每 ~25 轮批量修剪**——明确说明逐轮修剪会每轮打破前缀缓存，批量修剪让失效事件之间前缀字节不变；被逐出的截图用一句文本摘要替位。参考实现即 `only_n_most_recent_images` 参数。OpenAI CUA 则靠服务端 `previous_response_id` 携带历史，客户端只上传新帧。

### 2.6 Gemini 缓存约束（本仓运行时）

隐式缓存 2.5+ 默认开启；**最小可缓存前缀 2,048（2.5 系）/ 4,096（3.x 系）token**，命中报告在 `cachedContentTokenCount`；条目短寿命，相似前缀请求要时间上贴近。显式 `CachedContent` TTL 默认 1h、按 token-hour 计存储费（explorer.py:1709 已有先例）。**任何更早字节的改动使其后全部失效**——这是修正 #1 的硬依据。

### 2.7 提炼的六条通用原则

1. 累积为常态，压缩靠**阈值/边界稀疏触发**，不逐步进行。
2. 突变要么贴尾（浅、可预测），要么稀疏成批（深、与语义边界重合）。
3. 摘要输出**结构化 schema**，不是自由散文；旧摘要折叠进新摘要，不堆叠。
4. 最近 K 步/最近 30% 永远原样——所有系统都有生窗口下限。
5. 压缩必须**可逆**：原文外置（磁盘/DataEngine），占位符携带回捞引用。
6. 计量用真实值，阈值分软/硬两档，硬档换更激进的形态而非同一手段加量。

---

## 3. 目标架构：单一转写（Transcript）+ 四区纪律 + 三级压缩

### 3.1 核心转变

Operator 的上下文从"每回合重编译的渲染串"变为**会话级增量转写**——一份 append-only 的消息账本，Flash 与 Pro 共用同一实现，由新服务 `TranscriptLedger`（挂 `ctx.step_memory` 之下或并列）持有。State 只携带轻引用，重物不进图通道（与 visibility 方案 C 同一原则）。

`build_plan_and_history` **不删除**，降级为两个角色：
- **辅助 agent 的编译视图**：Diagnoser / Committee / Outputter / HistoryAnalyzer / Planner / Checker 仍按各自 ContextPolicy 从 DataEngine 编译（Claude Code 同型：主循环累积、子 agent 拿新鲜编译上下文）；
- **转写的冷启动构造器**：进程重启/崩溃恢复时，从 DataEngine 步骤记录重建转写的冻结区。

### 3.2 四区纪律（缓存友好的突变规则）

```text
[S 稳定前缀]  系统提示 + 工具 schema + 计划语法
              整会话字节不变；≥4K token 以满足 Gemini 3.x 隐式缓存下限
[F 冻结历史]  已压缩产物：chunk 摘要块 + 逐步摘要行
              只在"压缩事件"时突变（稀疏、深）
[A 活跃窗口]  最近 N 步原始消息（AI 推理原文 + 工具结果）
              append-only；倒数第 K 步处有一个"擦洗沿"（浅、恒定深度）
[T 当前尾部]  本回合观察：当前截图 + UI 列表 + 计划复诵 + 注入指令
              每回合新建
```

**突变只允许三种，全部可预测：**

1. **贴尾擦洗（深度恒定，逐回合）**：
   - 深度 1：上一回合的 UI XML 列表、计划复诵副本剥除（现 Flash `prune_history_xml` 行为，代价只是最后一回合的缓存，几乎免费）；
   - 深度 K（默认 3）：倒数第 K 步的截图 → 替换为后台已备好的视觉转换摘要。摘要通常在几秒内就绪，K 步 ≈ 数十秒，几乎总能赶上；未就绪则该步暂缓（lossless-pending 保图），最多宽限 m 步（默认 3），仍未到则替换为占位符 `[visual summary pending; evidence at DataEngine step N]`——**彻底消灭现 Flash 的"迟到回填突变任意深度"与"图丢摘要空"两个缺陷**。
   - 恒定深度擦洗让缓存前缀**单调增长**（每轮失效的只是最后 K 步），同时 token 稳态恒定——严格优于"每 25 轮批一次"（后者在批间攒 25 轮的图）。
2. **压缩事件（稀疏、深）**：见 §3.3，F 区重写 + A 区前移。深突变与语义边界重合，此时接受一次全量 re-prefill（Claude/Codex 同款代价）。
   - **思维签名政策（2026-09-01 定）：回合内强制、A 区顺带、压缩即弃**——Gemini thought signature 仅在当前回合函数调用周期内是 API 硬要求（原样回传）；A 区 AIMessage 字节不动故签名免费有效（擦洗只碰观察/工具消息，从不碰 AIMessage）；已完结回合的签名 API 层面可选，chunk 化整回合移除后自然消亡，不建任何跨压缩维护机制（签名绑定消息精确内容，语义上不可能脱离原消息存续）。真机 A/B 无签名相关错误佐证。
3. **紧急快照（罕见）**：F 区整体替换为会话快照，见 §3.3 L3。

### 3.3 三级压缩

**L1 — 图片/XML 擦洗**（上文深度 K 机制）。摘要由 `StepMemoryService` 每步后台 dispatch（flash-lite，沿用红标叠加 + 禁判定词契约），**准备是每步的，应用是延迟的**。

**L2 — 段压缩（chunk 化）**，触发器取先到者：
- **里程碑切换**：**唯一事实源是已执行步骤的 `subgoal_hash` 实际变化**（`_record_turn` 处比较相邻步盖章）；`_process_plan_write` 的顶层完结事件只入队一个"待确认边界"，由下一个盖章步骤确认后才触发——防止被棘轮验证否决回滚的计划写入造成伪切换。切换时把上一段（不含活跃窗口底线内的步）压成一个 **HistoryChunk**；
- **尺寸阈值**（不切里程碑时的兜底，即提案第 3 条）：开放段累计 ≥12 步或段内摘要 ≥2K token；
- **边界事件**：包/Activity 大切换、目标变更、FA 边界、不可逆副作用（继承姊妹文档 §6.5 全表）。

Chunk 身份 = `(start_step_id, end_step_id, source_step_ids)`；`subgoal_hash`（经别名链解析）只是标签。

**chunk 是"分段账本"，不是融合胶囊（用户硬约束，2026-08-31）**：压缩只缩每步的**宽度**（推理原文/XML/截图 → 摘要/动作行），永不缩步的**数量**——段内步 1:1 保留，不合并、不重排、不丢步。两个刚性理由：① 视频分析模块以步号/时间戳为外键对齐视频时间轴，步粒度一旦融合即断链；② 大模型对任务整体的时间感知必须精确到步（"第 23 步做了什么、距今多少步"要可直接回答）。

**chunk 内部结构为三段式（用户 2026-09-01 定形：梗概 + 效果 + 压缩步摘要，底下逐步动作）**：

```text
[Chunk 3 | 里程碑「登录并进入设置页」| Steps 18–29 | T+12:40 → T+15:02]

① 梗概与效果（LLM 段头）
  本段在做什么：为后续改配置，先完成登录并进入设置页。
  实际做了什么：用账号 A 走了短信验证码登录，途中关闭了一次推广弹窗。
  效果/留下什么：已登录态（账号 A）；验证码输入页出现过一次超时重发；
    笔记 notes/login_flow.md 记录了验证码入口路径。
  Entry: 应用主页，未登录    Exit: 设置页已打开
  Verified: …  Unresolved: …  Failed paths: …  Entities: …

② 压缩步摘要（LLM，逐区间叙事，必须带步号且全覆盖）
  - Steps 18–20: 进入登录页，切换到验证码登录并填入手机号
  - Step 21: 首次获取验证码后等待期间出现推广弹窗，关闭之
  - Steps 22–26: 输入验证码，一次超时后重发并重新输入
  - Steps 27–29: 登录后从侧边栏进入设置页

③ 逐步动作账本（机械拼装，1:1，永不省略）
  - Step 18 (T+12:40): tap(632,1180)[登录入口] → executed
  - Step 19 (T+12:56): tap(210,884)[验证码登录] → executed
  - Step 20 (T+13:04): type("138…") → executed
  …（每步一行，直到 Step 29）
```

三段的分工与生成方式：

- **① 段头梗概（LLM，gemini-3.7-flash）**：三问式散文（在做什么 / 做了什么 / 有什么效果——效果必须包含本段**留下的笔记**：段内 notes 写入/更新的文件与要点引用）+ 结构化字段（沿姊妹文档 §7.3：verified_facts / unresolved / failed_paths / important_entities / entry_state / exit_state）。要求**特别详细、尽量维持原状**：宁可长，不许把事实抽象到不可辨认。**笔记关联是机械校验项（用户 2026-09-01 定，"不能丢"）**：段内每条 note 写入（save/update/append_note，经 trace 抓取）的目标文件 key 必须出现在段头 effect 中，缺任一即该次尝试判负重生成——与 ② 的覆盖校验同级；chunk 存"指针+要点"，笔记全文在 notes 系统（read_note/recall 可回捞），可恢复压缩。
- **② 压缩步摘要（LLM，同一次调用产出）**："Steps xx–xx 做了什么，Step xx 做了什么"式的区间叙事，输入是段内逐步视觉摘要 + 动作 + Validator 结果。硬性约束：每行必须带步号引用；**区间并集必须无缝覆盖整段步范围**（机械校验，缺口即重生成）；同一区间只并列同质连续动作，异质动作单步成行；中立措辞契约沿用（禁判定词）。
- **③ 逐步动作账本（机械拼装，不经 LLM，零失真）**：步号 + 相对时间 + 精确动作 + Controller/Validator 结果短语，全部从 DataEngine 确定性渲染。这是视频对齐与步级时间感知的**硬骨架**，任何层级的压缩都不得触碰。三条账本细则：
  - **动作必须带元素语义（用户约束，2026-09-01）**：`tap(632,1180)['确认按钮']` 而非裸坐标。索引点击路径已在记录时存 `target_text/resource_id/class`（`operator.py:893-896`）；裸坐标路径（`operator.py:854-861` 返回全空）与 Flash/FA 动作工具需补**记录时语义充实**（见 §6.1）；坐标永远保留（视频对齐/重放的外键），语义并列。
  - **FA 恢复子动作入账**：一步内 Failure Analyzer 补做的真实设备动作（`_exec_*`，已在 `interleaved_events`）以缩进子行机械渲染——视频轴上有帧的动作，账本必须有行。
  - **用户注入指令逐字保全**：段内 `injected_instruction` 以独立行原文保留在 ③（`User @ Step 21: "…"`），任何压缩层级 never-evict（Codex 保用户消息 / Claude Code 摘要设 All User Messages 节，同一共识）。

每步的完整视觉摘要不再逐条出现在 chunk 里（它们是 ② 的生成输入，原文永远在 DataEngine，recall 可回捞；A 区未压缩前照常在场）。chunk 版本化 append-only。

**就绪门控交换（用户 2026-09-01 定，"必须就绪再换"；修订 M3 原实现）**：段头 ①② 未就绪时**禁止**交换——原始消息原样留在转写里，lossless-pending 原则从图片层贯彻到段层。边界事件只做两件事：关闭段、dispatch 段头生成；**交换发生在段头就绪之后的下一次渲染/压缩事件**（深突变时机照旧成批，缓存代价不变，只是延后）。理由：段头缺席时 `verified_facts` 缺席，只剩"发生了什么"没有"确立了什么"——模型会怀疑已验证事实并重做，这是实测过的死循环高危形态。降级梯：生成失败（有界重试耗尽）→ 原文继续保留 + fallback 模型重投；软阈值压力下仍未就绪 → 依旧保留原文（正确性优先于 token）；**仅硬阈值紧急态**允许 ③-only 交换（带 pending 标记与 recall 指引；checker post-hoc 标注独立于段头，pending 态照常渲染）或直接走 L3。M3 原"冻结 ③ + pending 标记、下次事件收割"的实现按此修正——A/B 中"21 chunk 段头全 pending"在门控下退化为"尚未压缩"的安全形态，而非"历史只剩账本"。

**旧 chunk 不堆叠新摘要**：无界任务（`[Loop:continuous]`）下 chunk 数超限时做二级合并（era 合并）——**合并的只是 ① 段头**（结构化字段集合并，非散文再摘要），② 区间叙事与 ③ 逐步账本仍按段保留（预算极紧时 ② 可退化为只留区间标题行）。可见预算靠分级降宽（全三段 → ①合并+②③ → ①合并+③ → 时段段落）+ recall 兜底。

**极限层（决策 5 终审定案，用户 2026-09-01）：允许丢步，压成"时段段落"**——era 数再超上限时，最老 era 整体压缩为一段话，**必须携带步号范围与起止时间**：`[Era N | Steps a–b | T+hh:mm → T+hh:mm] <一段话概括该时段在做什么/做了什么/留下什么>` + recall 指引行；用户注入指令行仍逐字保留（never-evict 不随丢步豁免）。段落**机械拼装优先**（由该 era 各 chunk ① 的 doing/did/effect 与 verified_facts 浓缩，不做散文再摘要的 LLM 调用；段头缺失的 chunk 回退用里程碑标签）。步级可寻址在此层降级为"时段可寻址"：起止时间是视频对齐的粗粒度外键，步级细节走 recall_history 回捞（DataEngine 永远完整）。此前"步骨架任何层级不破"的表述据此修订：**该不变量适用于 L2 chunk、era 合并、L3 快照，唯极限时段段落层除外**。

**L3 — 紧急全量快照**：实测 token 越过硬阈值（如连续注入大量工具输出）时，F 区的**段头/知识面**整体替换为一个结构化会话快照（schema 取 Gemini CLI `state_snapshot` 与 Claude Code 9 节的交集，加本域字段）：`overall_goal / plan_state / verified_facts / unresolved / failed_paths / important_entities / device_app_state / recent_actions`。快照从 chunk 段头结构化字段合并生成，尽量不做"摘要的摘要"。**逐步索引仍保留**（最小行宽：步号 + 相对时间 + 动作短语），步级时间骨架在最坏情况下也不断链。A 区底线不动。

**滑动窗口底线（提案第 2 条保留）**：任何触发器都不得把 A 区压到 `min_active_steps`（默认 5）以下；最近 1–2 个完整回合（AI 推理原文 + 工具结果 + Validator 配对）永远原样——姊妹文档 §4.2"连续推理先于压缩"原则不变，且切分点永不劈开 tool-call/response 对（Gemini CLI 同款约束）。

### 3.4 计量与阈值

- **实测优先**：每轮把上一次 LLM 调用的 `usage_metadata.prompt_tokens` 记为转写基数，加本轮追加内容的启发式增量（char/4 + 每图按分辨率估算）作为决策输入。无估算器依赖、无额外调用。
- 预算三档（**临时默认值，M0 实测后定标**）：`context_budget_tokens=80K`；软阈值 0.7（56K）→ 触发 L2 压最旧段；硬阈值 0.9（72K）→ L3 快照。工具单次输出上限 16K（Codex 同款），超限走同回合信封（姊妹文档 §13 保留）。
- 姊妹文档实测提醒仍有效：8.8–12.7K 区间"长度与延迟无关"**不可外推**到 80K；但缓存命中改变了成本函数——M0 必须重测缓存命中下的 TTFT 曲线再定 budget。

### 3.5 可逆性与召回

- 一切被压缩内容在 DataEngine 原样在案（现状已满足：截图 SHA-256 内容寻址、步骤/trace 全量）；占位符与 chunk 块一律携带 step 引用。
- `recall_history` 工具照姊妹文档 §10 原样落地（FTS + step 范围 + 包/Activity + 屏幕 hash，≤5 结果 ≤2K token，图片按需回捞单步）；本地屏幕相似度提示（不发图、不调模型）保留。
- **Operator 只挂一个历史工具（2026-09-01 定，防过度设计）**：取历史 = chunk ①②③ 常驻索引（免费）→ `recall_history` 确定性回捞（一次查询，事实级）。HistoryAnalyzer **不进 Operator 工具清单、不进提示词指导**——它输出属推理级（事实优先级链最末）、每次调用是全量历史输入 + 关键路径等待，且"开放式跨历史语义问题"的需求目前纯属推测。仓库中它照旧存在（其他流程自用）；若真机 trace 日后出现"recall 反复查不到、问题属聚合/因果型"的实证，再评估以升级路径接入。era 溢出标记行指向 recall。

---

## 4. 与提案逐条对照

| 提案 | 方案落点 |
|---|---|
| 每步增量构建（Flash 式） | §3.1 Transcript：Pro/Flash 统一 append-only；Pro 获得真多轮 + 思考签名 + 缓存命中 |
| 后台 flash-lite 图片压缩 | §3.3 L1：每步后台准备（StepMemoryService dispatch），深度 K 延迟应用（修正 #1） |
| 里程碑切换时压缩前段 | §3.3 L2：里程碑切换是首要触发器；chunk 身份按 step 范围（修正 #2） |
| 滑动窗口底线 | §3.3：`min_active_steps` + 最近完整回合硬保留 |
| 长期不切换按窗口上限压 | §3.3 L2 尺寸阈值 + §3.4 实测计量（修正 #3）；再兜底 L3 快照（补强 #4） |

---

## 5. 摘要语义：两个 lens 按"高度"分工（决策修订，已确认 2026-08-31）

visibility 文档 §6.1 曾定"两 lens 按 profile 分立"（Flash=证据替身，Pro=整步胶囊），依据是 *Pro 丢弃旧步思考*。**本方案改变了这个前提**：转写保留推理原文直到段压缩，Pro 的步级摘要也变成了"证据替身"。因此修订为：

- **步级 = `VisualTransitionLens`（唯一步级 lens，双 profile 共用，输入按可得自适应）**：**替换哪张图就描述哪张图，有什么用什么**（用户 2026-09-01 定）——Flash 前后双图照旧；Pro 仅单决策帧+动作红标（描述该屏内容与动作落点，转换信息由相邻步摘要自然链接），单图时提示词不含 AFTER ACTION 段。**Pro 无动作后抓屏是刻意安全网**：ADB 截图延迟会丢瞬态控件（实测视频播放控件），截图量已压到最低——不得以任何方式"修复"（不补抓、不用步 N+1 前图拼后图）。禁判定词契约不变（successfully/completed/entered/navigated/failed/... 全禁）；缺后图永远只表示"无独立后验证据"。
- **段级 = `StepCapsuleLens` 产出 chunk 的 ①+②**：意图/策略/进度的压缩发生在段压缩时，输入是段内逐步视觉摘要 + 动作 + Validator 结果 + 该段推理原文（压缩前最后一次可读）+ 段内 notes 写入记录。单次调用产出 ① 梗概与效果（三问式散文 + 结构化字段，含留下的笔记）与 ② 压缩步摘要（步区间叙事，步号全覆盖机械校验）。**③ 逐步动作账本机械拼装不经 LLM**（用户硬约束：按步压缩、尽量维持原状、时间感知精确到步）。`summarizer.md` 的语义权威与中立契约（含 `[Loop]` 循环不算异常的判别）迁移到 ①②。
- **会话级 = 快照 schema**（L3 专用）。
- 事实优先级链不变：用户指令 > Validator/Controller 客观结果 > XML/OCR 差异 > 有源摘要 > 推理。

推论：Pro 图中的 `SummarizerNode` 不再逐步生成文本胶囊，退化为 `dispatch(VisualTransitionLens)` 一行；辅助 agent 编译视图中的步行改为"时间 + 视觉摘要 + 精确动作 + Validator 结果"，段外历史提供 chunk digest。**这是对先前已确认决策的修订，用户已于 2026-08-31 拍板采纳（见 §10 决策 1）。**

---

## 6. 数据模型与图接线

### 6.1 DataEngine 增量（最小 schema 变更）

- `StepRecord.extra_metadata` 增写：`summary_status`（pending/ready/failed/stale）、`summary_source`（lens 名）、`summary_version`、`summary_model`——`update_step_summary` 从盲覆写升级为带状态与版本的写入（同键并发以版本判序）。
- 新增 chunk 存储（SQLite 新表，字段即 §3.3 结构 + `session_id`/`version`）；`get_agent_friendly_steps` 增配套 `get_history_chunks`。
- 修 `record_step` 的隐式语义：post==pre 时 `post_image_name=None` 表示"无独立后图证据"，摘要器**不得**据此推断"屏幕未变"（姊妹文档 §4.3 原则写进 lens 契约）。
- **动作语义充实（record-time enrichment，M0）**：动作落 DataEngine 前，凡带坐标而缺 `target_text` 的（裸坐标点击 `operator.py:854-861`、Flash 动作工具、FA `_exec_*`），对当帧感知快照做命中测试（fused_elements → XML → OCR 取覆盖该点的最小可交互元素），best-effort 填 `target_text/target_class/target_resource_id`，并记 `target_label_source: "index"|"hit_test"|"ocr"|"none"` 区分模型指名与事后推断。渲染侧 `format_action_clean`（task_tree.py:39-64）已支持 target_text，零改动受益。Phase 2 PerceptionStore 落地后命中测试即快照一次查询，零额外抓取。

### 6.2 运行时归属

```text
ctx.step_memory: StepMemoryService     # visibility 方案 D 保留：调度/有界重试/flush/step_id 键控
    ├─ lenses: VisualTransitionLens / StepCapsuleLens(chunk) / SnapshotLens
    └─ transcript: TranscriptLedger    # 新：四区消息账本 + 擦洗沿 + 压缩事件
```

- 有界重试（替换 Flash 无界循环）+ `flush(timeout)` 收尾 + 显式降级态，均照 visibility §6.1 原文。
- 转写在内存；崩溃恢复走 §3.1 冷启动构造器。素材以 `PerceptionRef`/`image_name` 引用（Phase 2 落地后免搬 bytes；Phase 2 未落地前先传 bytes，接口留 ref 形态）。

### 6.3 图接线

- **事件源**：`_record_turn`（`graph.py:108-148`）比较相邻步 `subgoal_hash` → `transcript.on_milestone_boundary()`；`_process_plan_write` 的 `new_top_level_completions` → 同一入口（去重）。
- **Operator 切换**（flag `memory.transcript.enabled`）：`_build_prompt` 改为 `transcript.render_messages(tail=当前观察)`；旧 2-message 路径完整保留为回退。现成接缝 `unified_history` 槽位用于过渡期 A/B 对拍。
- **Flash 切换**：`compress_flash_messages` 的逐轮全量重扫替换为擦洗沿推进；`max_turns` 可放宽（压缩已兜底上下文）。
- 辅助 agent 不动图结构，编译视图接 ContextPolicy 策略表（visibility §6.2 原样，8 个调用点逐点对拍）。
- 顺手修：Diagnoser `window_steps` bug、两个死参数、死常量 `MAX_MESSAGES_IN_HISTORY`。

### 6.4 配置（收敛 visibility §6.3 与姊妹文档 §14）

```jsonc
"agent": {
  "memory": {
    "runtime": { "max_concurrency": 2, "retry_limit": 3, "flush_timeout_s": 30 },  // max_concurrency 1→2：硬化轮定标（基线报告 §6）
    "transcript": {
      "enabled": true,                  // M2-M4 出厂关灰度；M5 起默认开（2026-09-01 用户拍板）；false 即逐字节回滚开关
      "context_budget_tokens": 80000,   // 2026-09-01 基线定标：维持（稳态 ≤29% 预算）
      "soft_ratio": 0.7, "hard_ratio": 0.9,
      "min_active_steps": 5,
      "image_scrub_depth": 3, "pending_grace_steps": 3,
      "xml_scrub_depth": 1,
      "similarity_hint": true, "similarity_max_distance": 5  // M5 定标 8→5（460 步 dHash 分布）
    },
    "chunking": { "max_steps": 12, "target_source_tokens": 2000, "model": "gemini-3.7-flash", "max_chunks": 8, "max_eras": null },  // max_eras null=随 max_chunks（M5 新独立键）
    "step_lens": { "model": "gemini-3.5-flash-lite" },   // 现 agent.flash.step_summarizer 键保持兼容别名
    "recall": { "enabled": true, "max_results": 5, "max_text_tokens": 2000 },
    "policies": { /* 辅助 agent ContextPolicy 覆盖 */ }
  }
}
```

---

## 7. 分阶段落地

前置说明：visibility 文档 Phase 0/1 已实施；其 Phase 2（PerceptionStore）/Phase 3（ctx 分解）独立推进，与本轨道仅在 §6.2 的 ref 传递处交汇（非阻塞依赖）。

**M0 — 计量与 schema（无行为变化，可独立合入）** ✅ 代码侧已实施 2026-08-31；✅ 基线录制已完成 2026-09-01（真机 Pixel 10，见 `docs/plans/history-baseline-2026-09-01.md`）
- `summary_status/source/version` 落 `extra_metadata`；`update_step_summary` 版本化；
- 动作语义充实（§6.1）：裸坐标/Flash/FA 动作记录时命中测试补 `target_text` 等字段（越早上线历史数据越完整）；
- 转写计量管线（上轮 prompt_tokens 采集 + 增量估算）纯记录不决策；采集缓存命中率基线（`cachedContentTokenCount`）；
- 修 Diagnoser bug、死参数、死常量；Flash 摘要重试改有界 + flush 语义（visibility §8 已定为有意变更，单测覆盖）；
- 冻结代表性长任务集，录基线：输入 token、TTFT、全回合延迟、摘要就绪延迟、重复动作率、FA 触发率（姊妹文档 Phase 0 原样）。

M0 实施要点（2026-08-31，工作区未提交；与上述计划的差异逐条标注）：

1. **摘要状态与版本化**：`StorageManager.update_step_summary` 升级为原子读改写
   （单连接内读 `extra_metadata` → 判序 → 写回），签名
   `(step_id, summary, *, source, version, model, status)`，返回 bool（stale 写返回
   False）。判序规则：显式 `version` 低于在库版本即丢弃；`version=None` 自动
   `在库+1`；**状态降级需显式更高版本**——`pending` 不覆盖 `ready/failed`、
   `failed` 不覆盖 `ready`（防后台竞态回退）；`summary` 列仅被
   `ready`+非 None 摘要触碰。`DataEngine.update_step_summary` 同签名透传（后台
   写 + 仅 ready 发 SSE，与旧行为逐字节一致）。调用方：Flash
   `VisualStepSummarizer` dispatch 时补写 `pending`、成功写
   `ready/source="visual_transition"/model`、耗尽写 `failed`；Pro `SummarizerNode`
   写 `ready/source="step_capsule"/model`。`stale` 态本阶段只入枚举，无写入方
   （M2 起由重摘/回滚使用）。
2. **动作语义充实（record-time enrichment）**：新模块
   `artemis/utils/element_hit_test.py`（`find_element_at_point` /
   `hit_test_semantics`：最小覆盖元素胜出，XML 源优先于 OCR 源，best-effort 永不
   抛异常）。三条落点：① Pro 裸坐标分支（`operator.py` `resolve_target_element`
   坐标路径）就地命中测试 `state.indexed_elements`，click/long_press/input_text
   动作 dict 增 `target_label_source`（索引路径盖 `"index"`）；②
   `McpActionExecutor._execute_device_action`（Flash 与 FA 共用漏斗）在
   `_translate` 后、设备调用前对归一化 target 命中测试（**必须在 observe 之前
   ——observe 会覆写 indexed_elements 为后帧**），语义并入 action trace 的 args
   payload（FA 的 interleaved_events 渲染 `format_tool_call_clean` 零改动受益）并经
   `ToolExecutionResult.metadata["target_semantics"]` 回传；③ FlashRunner 落
   `record_step` 前把该 metadata 合入 `action_dict` 顶层。与计划的差异：
   `click_sequence`/`swipe` 多点动作本阶段不充实（单目标动作先行）；"可交互"
   以"已被感知索引"近似（索引元素不带 clickable 位）。
3. **token 计量管线**：新模块 `artemis/services/token_meter.py`
   （`extract_usage` 统一 LangChain `usage_metadata` 与 Gemini 原生
   `cachedContentTokenCount`/`input_token_details.cache_read` 两种形态；
   `SessionTokenMeter` 按会话累计）。采集点接在 LLM 网关
   `RobustChatModelWrapper.complete()/ainvoke()` 出口（全部生产调用必经；raw
   模型旁路仅测试使用，不采集），每次调用记一条 `llm_usage` trace（type
   `llm_call`）：单次 prompt/completion/cached + `context_base_tokens`（=本次实测
   prompt，即下一轮上下文基数）+ 会话累计五项（calls/prompt/completion/cached/
   cache-hit calls）。纯记录，不驱动任何决策；采集失败静默降级永不进入调用路径。
4. **三缺陷修复**：① Diagnoser `get_recent_subgoal_hashes` 改传
   `base_dir`（对齐 committee_tool 用法），Diagnoser 历史渲染恢复正常；②
   `build_plan_and_history` 删除 `chronological_last_step`/`detailed_subgoal_hashes`
   死参数，operator/failure_analyzer/planner 3 个调用点与 2 处单测同步清理；③
   删死常量 `MAX_MESSAGES_IN_HISTORY`（constants.py，全仓零引用确认）。
5. **Flash 摘要重试有界化**（有意行为变更）：`_run_summary_until_ready` 以
   `1 + retry_limit` 次尝试封顶（`StepSummarizerConfig.retry_limit`，默认 3，
   FlashRunner 接线），耗尽进显式 failed 态（`has_failed()` + DataEngine
   `summary_status="failed"`）；**failed 步在压缩器视角仍为 pending**（保图，
   lossless 语义不变——变更只砍掉无限 LLM 重试，不改消息流）；`flush(timeout)`
   收尾语义保留。
6. **测试**：新增 `tests/unit/utils/test_element_hit_test.py`（6）、
   `tests/unit/data_engine/test_step_summary_versioning.py`（7，含并发判序/降级
   拒绝/SSE 仅 ready）、`tests/unit/test_token_meter.py`（7）、
   `tests/unit/mcp/test_action_executor_semantics.py`（4，含缺元素数据优雅降级
   `"none"`）、operator 命中测试正例 + Flash 有界重试失败注入 2 例；存量断言
   同步（operator 精确 dict 断言补 `target_label_source`、两摘要器调用断言补
   kwargs）。验证：tests/unit 全量 **1094 通过 3 跳过 0 失败**；tests/tools
   相关子集（operator/diagnoser/validator）7 通过（既有 5 个 API key 联网失败
   未触发）。
7. ~~**未完成项**：代表性长任务基线录制~~ **已完成（2026-09-01）**：3 个冻结
   长任务 ×2 样本真机录制完毕，正式基线与 §3.4 定标建议见
   `docs/plans/history-baseline-2026-09-01.md` §3/§6（要点：Operator 首调输入
   P50 12.2K/P95 17.6K；隐式缓存基线 43% 调用/29.8% token；模型调用 P50
   13.8s；摘要就绪 P50 60s/P90 214s——长尾超擦洗窗口，定标建议
   max_concurrency 1→2）。附带发现：视觉 lens 调用走 `get_google_llm` 原始旁
   路，无 llm_usage 计量（与本条"raw 旁路仅测试使用"表述不符，待收口）。

**M1 — StepMemoryService + 擦洗沿（先 Flash 落地验证）** ✅ 代码侧已实施 2026-08-31（缓存命中提升待真机实测）
- 共享运行时成形；Flash 从"逐轮全量重扫"切到"擦洗沿"（深度 K 图替换 + 宽限 + 占位符）；
- 验收：成功路径消息序列与现版语义等价（图→摘要时点后移属有意变更，单独对拍）；缓存命中率显著上升（有实测基线可比）；race 缺陷回归测试（图丢摘要空、迟到回填深突变均不再发生）。

M1 实施要点（2026-08-31，worktree `zealous-gates-4626b1` 未提交，叠加在 M0 之上；与计划的差异逐条标注）：

1. **StepMemoryService 成形**：新 `artemis/memory/step_memory.py`——调度骨架自
   Flash `VisualStepSummarizer` 上提：零阻塞 `submit`、有界重试（1 + retry_limit，
   沿 M0 语义，耗尽进显式 failed 态并触发 `_on_status` 钩子）、`flush(timeout)`
   收尾（无参时用配置的 `flush_timeout_s`）、`max_concurrency` 信号量封顶并发
   （重试退避 sleep 在信号量之外，不饿死同伴）、状态查询
   （has_job/is_pending/has_failed/get_summary/get_job_payload）。**键控**：有
   DataEngine step_id 时以 `str(step_id)` 为规范键，tool_call_id 落别名映射；两者
   皆无的旧调用方保持序号键（压缩器的 tool_call_id → 序号回退链原样保留）。
   `VisualStepSummarizer` 取"并入服务"路线——改为服务子类，只保留视觉 lens 语义
   （红标叠加、`flash_summarizer.md` 禁判定词契约、M0 版本化
   `summary_status` 写入），对外表面（dispatch 签名/查询方法）零迁移。与计划的
   差异：a) 服务本阶段仍由 FlashRunner 自持，未挂 `ctx.step_memory`（M2 Pro 切换
   时再上提组合根）；b) lens 未做成独立协议对象，视觉 lens 即子类 `_attempt`
   （StepCapsuleLens 落地时再抽形式化 lens 接口）；c) `max_concurrency=1` 默认值
   使摘要 LLM 调用串行化（旧实现无界并发），属计划 §6.4 既定值。
2. **擦洗沿压缩器**：`ScrubEdgeCompressor`（`context_compressor.py`）——索引制
   旁路账本（契约：FlashRunner 消息列表 append-only），增量发现新消息，冻结前缀
   永不重扫。纪律：深度 1 剥 XML（保留集 = 最新 `xml_scrub_depth` 条观察，合并块
   保前缀语义与 `prune_history_xml` 开关照旧）；深度 K 图片决议——ready→摘要块
   （沿用 `--- Historical Visual Transition ---` 前缀、剥图后追加于消息尾，与旧
   产物同形）、failed→立即 `[visual summary unavailable; evidence at DataEngine
   step N]`（**不吃宽限**，M0 的"failed 仍保图"按计划收口为有意行为变更，单测
   覆盖）、pending→保图宽限至深度 K + pending_grace_steps，越限则
   `[visual summary pending; evidence at DataEngine step N]`；宽限期内摘要到达即
   正常替换；决议后冻结，**迟到摘要永不回填**（哈希不变量测试）。N = Flash 动作
   序号（与 DataEngine 步序一致）。细节差异：a) 无 job 的历史图（初始观察、摘要
   器关闭时的动作图）同样计深并在沿上静默剥除（无占位符）——旧实现是"下一轮即
   剥"，新时点后移至深度 K；b) 宽限计量单位是"图深度推进"而非回合数（无新图的
   空转回合不消耗宽限，上下文也未因图增长）；c) 旧 `compress_flash_messages` 原
   样保留，作语义对拍参照与回退路径（M2 转写落地后再删）。
3. **配置**：`agent.memory.runtime`（max_concurrency=1/retry_limit=3/
   flush_timeout_s=30）与 `agent.memory.transcript`（image_scrub_depth=3/
   pending_grace_steps=3/xml_scrub_depth=1）已落 `AgentGlobalConfig.memory`；
   显式的旧键 `flash.step_summarizer.retry_limit` 在 before-validator 里播种
   `memory.runtime.retry_limit`（新键显式设置时新键胜出，旧键本体不动）；
   enabled/model/prune_history_xml 三键权威仍在旧块（未迁移）。
   `xml_scrub_depth` 已实现为通用深度（>1 时保留更多层 UI 列表），默认 1 与现
   行为等价；`prune_history_xml=false` 时冻结也不剥 XML（保旧开关语义）。
4. **FlashRunner 接线**：摘要器以 `memory.runtime` 三值构造；压缩器为 per-run
   实例（擦洗账本是运行态）；`flush()` 走配置超时。
5. **测试**：+20（`tests/unit/memory/test_step_memory_service.py` 7：零阻塞/有界
   重试/别名键控/并发封顶/flush 超时与默认值/重投递清 failed；
   `tests/unit/agents/test_flash_scrub_edge.py` 8：成功路径新旧对拍（时点后移
   在断言中显式标注）、race a/b/c、冻结哈希不变量、XML 深度 1 等价、
   prune 开关、非跟踪消息不触碰；`tests/unit/test_memory_config.py` 5：默认值/
   显式值/旧键播种/新键优先/无显式旧键不播种）。存量适配 1 处：
   `test_summarizer_keys_actions_independently_of_turn_number` 的
   `_step_inputs["tc-1"]` 直取改为 `get_job_payload`/`resolve_key`（step_id 键控
   所致）。验证：tests/unit 全量 **1114 通过 3 跳过 0 失败**（M0 基线 1094+3，
   只增不减）；tests/tools 子集（operator/summarizer/validator/diagnoser）7 通过。
6. **max_turns 未动**（默认 30）：擦洗沿使图片上下文稳态有界，更大 max_turns
   已安全，放宽属配置决定，留给使用方/后续里程碑。
7. **未完成项**：缓存命中率提升的量化（M1 验收第二条）依赖 M0 尚未录制的真机
   基线——计量管线已就绪，待真机长任务前后对比。

**M2 — Pro 转写切换（本方案核心，flag 灰度）** ✅ 代码侧已实施 2026-09-01；✅ 真机 A/B 已完成 2026-09-01（红线全过且大幅改善，含一处阻塞 bug 的最小修复，见 `docs/plans/history-baseline-2026-09-01.md`）
- **前置子任务**：operator.json 模板拆分——今日 SystemMessage 经 `# CURRENT OBSERVATION` 切分后含易变的 plan+history（prompts.py:218-222），必须把静态规则留 S 区、动态内容移出，否则 S 区无从字节稳定；
- `TranscriptLedger` + Operator 双路径并存；转写路径 A/B 对拍旧路径（同任务同设备，姊妹文档 §18 红线：P50/P95 延迟回归 ≤5%、关键路径零摘要等待）；转写内时间一律 `T+mm:ss` 起点偏移（冻结后字节稳定），不用 "ago" 措辞;
- SummarizerNode 退化为视觉 lens dispatch（§5 决策已确认）；辅助 agent 编译视图接 chunk 前先保持现状。

M2 实施要点（2026-09-01，worktree `zealous-gates-4626b1` 未提交，叠加在 M0+M1 之上；与计划的差异逐条标注）：

1. **模板拆分（前置子任务）实现为运行时拆分**：operator.json 本体一字节未动——
   唯一易变跨度是字面量 `PLAN_HISTORY_TEMPLATE_SECTION`（`## Current Plan &
   Execution History\n{{ plan_and_history }}`），转写路径
   `render_transcript_static_system`（prompts.py）把它替换为静态指针段后渲染，
   得到整会话字节不变的 S 区（可用性装配 `resolve_operator_prompt_tools` 与
   语法开关 `_operator_grammar_flags` 从 TemplatePromptComponent 逐字提取共用，
   旧组件行为等价）。硬约束双保险：模板中该字面量"恰出现一次"的结构断言
   （缺失即抛错，§9 风险落地）+ 拆分前录制的旧路径渲染 SHA-256 golden
   （空计划/哨兵计划两份，`test_operator_transcript.py`）。S 区不再以
   `# CURRENT OBSERVATION\n` 结尾——该头衔移入每回合尾部观察消息。
2. **TranscriptLedger**（新 `artemis/memory/transcript.py`）：S/F/A 三区实体
   + 每回合 T 尾部由 render 参数传入。A 区 append-only；回合落账两段式
   **stage→commit**（step_id 与 Validator 结果在 execution_check/validator 之后
   才存在，故本回合消息先 stage，下一回合 build 时带 `step_key` +
   `validator_result` 提交）。Validator 结果消息门控
   `state.structured_decisions`（planner 拒绝清空、无终端动作为 None，均不落
   账），渲染为 `Status: <status>` + format_result_clean 错误/修复细节，带
   `T+mm:ss` 偏移（`format_session_offset`，分钟不进位保字节稳定单调）。
   观察消息的摘要键走 `id(message)` 侧表（不进消息序列化载荷）。工具配对不
   变量：擦洗只改内容块、永不删消息，切分点天然不劈 tool-call/response 对
   （单测覆盖）。
3. **擦洗沿复用=泛化 ScrubEdgeCompressor**（未另造 Pro 压缩器）：新增三个
   构造参数——`summary_key_getter`（无 tool_call_id 的 Pro 观察 HumanMessage
   按 step_id 键控查摘要）、`strip_markers`（深度 1 剥除扩展到
   `--- Visible UI Elements ---` 与 `--- Task Plan (recited) ---` 计划复诵副本）、
   `tail_offset=1`（活尾部在账本外，深度算术与 keep 窗口对齐 Flash 语义）。
   默认参数下 Flash 路径字节不变（M1 全部擦洗测试原样通过）；宽限/占位符/
   冻结不回填纪律在 Pro 消息形态下重跑 M1 用例形态（`test_transcript_ledger.py`）。
   `get_step_number` 从 VisualStepSummarizer 上提到 StepMemoryService 基类。
4. **Operator 双路径**（flag `agent.memory.transcript.enabled`，默认 false，
   出厂关）：关——旧 `_build_prompt` 路径零改动（golden + 存量 20 测试全绿）；
   开——`_build_prompt_transcript` 产出 S+F+A+尾部，尾部 = `# CURRENT
   OBSERVATION [T+mm:ss]` 头 + 计划复诵（新 PlanRecitationPromptComponent，
   复诵 task_plan 原文）+ 既有观察/注入/反馈组件原样复用。转写构建异常自动
   回退旧路径（灰度安全网）。与计划的差异：a) 过渡接缝取 PromptComponent
   机制新增组件而非 `unified_history` 空槽位（改动面更小，任务书允许取小者）；
   b) 复诵内容是 task_plan checklist 而非 build_plan_and_history 编译串——
   历史已由 A 区原始消息承载，整串复诵会大量重复（§3.2 T 区语义即"计划复诵"）。
   Operator 可见性读集新增 `last_execution_result`（落账需要，documented
   decision）。
5. **服务归属收口（M1 差异点 a）**：`ctx.step_memory` / `ctx.transcript_ledger`
   成为 ArtemisContext 声明字段（并列而非持有——§6.2 给出的两个选项取后者）；
   `artemis.memory.ensure_step_memory(ctx)` 为组合根工厂（Operator 转写路径与
   SummarizerNode 共用一个实例）；FlashRunner 构造后发布到 `ctx.step_memory`
   （只发布不消费，兼容存量 mock ctx 测试）；`ArtemisContext.__aexit__` 在后台
   任务清理前 flush 服务（异常退出 timeout=0 直接取消）。State 不携带任何消息
   重物（`test_state_contract.py` 全绿）。
6. **SummarizerNode 切视觉 lens（无条件，非 flag 后）**：不再自调 LLM 生成文
   本胶囊；按 step_id 从 DataEngine 取 pre/post 图 bytes（`get_step_image_path`），
   动作取步记录 `action_taken`（缺则回退 structured_decisions 解析），dispatch
   到 `ctx.step_memory` 视觉 lens（红标叠加、flash_summarizer.md 契约、M0 版本
   化 `source="visual_transition"` 写入均沿用）。图缺失→None bytes 纯文本降级，
   不抛错。与计划的差异：多动作步 dispatch 首动作为主体、其余进
   `action_args["additional_actions"]`（红标叠加只画单动作）。过渡期安全性靠
   task_tree.py "无摘要回退 detailed" 语义（未动）。
7. **冷启动构造器（最小版）**：转写空（0 回合、无 F 区）而 DataEngine 已有步
   记录时，用 `build_plan_and_history(min_summaries=len(steps),
   last_n_detailed=1)` 全量策略渲染冻结块，头部 `[Restored history]` 并注明
   "step times relative to the original session start"（该块内的 relative_time
   仍是 DataEngine 措辞，冻结一次生成故可接受；A 区从当前步正常追加）。
8. **测试**（只增不减）：+22 净增——`tests/unit/memory/test_transcript_ledger.py`
   12（S 区哈希稳定/A 区 append-only/T+mm:ss 与禁 ago/深度 1 剥复诵与 UI 列表/
   深度 K ready 替换/宽限→占位符→冻结不回填/failed 立即占位/配对不劈/冷启动
   块唯一性/双 stage 自动落账/无动作回合不落 Validator 消息）；
   `tests/unit/agents/test_operator_transcript.py` 7（golden 双哈希/模板单一
   切点/S 区静态渲染无历史/两回合四区结构/无动作提交/冷启动 F 区/flag 关
   2-message 不变）；test_summarizer.py 重写为 4（dispatch 断言与不再写胶囊/
   图缺失优雅降级/structured_decisions 回退与 additional_actions/无 step_id
   no-op）；test_memory_config.py +1（flag 默认关与显式开）。验证：tests/unit
   全量 **1136 通过 3 跳过 0 失败**（M1 基线 1114+3，只增不减）；tests/tools
   子集（operator/summarizer/validator/diagnoser）7 通过。
9. ~~**未完成项（待真机）**~~ **A/B 已完成（2026-09-01，见
   `docs/plans/history-baseline-2026-09-01.md`）**：红线全过且大幅改善——全回
   合延迟 P50 21.4s→5.0s（−77%）、缓存命中调用 43%→88%（token 29.8%→49.6%）、
   输出 token −74%、成功率 6/6 持平、零回退零摘要等待、无签名/格式类错误。
   三点必读：① A/B 中发现并最小修复了一个阻塞 bug——`_build_prompt_transcript`
   冷启动检查在第 2 回合误命中（上回合只 stage 未 commit，turn_count==0 且
   steps 非空），`set_restored_history` 被空账本不变量拒绝→每回合静默回退旧路
   径；修复为冷启动条件加 `not ledger.has_staged_turn`（operator.py，已在
   worktree，未提交，**需本轨道认领合入并补"第 2 回合 steps 非空"回归测试**）。
   ② 处理组实际是 M2+M3 叠加（chunking 挂同 flag），纯 M2 归因未拆。③ 实验
   模型为 gemini-3.6-flash（3.7 当日 503）。思考签名跨压缩事件的供应商侧验证
   仍留 M3 前。灰度顺序照 §10 决策 3：建议修复合入后复测一轮再进默认配置。

**M3 — L2/L3 压缩** ✅ 代码侧已实施 2026-09-01（真机验收待跑）
- 里程碑边界 + 尺寸阈值 + 边界事件三触发；chunk 表 + era 合并；L3 快照；
- 验收：>30 步长任务转写 token 稳态有界；计划改写/回滚后 chunk 身份与可见性不受损（新增回归：重命名走别名链）；`[Loop:continuous]` 100+ 步试跑无摘要堆叠。

M3 实施要点（2026-09-01，worktree `zealous-gates-4626b1` 未提交，叠加在 M0+M1+M2
之上；全部行为在 `agent.memory.transcript.enabled` flag 之后；与计划的差异逐条标注）：

1. **HistoryChunk 存储**：SQLite 新表 `history_chunks`（chunk_id/session_id/
   start·end_step_id/start·end_step_number/source_step_ids/subgoal_hash 标签/
   version/status/band1 JSON/band2/band3/rendered_text/created_at）。身份 =
   (session_id, 步号范围)，行**append-only**：同范围更高 version 追加新行，
   `get_history_chunks` 默认取各范围最新版（`all_versions=True` 给审计全轨）。
   DataEngine 增 `record_history_chunk`（后台写）/`get_history_chunks`。
   **单写者纪律**：全部 DB 写经 HistoryChunkManager 内存镜像（capsule 服务不
   直写 DB），版本号无竞争。
2. **三段式 chunk（§3.3 逐字对齐）**：
   - **③ 机械账本**（`chunking.build_action_ledger`，不经 LLM）：每步一行
     `- Step N (T+mm:ss): <format_action_clean> -> <结果短语>`（时间 = 步
     timestamp − session_start_time，经 `format_session_offset`，禁 "ago"）；
     结果短语 = format_result_clean 错误/修复细节，success 收敛为 `executed`；
     FA 恢复动作（interleaved_events 中 `_exec_*`）以 `    FA: ` 缩进子行经
     `format_tool_call_clean` 渲染；injected_instruction 逐字独立行
     `  User @ Step N: "…"`，**任何层级 never-evict**（含 era ③ 溢出标记与
     L3 最小索引，单测覆盖）。为此 `_record_turn` 把 `state.injected_instruction`
     盖章进步记录 extra_metadata（execution_check 读集补声明，documented
     decision）。差异：多动作步仍一行，首动作 + `(+N more actions)` 后缀。
   - **①+② StepCapsuleLens**（`memory/chunking.py` + 提示词
     `memory/step_capsule.md`，模型 `agent.memory.chunking.model`）：单次调用
     产出 JSON（doing/did/effect 三问式——effect 硬性要求含段内 notes 写入的
     目标文件与要点，输入里逐步附 save/update/append_note 的 key+content 摘录、
     task_plan 写入除外——+ verified_facts/unresolved/failed_paths/
     important_entities/entry·exit_state + intervals）。② 区间并集
     `validate_interval_coverage` 机械校验（缺口/重叠/越界即该次尝试判负→
     StepMemoryService 有界重试→耗尽 failed→chunk 维持 pending，③ 独立可用，
     单测覆盖）。中立契约禁词表沿 flash_summarizer.md/summarizer.md 迁入，
     `[Loop]` 循环不算异常的判别一并迁入 lens 提示词。**lens 形式化接口收口
     （M1 差异点 b）**：`step_memory.StepLens`（`render(key, payload)`）+
     StepMemoryService 可选 `lens` 参数与默认 `_attempt` 委托 + `_on_ready`
     钩子；差异：VisualStepSummarizer 保留内联 `_attempt`（零迁移），未强迁。
   - **异步纪律**：capsule 经 `ChunkCapsuleService`（StepMemoryService 子类，
     memory.runtime 三值）后台 dispatch；压缩事件时刻未就绪→冻结 ③ + pending
     标记；就绪结果**只在下一次压缩事件**收割（镜像+DB ready 版本写均在收割
     时；ctx.__aexit__ 增 chunker.flush 兜底收割落库，无 F 区重渲染）——冻结
     块事件间字节稳定有哈希单测。
3. **触发接线**：里程碑唯一事实源 = `_record_turn` 记步后通知
   `on_step_stamped(step_id, subgoal_hash)`，chunker 内比较相邻盖章；
   `_process_plan_write` 的 `new_top_level_completions` → `queue_boundary_hint`
   （未确认边界），下一盖章步 hash 未变即丢弃（棘轮否决回滚的伪切换单测覆盖；
   注：执行图在 `_record_turn` 前恒 await planner 验证，盖章天然后于回滚落定）。
   尺寸阈值 = 开放段回合数 ≥ `chunking.max_steps` 或段内转写文本 char/4 ≥
   `chunking.target_source_tokens`（差异：源 token 估算用账本内该段消息文本，
   非"段内摘要"——转写擦洗后消息主体即摘要，作机械代理）；软阈值 =
   SessionTokenMeter 的 last_prompt_tokens ≥ budget×soft_ratio 时压最旧开放段
   （≤max_steps 回合；测试经 `meter_getter` 注入假计量）。**压缩事件在
   ledger.render 时执行**（operator 单线程路径；graph 事件只置状态）：底层
   `_active` 列表保持 append-only（`_active_start` 边界前移，ScrubEdge 索引
   恒有效），边界永在完整回合末（不劈 tool-call/response 对，单测覆盖）；
   加严：闭合段须**整段**滑出 `min_active_steps` 保护窗才压缩——底线只延迟
   事件、不切分段（忠于"上一段压成一个 HistoryChunk"），仅尺寸/软阈值可对
   开放段出窗部分分片（每片 ≤ max_steps）。
4. **L3 紧急快照**：last_prompt_tokens ≥ budget×hard_ratio 时 F 区渲染为单块
   会话快照（overall_goal 取 operator 构造时 initial_goal / plan_state 读
   task_plan 原文 / verified·unresolved·failed_paths·entities 结构化字段机械
   集合并 / device_app_state 取最新有 capsule chunk 的 exit_state）+ **每
   chunk 最小行宽逐步索引**（`build_action_ledger(minimal=True)`：步号+T+时间+
   动作短语，user 行保留）。差异：计划"一次 LLM 润色可选"未实现（纯机械合并，
   避免摘要的摘要）；recent_actions 未入快照（A 区底线本就保留最近原始回合）。
5. **era 合并**：F 区完整 chunk 数超 `chunking.max_chunks`（新键，默认 8）时
   最旧溢出段折叠成 era（① 结构化字段集合并非散文再摘要、② 退化为
   `- Steps a–b: <里程碑标题|did 首句>` 标题行、③ 逐段 1:1 保留）；era 数再超
   同一限值时最旧 era 的 ③ 溢出为
   `[Era N | Steps a–b: ledger via recall_history]` 标记行（user 注入行仍逐字
   保留）。**§10 决策 5 按建议实施，待用户终审**（终审后标记行已升级为时段
   段落，见本节末"决策 5 终审收尾"）。差异：每次折叠事件生成一个
   era（未做跨事件 era 再合并）；era 上限复用 max_chunks 同值。
6. **异步检查点回果**：`checkpoints.harvest_run` 在 `_record_verdicts` 后
   best-effort 通知 `annotate_from_checkpoint(checkpoint_id, verdicts)`——按
   subgoal hash 经 `get_all_subgoal_aliases` 别名链匹配 chunk 标签（重命名
   韧性单测），命中即追加 band1.annotations + **version bump + DB 即时追加**，
   F 区文本下一压缩事件才重渲染（单测覆盖）。
7. **配置**：新块 `agent.memory.chunking`{max_steps=12, target_source_tokens=
   2000, model="gemini-3.7-flash", max_chunks=8}；transcript 增
   context_budget_tokens=80000 / soft_ratio=0.7 / hard_ratio=0.9 /
   min_active_steps=5。真机基线报告（history-baseline-2026-09-01.md）截至实施
   时不存在，全部维持计划临时默认，**待定标**。
8. **Flash 侧**：chunker 只在 Pro 转写路径的 TranscriptLedger 上构造
   （operator `_ensure_transcript_ledger`，有 DataEngine 才挂）；Flash 尚无
   ledger，M3 对 Flash 零行为变化。chunker 对里程碑事件是可选输入，尺寸/软硬
   阈值纯凭 ledger 回合元数据触发——Flash 未来接 ledger 时自动呈现"只有尺寸
   阈值触发"的任务书形态。
9. **测试**：+29 净增——`tests/unit/memory/test_history_chunking.py` 25
   （③ 结构 golden 与 FA 子行/注入逐字/最小行宽可寻址、② 覆盖校验与缺口
   重生成与有界重试降级 pending、三段顺序 golden（对照 §3.3）、真切换整段
   成 chunk、伪切换回滚丢弃、尺寸两式、软/硬假计量注入、min_active 底线、
   F/A 边界不劈对、冻结字节稳定与就绪不即时回填、回果 version bump 与下次
   事件重渲染、重命名别名链、era 合并 ③ 保留、era 溢出标记与 never-evict、
   全层级步可寻址不变量、lens 接口）；
   `tests/unit/data_engine/test_history_chunk_storage.py` 4（roundtrip/版本
   append-only 最新胜出/范围排序/会话隔离）。验证：tests/unit 全量
   **1165 通过 3 跳过 0 失败**（M2 基线 1136+3，只增不减）；tests/tools 子集
   （operator/summarizer/validator/diagnoser）7 通过。
10. **未完成项（待真机/后续）**：验收三条真机项（>30 步 token 稳态、
    `[Loop:continuous]` 100+ 步无堆叠试跑、capsule 质量盲评）；辅助 agent
    编译视图接 chunk digest（§10 决策 4 建议 M3 末——本次任务书未含，留 M4
    与 recall 一起收口）；阈值定标依赖真机基线报告。

**M4 — recall + 策略表收拢** ✅ 代码侧已实施 2026-09-01（阈值定标待真机）
- `recall_history` 工具 + 屏幕相似度提示；ContextPolicy 8 调用点逐点对拍收拢；配置收敛与旧键兼容别名。

M4 实施要点（2026-09-01，worktree `zealous-gates-4626b1` 未提交，叠加在 M0+M1+M2+M3
之上；与计划的差异逐条标注）：

1. **recall_history 工具**（新 `artemis/tools/history_recall.py`，规格照姊妹文档
   §10）：签名 `(query, step_range=None, include_details=False,
   include_images=False, max_results=5)`。第一期实现为 **Python 扫描而非
   SQLite FTS5**（差异：现库无任何 FTS5/索引，且 `get_agent_friendly_steps`
   已一次性给出全部检索面；FTS 留待量级需要时加）——检索面：步摘要/精确动作/
   Validator 结果/Operator 思考/FA trace 与注入指令（步记录）、XML/OCR 文本
   （`storage.get_image`，**扫描封顶最近 150 步**）、notes 文件（`utils/notes`）、
   history_chunks（band2/band3/rendered_text）。包/Activity 检索经 XML blob
   文本命中（差异标注：`record_step` 的 `foreground_app` 形参历史上从未被任何
   调用方写入，包名不单独持久化）。边界全部落地：结果数钳到
   `recall.max_results`；响应 char/4 估算超 `max_text_tokens` 即截断并提示收窄；
   **每条结果带步号**（notes 锚定到最近写入步，无写入记录则"as of Step N"）;
   include_details 每字段限幅；include_images 仅回捞**单步**真实存在的图
   （data-URL 块，形态镜像 ObservationPromptComponent；文件不存在给文字说明，
   永不多于 1 步）。**step_range 请求总是附带该范围的 `build_action_ledger`
   全宽行**——era ③ 溢出标记行 `[Era N | Steps a–b: ledger via recall_history]`
   由此获得真实入口。空 query 且无 step_range 拒绝执行（回用法提示）。
2. **注册（仅 Pro，Flash 不动）**：graph.py `operator_specialized_wrappers`
   追加 wrapper；`DEFERRING_TOOLS` 加 recall_history（pre-decision 语义：与
   Turn-Ending 动作同回合即拒绝动作）；提示词 `_PRE_DECISION_MEMORY_TOOLS`
   加名 + operator.json 在 TOOL USE PROTOCOL 第 1 类下加
   `[% if "recall_history" in available_tools %]` gated 指导段（六触发：似曾
   相识/旧精确值/两步无进展/第三次同一动作/摘要与观察冲突/用户要求回早前
   状态；明示禁投机召回；标记行=显式入口）。可用性 = `ctx.data_engine` 存在
   且 `memory.recall.enabled`——M2 的 SHA-256 golden 用 `data_engine=None`
   的 ctx 录制，故 **golden 无需重录、原样通过**；受影响的是全集字面断言
   （见要点 6）。manifest：`BACKEND_INDEPENDENT_TOOLS` 加 recall_history
   （分类完整性测试强制）。
3. **屏幕相似度本地提示**（零模型调用、零历史图入 prompt）：新
   `artemis/utils/image_hash.py` 纯 PIL 64 位 dHash（JPEG draft 快解码，
   哈希对重编码稳健故无需像素组件的 q75 对称重编码技巧）；`record_step`
   **同步**盖章 `pre/post_image_dhash` 入 extra_metadata（同步是为了避免与
   后台写线程的并发字典突变；draft 解码使成本毫秒级；post==pre 时 post 哈希
   取 pre 哈希——"无独立后图证据"但后屏即前屏）。新
   `HistoricalStateHintPromptComponent` 插在 ScreenshotSimilarity 之后（legacy
   与 transcript 两条组件清单都插）：**近 3 步任一命中即整体静默**（该 regime
   属像素级"卡在同屏"组件，两者永不同时触发）；更老步扫描封顶 500 步
   （含已 chunk 化段——`get_agent_friendly_steps` 全量），距离≤阈值取最近的
   最小距离步，注入方案原文句式提示。
4. **ContextPolicy 策略表**（新 `artemis/memory/context_policy.py`）：8 个
   生产调用点（operator/operator_cold_start/failure_analyzer/planner/
   outputter/history_analyzer/diagnoser/committee）收拢为每 agent 一条
   `ContextPolicy` 声明，字段 1:1 映射 build_plan_and_history kwargs
   （`uncompressed`=min_summaries=len(steps) 惯用法、`whitelist`=运行时传
   keep_subgoal_hashes 两个便捷位）；`build_history_for(agent, ...)` 为唯一
   入口，operator 的 last_n_detailed 构造器旋钮保留为运行时覆盖。**逐点
   golden 对拍**：`test_context_policy.py` 参数化 8 例逐字节断言 + "全表必有
   golden"覆盖断言。`agent.memory.policies` 按 agent 覆盖字段（未知字段告警
   忽略）。差异：第 9 个调用点 `mcp_server/tools/inspect_trace.py` 属离线
   审计渲染而非 agent 编译视图，未入表（保持直调）。
5. **编译视图接 chunk（§10 决策 4 收口）**：`build_plan_and_history` 增可选
   `chunks` 入参——形状无关 dict（步号范围 + 预渲染文本），chunk 块紧随
   `--- Execution History ---` 按序输出，范围内步行被替换（detailed 窗口与
   最新步永不吞）；**chunks 为 None/空时输出逐字节不变**（golden 锁定）。
   视图渲染在策略层：`full`（outputter/history_analyzer）= 持久化
   rendered_text 原样（含 ③ 全宽行）；`digest`（diagnoser/committee）=
   rendered_text 在 "③ Step action ledger" 标题处截断 + recall 标记行
   `③ Step action ledger: available via recall_history (Steps a–b)`。chunk
   仅当调用点传 engine + transcript flag 开 + 有持久化行时进入；engine 仅从
   四个辅助 agent 调用点传入，operator/planner/FA/冷启动永不接（冷启动策略
   `chunk_view=None` 显式声明）。**行为增益（实施标注）**：diagnoser/
   committee 的 strict/whitelist 悬崖在长任务中首次由段外 digest 块补偿——
   chunk 块总是全量渲染，不受步级可见性策略过滤。差异：digest 从持久化
   rendered_text 截断而非从 band1/band2 重渲染（零重复渲染逻辑；pending 态
   chunk 自然退化为 pending 说明 + 标记行）。
6. **配置**：`agent.memory.recall`{enabled=true, max_results=5,
   max_text_tokens=2000, max_image_steps=1}、transcript 增
   `similarity_hint=true` / `similarity_max_distance=8`、`memory.policies={}`。
   真机基线报告（history-baseline-2026-09-01.md）截至实施时仍不存在，相似度
   距离阈值与 recall 预算默认值**维持临时、待定标**。旧键兼容别名本阶段无
   新增需求（recall/similarity 均为全新键）。
7. **测试**（只增不减）：+45 净增——`tests/unit/tools/test_history_recall.py`
   13（关键词命中与步号不变量、step_range 过滤、范围回捞全宽行（era 入口）、
   结果数钳制、token 截断、notes 检索与步锚定、chunk 行检索、图仅单步仅真实
   文件、配置封零、缺图降级、空参拒绝、无引擎降级、可用性三态）；
   `tests/unit/utils/test_image_hash.py` 5（稳定性/JPEG 重编码小距离/异屏大
   距离/垃圾输入 best-effort/距离边界）；
   `tests/unit/agents/test_historical_state_hint.py` 8（命中注入、近 3 步
   静默、无命中/关开关/步数不足静默、平距取最近、无哈希跳过、record_step
   盖章含 post==pre 镜像）；`tests/unit/memory/test_context_policy.py` 16
   （8 点 golden 参数化、全表覆盖、配置覆盖与未知 agent、flag 关字节不变、
   full/digest 渲染、chunk_view=None 不接、最新步不被吞）；
   test_memory_config.py +2（recall/similarity/policies 默认与覆盖）。存量
   断言同步 4 处：test_prompt_assembly 全集枚举字面与 removed-tool 参数化加
   recall_history、test_operator_prompts 记忆工具枚举字面、
   test_history_analyzer patch 目标改 build_history_for、action_manifest 分类
   集。验证：tests/unit 全量 **1210 通过 3 跳过 0 失败**（M3 基线 1165+3，
   只增不减）；tests/tools 子集（operator/summarizer/validator/diagnoser）
   7 通过。
8. **未完成项（待真机/后续）**：相似度距离阈值与 recall 预算定标依赖真机
   基线报告；include_images 的图片块（ToolMessage 多模态内容）在 Gemini 工具
   响应管线的端到端表现待真机验证；Flash 注册 recall 留待其 ledger 接 chunk
   之后（方案既定）；FTS5 索引留待检索量级需要时引入。

**硬化轮 — A/B 实测问题收口** ✅ 已实施 2026-09-01（worktree `zealous-gates-4626b1` 未提交，叠加在 M0-M4 之上；任务来源 = `history-baseline-2026-09-01.md` §5/§6 的移交项）

1. **M2 冷启动 bug 认领合入 + 回归测试**：A/B 会话的最小修复（`_build_prompt_transcript`
   冷启动条件加 `not ledger.has_staged_turn`，operator.py，含注释）经核实形态正确
   完整，本轮正式认领。`test_operator_transcript.py` 的两回合用例数据构造改为
   真实形态——第 1 回合无步记录、第 2 回合 steps 非空且上一回合 staged 未
   commit（原用例 `data_engine=None` 使 steps 恒空，正因此没抓到）；负向验证
   已做：临时移除修复条件该用例即失败。
2. **chunk ①② 段头全 pending 根因（已定论）+ 修复**：worktree
   traces/data_engine.db 实证——dispatch 一环正常（21 个 chunk 全部 submit），
   断环在 LLM：**capsule 尝试 41 次全部 "503 Service Unavailable"**，5 个 job
   走到有界重试耗尽进 failed，其余 16 个卡在 `max_concurrency=1` 串行队列没轮
   到尝试就随会话结束；"下一次压缩事件收割 + flush 兜底收割落库"链路本身无缺
   陷，只是全程无就绪结果可收（21 行 DB 记录恒 version=1 pending 与此吻合）。
   根因 = `chunking.model` 默认 gemini-3.7-flash 当日全天 503，而 capsule lens
   走 `get_google_llm` 原始旁路，没有网关的分类重试/降级模型（同日 A/B 主模型
   手工换 3.6、step lens 3.5-flash-lite 均健康，唯 capsule 绑死单一端点）。
   修复：`StepCapsuleLens` 增降级模型链——`HistoryChunkManager` 从 llm 配置
   summarizer 角色的 fallback（继承全局 default.fallback，现值
   gemini-3.6-flash）解析同 provider（google）且异于主模型的降级模型，render
   经 `services.llm.with_fallback` 分类切换（503/超时类才降级，bad request 不
   掩盖进弱模型）。"短会话等不到下一次压缩事件"的假设不成立——flush 收割落库
   M3 已实现，本轮补单测锁定（迟到就绪 → flush 收割 → DB ready v2 落库、冻结
   文本不重渲染）。单测另覆盖：降级成功（主 503 → fallback 出 capsule）、无降
   级时 503 耗尽 failed 且 chunk 维持 pending、降级模型解析三态（google-only、
   不等于主模型）。
3. **视觉 lens 单图自适应（§5 修订版落地）**：新增
   `flash_summarizer_single.md` 单图变体提示词——只描述决策帧内容与动作落点
   （红标），**无 AFTER ACTION 段**，明示"缺后图仅表示无独立后验证据，严禁描
   述/推测/合成动作后状态（转换叙事由相邻步摘要链接）"，禁判定词表原样沿用；
   `_attempt` 按"前后双图 → 原转换模板、其余（Pro 恒缺后图、任一图缺失、纯文
   本）→ 单图变体"选择，单图块标签改为 DECISION FRAME（仅后图存在时亦有中性
   标签）。Pro 无动作后抓屏的刻意安全网未动（不补抓、不用步 N+1 前图拼合）。
   **回显校验兜底**：`degenerate_summary_reason`——输出含 "--- [" 段标记或以
   "---" 开头、<15 字符、>1500 字符即判该次尝试失败，走既有有界重试，耗尽进
   failed；双图模板补一句"输出永不含 --- 分隔符"降低无谓重试。单测覆盖单图/
   双图模板选择与块标签、回显判负后重试恢复、echo/过短/过长三形态耗尽 failed。
4. **视觉 lens 计量接入（M0 遗留收口）**：`record_llm_usage`/
   `SessionTokenMeter.record` 增 `update_last_prompt` 开关；视觉 lens 与
   capsule lens 的原始旁路调用成功后各记一条 `llm_usage` trace（source
   `lens:visual_transition:<model>` / `lens:step_capsule:<model>`），一律
   `update_last_prompt=False`——lens 小 prompt 不得覆写 chunker 软/硬阈值消费
   的 `last_prompt_tokens` 上下文基数；网关包装模型（RobustChatModelWrapper）
   已在出口自计量，旁路计量以 isinstance 判别避免重复。M0 实施要点第 3 条
   "raw 旁路仅测试使用"的表述至此与现状一致（旁路仍在，但已计量）。
5. **定标落地**：`memory.runtime.max_concurrency` 默认 1→2（依据基线报告
   §6：串行队列使摘要就绪 P90 长尾超擦洗窗口 K=3+宽限；lens 是独立轻模型调
   用，并发 2 风险低），Field description 已标注依据与出处；
   `similarity_max_distance=8` 维持待定标（基线未产生相似屏配对数据）。

6. **就绪门控交换（§3.3 修订落地；任务中途经同伴会话转达用户拍板补入本轮）**：
   HistoryChunkManager 事件流重构为"关段 ≠ 交换"——触发器（里程碑/尺寸/软阈
   值）只**关段**：建 chunk 镜像、落 v1 pending DB 行（recall/辅助视图可见）、
   dispatch 段头生成，原始回合原样留在转写 A 区（`_awaiting` 队列，lossless
   pending 从图片层贯彻到段层）；**交换（freeze_turns + 渲染 chunk 块）发生在
   段头就绪后的首次渲染**，且只交换就绪的连续前缀（交换消耗最旧未压回合，老
   段 pending 时后继段一并保留原文）。降级梯照 §3.3：生成失败（有界重试耗尽）
   → 原文保留 + 重投（lens 每次尝试自带 fallback 模型，见第 2 项；重投节奏限
   触发/软硬压力渲染，不逐渲染，避免重引入无界 LLM 重试）；软阈值压力下仍未
   就绪 → 依旧保留原文（正确性优先于 token）；**仅硬阈值**强制交换全部已关段
   （pending 一并入 L3 快照，③ 最小索引存活）。checker post-hoc 标注独立于段
   头（awaiting 态即刻 DB version bump），冻结文本仍只在交换事件重渲染（事件
   间字节稳定）。无步记录的关段（chunk 缺失）永不常规交换，仅硬阈值随队清
   出。CHUNK_PENDING_NOTE 增 recall_history 指引。A/B 的"21 chunk 段头全
   pending"在门控下退化为"尚未压缩"的安全形态。单测：里程碑用例改为"关段不
   交换 → 就绪后下次渲染交换"全流程、冻结区事件间字节稳定重写为门控形态、
   软压力不交换、失败重投且原文保留、硬阈值强制入 L3 等新用例；触发器/era/
   L3/寻址性用例改用 auto-resolve capsule 桩保持原有触发语义断言。
7. **段头笔记关联升级为机械校验（同伴会话转达用户拍板补入）**：
   `parse_capsule` 在 ② 区间覆盖校验之后增加笔记覆盖校验——payload 中段内
   note_writes 的去重 key 必须以子串出现在 band① 任一文本字段（doing/did/
   effect/entry/exit/四个结构化列表），缺任何一个该次尝试判负返回 None 走既
   有有界重试，耗尽维持 pending（配合门控：不交换、原文保留）；
   step_capsule.md 明示该硬约束（machine-checked）。单测：双笔记段漏一 key
   判负、补全通过、无笔记段不受影响。

   验证：tests/unit 全量 **1224 通过 3 跳过 0 失败**（M4 基线 1210+3，只增不
   减，净增 14）；tests/tools 子集（operator/summarizer/validator/diagnoser）
   7 通过。灰度前置 ①（§10 决策 3 / 基线报告 §7-3）至此满足；前置 ②（修复后
   复测一轮 A/B）仍待真机，且复测轮应同时验收门控形态（段头就绪率、交换延
   迟、A 区稳态尺寸）。

**M5 — 缓存深化（可选）** ✅ 终轮已实施 2026-09-01（worktree `zealous-gates-4626b1` 未提交，叠加在 M0-M4+硬化轮之上）
- S 区显式 CachedContent（explorer 先例外推）；`short_term_memory` 移除（姊妹文档 §3.3，转写落地后其职责已被真多轮取代）；供应商 stateful API 评估。

M5 实施要点（2026-09-01；与计划的差异逐条标注）：

1. **默认启用（用户拍板）**：`agent.memory.transcript.enabled` 默认 false→true
   （`config/agent.py` 默认值 + `config/artemis.jsonc` 新增完整 `agent.memory`
   块同步——jsonc 此前根本没有 memory 块；L2/L3 chunking 挂同一 flag 随同默认
   开）。**旧路径与转写构建异常自动回退逻辑全部保留不删——`enabled: false`
   即回滚开关**，置 false 逐字节回旧 2-message 行为（Field description 与
   jsonc 注释均已标注，含 A/B 依据摘要）。测试纪律：凡"flag 关"语义的既有
   测试改为显式传 false（`test_operator.py` 全部 21 处构造加
   `transcript_config=LEGACY_TRANSCRIPT`；`test_memory_config.py` 默认断言改
   true、显式 false 改为"回滚开关"用例），"flag 开"路径成为默认被测面。涟漪
   仅一处：`test_outputter` 的 Mock engine 需补 `get_history_chunks=[]`（辅助
   视图默认开始咨询 chunk 存储，空 chunk 时输出逐字节不变的 M4 语义未变）。
2. **short_term_memory 全链移除（姊妹文档 §3.3 落地）**：按 visibility 附录 B
   台账纪律先核销全部消费者再删——State 字段（state.py）、operator 提取/写块
   （operator.py 返回 dict 与 `<short_term_memory>` 正则提取）、
   `ShortTermMemoryPromptComponent`（prompts.py 类 + operator.py 两条组件清单
   引用）、visibility manifest operator 读/写集、operator.json 的
   Short-Term Memory 指令段（含示例块，模板不再教模型产出该标签）。
   sdk `_get_graph_state` 经核实已是 `State.initial()` 单源（Phase 0/1 收拢），
   字段删除即覆盖。**两处防御性清洗有意保留**：`task_tree.py` 渲染侧的
   `<short_term_memory>` 标签剥除与 showcase_ui markdown-parser 的标签过滤——
   历史 DB 中旧会话的思考原文仍含该标签，姊妹文档 §3.3 原文要求
   "safely ignoring it in older responses"。
3. **C 清尾（M0-M4 标注遗留小项逐项处置）**：

   | # | 遗留项 | 处置 |
   |---|---|---|
   | C1 | 旧 `compress_flash_messages`（M1 标注"M2 落地后再删"） | **已删**。语义对拍测试改造：`test_flash_scrub_edge.py` 两处 legacy 参照改为对 ScrubEdge 产物的直接字面断言；`test_flash_step_summarizer.py` 两例转为 ScrubEdge（`image_scrub_depth=1` 复现 legacy 时点，ordinal 回退键控保留覆盖），三例存档删除（pending 回退/迟到回填为 ScrubEdge 明令禁止的旧语义，前缀保留已由 scrub_edge 用例覆盖） |
   | C2 | `record_step` 的 `foreground_app` 形参从未落库（M4 发现） | **已做**。显式形参优先落 `extra_metadata["foreground_app"]`；形参缺席时从 ui_tree 的 package 属性纯字符串推导（最频繁非 systemui 包，`_derive_foreground_app`，零设备调用零同步开销）；recall `_step_haystack` 检索面接入——包名查询不再仅依赖封顶 150 步的 XML blob 扫描。测试 +4 |
   | C3 | click_sequence/swipe 多点动作语义充实（M0 差异） | **已做**。`_target_semantics` 扩展：click_sequence 逐点命中测试（`points_semantics` 列表）、swipe 打 start/end 两点（`start/end_semantics`），**首点语义提升为主 `target_*` 标签**；全程 best-effort 永不抛。测试 +2（原"swipe 跳过充实"断言按新行为改写） |
   | C4 | era 上限复用 max_chunks 同值（M3 差异） | **已做**。新键 `chunking.max_eras`（默认 None=随 max_chunks，保 M3 等值行为）；chunker `_fold_eras` 的 era 溢出改用独立上限。测试 +2（宽松 era 上限不溢出/紧上限提前溢出、config 默认与覆盖） |
   | C5 | `similarity_max_distance=8` 待定标（M4） | **已定标 8→5**。worktree traces/data_engine.db 有 460 步 dHash 盖章（16 会话）：同屏重拍（同 SHA 相邻/wait 步/同 SHA 远距重访）距离集中 ≤4；相邻步分布双峰、谷在 5-6；跨应用会话对（T1 Settings × T2 时钟，保证异屏，n=11,770）≤5 仅 0.14%（阈 8 时 0.24%，假阳翻倍）。取谷值 5：同屏召回不损、异屏假阳减半。定标证据写入 Field description |
   | C6 | M2 golden（模板拆分前 SHA-256） | **已按新现状重录一次**：operator.json 移除 Short-Term Memory 指令段（本轮 B 项唯一有意模板变更）使旧哈希失效；重录 EMPTY/SENTINEL 双哈希并在测试注释注明重录原因与时间。golden 的"任何其他漂移即 flag-off 路径不再逐字节"约束继续有效 |

4. **D1 — S 区显式 CachedContent：评估后决定不实现**。依据：A/B 实测隐式缓存
   已达 88% 调用命中/49.6% token 命中（基线 43%/29.8%），S 区字节稳定 + 转写
   append-only 正是隐式缓存的理想形态，边际收益只剩"稳态间隔太长导致隐式条目
   过期"的场景；而显式 CachedContent 要求最低 4,096 token 起步、按 token-hour
   计存储费、需要显式生命周期管理（创建/续期/删除），并把 S 区绑定到单一模型
   版本（fallback 切模型即缓存失效）。当前 Operator 步间隔 P50 8.8s 远短于隐式
   条目寿命，付费显式缓存买不到增量命中。**重启条件**（写给未来）：若出现
   ①步间隔常态 >5min 的长轮询任务（`[Loop:continuous]` 大 Interval）实测隐式
   命中率显著回落，或 ②S 区膨胀（如工具 schema 大幅增长）使单次 re-prefill
   成本可观，则按 explorer.py:1709 先例实现，config 开关默认关。
5. **D2 — Gemini stateful/Interactions API 评估备忘**：见新增 §11（只评估不
   实现）。
6. **归档**：状态行更新为已实施；§10 决策逐条标注（决策 5 维持"已按建议实施、
   待用户终审"）；两份姊妹文档头部注记补最终状态指针。

   验证：tests/unit 全量 **1229 通过 3 跳过 0 失败**（硬化轮基线 1224+3，只增
   不减，净增 5：+8 新增 −3 存档删除）；tests/tools 子集（operator/summarizer/
   validator/diagnoser）7 通过。

   **最终残留（2026-09-01 终轮验收后更新）**：~~recall include_images 真机端到
   端；最终验收轮真机三件套（>30 步 token 稳态、`[Loop:continuous]` 100+ 步、
   capsule 盲评——此轮同时验收就绪门控形态与默认开后的复测 A/B）~~ **已全部
   完成并通过**（2026-09-01 最终真机验收轮，五项全过：4 对同模型 A/B 回退 0/
   就绪率 60/60/延迟−60%/缓存 40%→86%；112 步 Loop 无堆叠、token 有界 33% 预
   算、注入逐字保全；48 chunk 盲评禁词违规 1 例且语义无害；recall 图片块 3 次
   Gemini 调用全 success；相似度提示正例+静默负例均验证——详见
   `docs/plans/history-acceptance-2026-09-01.md`）。仍留：git 提交/PR（用户决
   定，全部改动仍在 worktree `zealous-gates-4626b1` 未提交）；Flash 接
   TranscriptLedger/chunk（文档既定后续项，Flash 侧 transcript.enabled 无效果、
   零行为变化）；两项不阻塞优化候选（[Loop] 里程碑内相似度提示静默、
   gemini-3.7-flash 供应商稳定性观察，验收报告 §6-a/e）。

**决策 5 终审收尾** ✅ 已实施 2026-09-01（用户终审 2026-09-01 定案，worktree
`zealous-gates-4626b1` 未提交；叠加在 M0–M5 之上）：

1. **形态变化**：era 溢出的极限层从 M3 的"纯标记行"
   `[Era N | Steps a–b: ledger via recall_history]` 升级为 §3.3 终审定案的
   **时段段落**——
   `[Era N | Steps a–b | T+hh:mm → T+hh:mm] <一段话概括>` + 缩进 recall 指引行
   `(Step-level ledger via recall_history for steps a–b)` + user 注入行逐字保留
   （never-evict 不随丢步豁免）。起止时间取该 era 首 chunk `start_offset` 与末
   chunk `end_offset`（既有 T+ 会话偏移格式），是视频对齐的粗粒度外键。
2. **段落机械拼装，零新增 LLM 调用**（`chunking.render_era_period_paragraph`）：
   doing 取各 chunk ① 首个非空；did 逐 chunk 取首句串联（band1 缺失/pending 的
   chunk 回退用 milestone_label，无标签再退 `steps a–b`）；effect 中笔记引用
   （`notes/<key>` 路径与 `*.md` 文件名正则）合并去重；verified_facts 集合并后
   取前 4 条。全部 chunk 无 band1 时段落退化为"里程碑标签列表 + 步范围"——
   **永不为空**（新增单测覆盖两级回退）。
3. **代码位置**：`artemis/memory/chunking.py`（`RECALL_MARKER_TEMPLATE` 更名
   `RECALL_GUIDANCE_TEMPLATE` 并改为缩进指引行；`render_era_block` recall_only
   分支重写；新增 `render_era_period_paragraph`/`_first_sentence`）；recall 端
   无解析耦合（`recall_history` 的 step_range 入口本就按步号范围回捞，与标记
   行字面无关），仅同步了 `artemis/tools/history_recall.py` 文档字符串与
   `artemis/agents/operator/operator.json` 提示词中对入口行形态的描述。
4. **不变量修订落地**（§8 同步）："全层级步可寻址"单测改为 L2 chunk/era 合并/
   L3 快照层逐步可寻址 + **极限段落层时段可寻址**（正则断言 `Steps a–b` 与
   `T+..→T+..` 在场）。era 溢出用例改断言新形态（时间范围在场、段落非空、
   doing/笔记引用/verified 进段落、指引行与 user 行仍在）。
5. **验证**：tests/unit 全量 **1231 通过 3 跳过 0 失败**（M5 终轮基线 1229+3，
   只增不减，净增 2：时段段落逐 chunk 回退 + 全缺失退化两用例）。

每阶段独立可回滚；M2 起所有新行为在 flag 后面，关 flag 即回现状（M5 后 flag 出厂开，关 flag 语义不变）。

---

## 8. 验收红线（继承并加严）

> **终轮判定（2026-09-01）：六条红线全部 PASS**（"速度/质量"两条因样本量为方向性判定，"对拍"引用 M5 单测 golden）——逐条依据见 `docs/plans/history-acceptance-2026-09-01.md` §7。

- **不损伤**：压缩产物一律可经 step 引用回捞原文；chunk/快照生成失败时源摘要与 detailed 渲染回退可用（现 `task_tree.py:920` 语义保留为最后防线）。
- **步骨架不变量**：L2 chunk、era 合并、L3 快照层级后，转写中步 1:1 可寻址——每步的步号与相对时间可直接读出，无合并、无丢步、无重排；视频分析模块按步号/时间戳的对齐在压缩前后逐步一致（新增回归测试）。**唯一例外：极限时段段落层**（决策 5 终审）——允许丢步，但段落必须带步号范围与起止时间（时段可寻址），用户注入行仍逐字保留，步级细节 recall 可回捞。
- **速度**：关键路径零摘要等待、零新增同步调用；延迟回归 ≤5%（30 对任务、置信区间）。
- **缓存**：M1/M2 各自报告命中率提升（这是本方案区别于现状的核心收益，必须量化）。
- **质量**：重复动作率、历史致回滚率不升；跨计划改写的关键事实保留率上升；摘要/Validator 冲突趋零。
- **对拍**：辅助 agent 编译视图收拢前后逐字节一致；Flash M1 语义等价对拍。

---

## 9. 风险与回退

| 风险 | 缓解 |
|---|---|
| 转写路径引入回归（最大风险面） | 双路径并存 + flag；A/B 对拍先行；State/图结构不动 |
| Gemini 隐式缓存命中不及预期（条目短寿命、前缀下限） | M0 先测真实命中率；不及预期则 M5 显式缓存提前；方案收益不全押缓存（真多轮/签名连续性独立成立） |
| chunk 质量差导致远期决策劣化 | 结构化字段合并而非散文再摘要；recall 兜底；盲评（姊妹文档 §17 三方对比法沿用） |
| flash-lite 摘要质量上限 | lens 模型可配；闲时用 3.7 重摘（姊妹文档 §8.3 隔离模式保留） |
| 无界任务 chunk 无限增长 | era 二级合并 + 预算封顶 + L3 快照 |
| 转写崩溃丢失 | DataEngine 为事实源，冷启动构造器重建 F 区（A 区丢失退化为冷启动，可接受） |
| 裸坐标动作账本无语义（"click 82,32 看不出在点什么"） | M0 记录时命中测试充实 `target_text`（§6.1）；`target_label_source` 区分指名与推断 |
| 异步检查点结果在段 chunk 化之后才回果，① 的 verified/unresolved 过时 | chunk 版本化 + checkpoint harvest 对命中步范围的 chunk 追加 post-hoc 标注并 bump version；F 区文本在下一次压缩事件时才重渲染（不做即时深突变） |
| 一步内 FA 恢复动作视频轴有帧、账本无行 | ③ 缩进子行机械渲染 `_exec_*`（数据已在 interleaved_events） |
| 段内用户注入指令被压缩叙事吞掉 | ③ 逐字保全行 + never-evict（§3.3 账本细则） |
| 回合内工具循环（≤20 次迭代）在两次计量之间撑爆上下文 | 16K 单工具信封 + 回合内累计估算越硬阈值时强制决策（姊妹文档 §13 语义接入 L3 触发检查） |
| 计划写入被否决回滚造成伪里程碑切换 | 触发事实源锁定为已执行步骤盖章变化；计划事件只入队待确认（§3.3） |
| chunk 化删除携带思考签名的旧 AIMessage 影响后续轮 | 压缩点永远在完整回合边界（不劈 tool-call/response 对）；M2 真机验证项 |
| **今日 Pro 的 SystemMessage 本身含易变历史**——模板在 `# CURRENT OBSERVATION` 处切分，part 0（含 `## Current Plan & Execution History`）进系统消息（prompts.py:218-222）；不拆模板则 S 区无从稳定，缓存收益归零 | M2 前置子任务：operator.json 模板拆分——静态规则/计划语法留 S 区，plan+history 移出系统消息（姊妹文档 §12 同一要求，在此升为 M2 硬依赖） |
| 转写内时间戳字节不稳定：现 `get_relative_time` 是 "x min ago" 措辞，冻结后即变陈旧谎言，重算则破字节稳定 | ③ 账本一律用会话起点偏移 `T+mm:ss`（冻结后永真且字节稳定）；"ago" 措辞仅限辅助 agent 的每次重编译视图 |
| 极端规模下 ③ 与有限预算的数学冲突：`[Loop:continuous]` 跑到数千步时，即便最小行宽的逐步账本也会耗尽预算 | 开放决策 §10：最老 era 的 ③ 溢出到 recall-only（转写留 `Steps 1–500: ledger via recall_history` 标记行），或预算随任务放宽——需用户定溢出规则 |
| 模型路由 fallback 切换后 prompt_tokens 分词口径不一致 | 计量当近似值用，软/硬阈值留 10%+ 余量；切换事件记入计量日志 |

---

## 10. 开放决策（待用户拍板）

1. ~~**步级单 lens 修订**（§5）~~ **已拍板（2026-08-31）：采纳**。步级仅保留 `VisualTransitionLens`（双 profile 共用），Pro 步级胶囊上移为 chunk 级 `StepCapsuleLens`。原否决备选（双摘要并存、`update_step_summary` 加 lens 维度、成本 ×2）作废。
2. ~~**默认值定标**~~ **已决（2026-09-01）**：基线报告 §6 定标——`context_budget_tokens=80K`/软硬阈值/K=3/min_active=5/chunk≤12 全部维持（实测远未触及，触发行为验收留 `[Loop:continuous]` 真机轮）；`max_concurrency` 1→2（硬化轮）；`similarity_max_distance` 8→5（M5 依 460 步 dHash 分布定标，见 §7 M5 要点 3-C5）。
3. ~~**M2 灰度顺序**~~ **已决（2026-09-01）**：A/B 红线全过且大幅改善后，用户拍板 M5 将 `transcript.enabled` 提为默认开；`enabled:false` 保留为逐字节回滚开关。基线报告 §7-3 的两个前置中 ①（冷启动修复认领+回归测试）已在硬化轮满足，②（修复后复测轮）并入最终验收真机轮。
4. ~~**辅助 agent 视图升级时点**~~ **已决**：M4 收口（编译视图接 chunk digest，full/digest 双渲染，见 §7 M4 要点 5）。
5. **极端规模的 ③ 溢出规则**：~~最老 era 的 ③ 溢出到 recall-only 标记行~~ **已终审定案（用户 2026-09-01，修订原实施）**：极限层允许丢步——最老 era 压缩为"时段段落"（一段话概括 + **步号范围与起止时间必须在场** + recall 指引 + user 注入行逐字保留），段落机械拼装自 era 各 chunk ① 字段。见 §3.3 极限层小节；M3 原"纯标记行"实施按此升级（终审收尾轮落地）。

---

## 11. 供应商 stateful API 评估备忘（M5，只评估不实现）

对象：Gemini **Interactions API**（2026 年起 Google 推荐的新接口面，与本仓现用的
`generateContent` 并行提供）。其与本架构直接相关的三个特性（官方文档核实于
2026-09-01）：

- **服务端会话状态**：`store: true`（默认开）+ 后续请求带
  `previous_interaction_id` 即可让服务端续接完整对话历史，客户端无需重发消息；
  付费层交互对象保留 55 天（免费层 1 天），可配置 7/14/28/55 天窗口，可显式删除。
- **思考签名服务端化**：stateful 模式下 thought signature（Gemini 3 起的加密思考
  连续性凭据）由服务端随会话状态自动管理，客户端完全无需回传/维护——本方案
  §3.2 的"思维签名政策"（回合内强制、A 区顺带、压缩即弃）在该模式下整体消失为
  非问题。
- **显式缓存不支持**：Interactions API 只有隐式缓存；显式 `CachedContent` 仅
  `generateContent` 提供。若未来重启 D1（显式缓存），则与迁移 Interactions API
  互斥，需二选一。

### 11.1 与本架构的相容性评估

| 维度 | 现架构（M0-M5 转写） | Interactions API stateful | 评估 |
|---|---|---|---|
| 历史表示 | 客户端 TranscriptLedger 四区，字节级自主控制 | 服务端黑盒累积，客户端只发新尾部 | **根本冲突**：擦洗沿（深度 K 图→摘要替换）、L2 chunk 化、L3 快照全部依赖对历史消息的**受控深突变**——服务端状态是 append-only 黑盒，无法就地替换第 i 条消息的图片块，也无法把 12 步折叠成一个 chunk 块。要保留分层压缩就必须在压缩事件时**放弃续接、整段重建新会话**（丢 `previous_interaction_id` 换新 id），届时"免重发历史"的收益只在压缩事件之间存在 |
| 缓存 | 隐式缓存实测 88% 调用命中（A/B） | 隐式缓存由服务端在续接时自动优化 | 收益近似打平：本架构的 S 区稳定 + append-only 已把隐式缓存吃到接近上限，服务端续接省的是**上行带宽与序列化**，不是缓存未命中 |
| 思考签名 | A 区字节不动即免费有效，压缩即弃（§3.2） | 服务端全托管 | Interactions 略优，但现政策实测零签名错误（A/B 红线），痛点不存在 |
| 可逆性/审计 | DataEngine 全量在案，recall 可回捞 | 服务端 55 天留存，之后自动删除 | 现架构必须保留 DataEngine 事实源不动摇；服务端状态只能作缓存层，不可作事实源 |
| 生态 | LangChain `langchain-google-genai`（generateContent 系）+ 本仓网关（分类重试/fallback 模型链） | 需直连新 API 面；fallback 切模型时服务端会话是否可跨模型续接未验证 | 迁移面 = LLM 网关全重写 + fallback 语义重新设计，改动半径大 |
| 隐私/合规 | 请求即用即弃（模型侧） | 默认 `store: true`，设备操作史（截图、输入文本）在 Google 侧留存最长 55 天 | 移动 UI 自动化的截图流含敏感界面，默认服务端留存需要显式合规评估；`store: false` 则失去 stateful 收益，自相矛盾 |

### 11.2 结论

**不迁移，不部分采用（2026-09-01）。** 三条硬理由：① 分层压缩（本方案的核心
价值：token 稳态有界 + 步骨架不变量）与服务端黑盒状态根本不相容，保它就得在每
次压缩事件重建会话，收益归零；② 现架构在缓存命中与签名连续性上的实测表现已接
近 Interactions 能给的上限（88% 命中、零签名错误），痛点侧无增量；③ 截图流默
认服务端留存 55 天是移动自动化场景新增的合规面。

**重估触发条件**（写给未来）：① Google 宣布 `generateContent` 弃用时间表（届时
迁移变为强制，方案是"压缩事件即会话重建"的混合形态，本备忘 §11.1 第一行即设计
起点）；② Interactions API 补齐显式缓存与服务端历史编辑（可控修剪/替换）能力；
③ 出现"无压缩短会话"型新 profile（如 <15 步的 Flash 任务），可单独试点
stateful 模式免去消息重建。
