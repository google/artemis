# 历史模块真机基线与 M2 A/B 实测报告（2026-09-01）

状态：已完成（真机 Pixel 10 / 59100DLCR0033X，worktree `zealous-gates-4626b1` 未提交代码）

对应任务：`docs/plans/history-module-redesign.md` §7 的 M0 未完成项（基线录制）与 M2 待真机项（A/B 对拍）。

---

## TLDR

在 Pixel 10 真机上，用冻结的 3 个代表性长任务（各 2 对样本，共 6 基线 + 6 flag-on 运行，全部 Pro profile）完成了 M0 基线录制与 M2 转写路径 A/B。**结论（方向性，样本量 6 对）：§8 全部红线通过，且转写路径远超"不回归"标准——全回合延迟 P50 从 21.4s 降到 5.0s（−77%），模型调用 P50 13.8s→4.3s，缓存命中调用占比 43%→88%（token 口径 29.8%→49.6%），总输出 token −74%，任务成功率两轮均 6/6，flag-on 轮零回退、零摘要等待、零摘要 failed、无思考签名/消息格式类错误。**建议按 §10 决策 3 推进灰度，但先复测一轮确认（见"重要注意事项"）。

三个必须显著标注的事项：

1. **发现并最小修复了一个 M2 阻塞 bug**（operator.py 冷启动误判，导致转写路径从第 2 回合起永久静默回退旧路径）。首轮 flag-on 数据因此作废重跑。修复未提交，在 worktree 工作区中，需 M2/M3 会话认领合入。
2. **A/B 的处理组不是纯 M2**：worktree 中已有 M3 代码（`HistoryChunkManager` 挂同一 flag），flag-on 轮实际是"M2 转写 + M3 L2 chunking"叠加（6 会话共产生 21 个 chunk）。收益归因到 M2/M3 的拆分需要后续单独对拍。
3. **实验模型为 `gemini-3.6-flash`**（配置默认的 3.7-flash 当日对本 key 持续 503），两轮同模型可比；`config/artemis.jsonc` 已还原原文。

---

## 1. 方法与环境

- **代码**：worktree `C:\Users\wfq55\Documents\ChatGPT\artemis\.claude\worktrees\zealous-gates-4626b1`（分支 `claude/zealous-gates-4626b1`，M0+M1+M2 已实施未提交；另含 M3 chunking 代码）。以主检出 `.venv` 的 Python、worktree 为 cwd 运行 `python -m artemis.interfaces.cli.main run … --standalone`（绕开外部 Daemon，确保跑的是 worktree 代码；`import artemis` 解析与 ROOT_DIR/traces 均已验证指向 worktree）。
- **设备**：Pixel 10（59100DLCR0033X），Android 16，中文界面。`--without-video-recording-tools`，checker/committee/planner_validation 均按仓库配置关闭；两轮环境完全一致。
- **模型**：default=google:gemini-3.6-flash（原因见 TLDR #3；fallback 同 3.6）；lens=gemini-3.5-flash-lite（可用性已验证）。
- **flag**：基线轮 `agent.memory.transcript.enabled` 缺省（=false）；A/B 轮在 worktree `config/artemis.jsonc` 临时置 true，实验后已还原。
- **数据源**（worktree `traces/data_engine.db`）：
  - `llm_call` trace（DataEngineCallbackHandler）：单次模型调用 duration、消息数、token_usage（含 `input_token_details.cache_read`）、父 agent span 归属；
  - `agent` span trace：operator 回合（=全回合延迟）、planner/validator/summarizer；
  - `llm_usage` trace（M0 token_meter）：网关出口计量 + 会话累计；
  - `steps` 表：步时间戳（步间隔）、`extra_metadata.summary_status/…`（M0 版本化字段，全程在案）；
  - 摘要就绪延迟 = 视觉 lens 调用（summarizer span 派生）的结束时刻 − dispatch span 时刻（配对按 FIFO 顺序，近似值）。
- **统计口径**：P50/P90/P95 为跨会话池化分位数；分析脚本与原始输出保存在会话 scratchpad（`analyze.py`、`base-metrics.txt`、`flagon2-metrics.txt`）。
- **看门狗**：单任务 45 分钟 timeout，任务间 10s 间隔；全部任务均未触发 timeout。

## 2. 冻结任务集（Pro profile，多里程碑）

| 任务 | 目标概述 | 无副作用性 |
|---|---|---|
| T1 settings-sweep | 设置 7 个面板逐项采集（关于本机/电池/显示/声音/网络/存储/日期时间）并逐节追加到笔记，最后汇总 | 纯读 + 内部笔记 |
| T2 clock-alarms | 时钟创建 3 个闹钟（6:15/7:45/21:30）→ 逐个核对 → 关闭其一 → 逐个删除 → 确认清空 | 自建自删 |
| T3 multi-app-time | 设置读时区 → 时钟世界钟加 London/Tokyo 读时间后移除 → 日历读今日日期 → 笔记比对汇总 | 自加自删 + 纯读 |

实测步数：T1 26–45 步、T2 28–45 步均达 >30 步目标区间；T3 14–27 步偏短（多应用切换任务被 agent 高效完成，未达 30 步——记为任务设计偏差，不影响 T1/T2 承载的长历史压力）。12 次运行全部自然完成（rc=0、状态 completed），无人工干预；设备侧弹窗由 agent 自行处理。

## 3. 基线轮原始统计（flag 关，6 会话）

| 会话 | 任务 | 步数 | 墙钟 s | Operator 首调输入 tok P50/P95 | 缓存命中调用 | cached/input | 模型调用 s P50/P90 | 回合 s P50/P90 | 步间隔 s P50/P90 | 摘要就绪 s P50/P90 | 摘要 failed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| db83934e | T1 | 34 | 1415 | 11305/12204 | 23/59 | 26.6% | 22.0/32.5 | 29.2/60.2 | 33.2/63.2 | 189.2/275.3 | 0 |
| 1f229af1 | T2 | 40 | 1560 | 12107/17034 | 13/52 | 16.0% | 26.9/41.3 | 30.1/55.9 | 33.5/52.5 | 107.7/215.5 | 0 |
| e31a052d | T3 | 14 | 729 | 11646/12805 | 14/29 | 32.6% | 8.0/25.1 | 17.4/56.1 | 21.4/59.8 | 2.3/11.4 | 0 |
| f02e473f | T1 | 45 | 1375 | 15879/18558 | 72/109 | 45.3% | 6.9/23.0 | 15.3/48.5 | 18.2/52.2 | 25.5/106.3 | 0 |
| 765305ba | T2 | 39 | 1098 | 12701/14272 | 6/46 | 8.3% | 15.2/32.2 | 15.6/33.9 | 19.0/37.8 | 127.4/198.7 | 1 |
| 87b3d0e6 | T3 | 16 | 634 | 12808/13918 | 10/28 | 22.7% | 9.6/29.1 | 17.4/69.6 | 22.8/91.5 | 29.3/93.0 | 0 |

**基线聚合（这是 §3.4/§10 定标的正式基线）**：

- Operator 首调输入 token：**P50 = 12,190，P95 = 17,589**（legacy 2-message 全量重编译路径）
- Operator 缓存：命中调用 **138/323（43%）**，cached/input = **29.8%**（Gemini 隐式缓存对稳定提示头已有基础命中）
- 模型调用延迟：**P50 = 13.8s，P90 = 31.2s**；全回合：**P50 = 21.4s，P90 = 58.0s**；步间隔：**P50 = 24.8s，P90 = 62.2s**
- 摘要就绪延迟（dispatch→ready）：**P50 = 60.0s，P90 = 213.6s**（长尾主因：`max_concurrency=1` 串行队列 + lens 25s 超时重试；见 §5-d）
- 摘要 failed 率：**1/188（0.5%）**；任务成功率 6/6
- 会话级（6 会话合计）：LLM 调用 349 次，prompt 4.61M tok，completion 263.6K tok

## 4. A/B 轮（flag 开 = M2 转写 + M3 chunking）与红线对照

有效 A/B 轮（flagon2，operator.py 修复后）原始统计：

| 会话 | 任务 | 步数 | 墙钟 s | 首调输入 tok P50/P95 | 缓存命中调用 | cached/input | 模型调用 s P50/P90 | 回合 s P50/P90 | 步间隔 s P50/P90 | 摘要就绪 s P50/P90 |
|---|---|---|---|---|---|---|---|---|---|---|
| 5867d1f2 | T1 | 26 | 345 | 16158/19551 | 28/30 | 50.4% | 4.9/8.0 | 5.3/8.7 | 8.9/11.4 | 44.9/74.2 |
| bebd7329 | T2 | 28 | 480 | 15588/20225 | 26/30 | 48.0% | 4.2/40.4 | 4.4/33.6 | 8.4/37.7 | 25.3/65.5 |
| 8f9cf01b | T3 | 14 | 344 | 16174/20191 | 22/27 | 51.7% | 3.3/8.6 | 6.0/14.6 | 8.2/18.0 | 2.9/5.8 |
| fed69c93 | T1 | 39 | 608 | 15218/17104 | 44/50 | 51.7% | 5.3/21.0 | 6.7/22.8 | 9.7/26.7 | 17.3/64.0 |
| 4b426449 | T2 | 40 | 635 | 17154/21287 | 38/43 | 46.4% | 4.0/32.6 | 4.5/24.0 | 8.3/27.0 | 35.2/61.2 |
| 59a9613f | T3 | 18 | 508 | 15789/19905 | 26/29 | 50.2% | 3.2/7.8 | 4.0/13.2 | 8.1/26.7 | 82.0/330.3 |

**红线对照（方案 §8 / 姊妹文档 §18）**：

| 红线 | 基线 | flag 开 | 判定 |
|---|---|---|---|
| 模型调用延迟回归 ≤5% | P50 13.8s / P90 31.2s | **P50 4.3s（−69%）/ P90 22.8s（−27%）** | ✅ 大幅改善而非回归 |
| 全回合延迟回归 ≤5% | P50 21.4s / P90 58.0s | **P50 5.0s（−77%）/ P90 21.4s（−63%）** | ✅ 同上 |
| 缓存命中显著上升 | 43% 调用 / 29.8% token | **88% 调用 / 49.6% token** | ✅（这是本方案核心收益，已量化） |
| 关键路径零摘要等待 | —（旧路径无此概念） | 回合 P50 5.0s，无任何等待摘要的阻塞形态；dispatch span 恒 <0.1s | ✅ |
| 无思考签名/消息格式错误 | — | 12 会话日志与 log trace 扫描（signature/format/malformed/invalid message 关键词）零命中；503 重试 0–3 次/会话属供应商限流 | ✅ |
| 转写异常回退次数为零或有明确原因 | — | **修复后 6 会话 0 次**（修复前每回合都回退，原因明确且已修，见 §5-a） | ✅ |
| 任务成功率不劣化 | 6/6 | 6/6（笔记调用 14–20 次/任务、删除等收尾动作在案，完成质量抽查可信） | ✅ |

**附加观察（非红线但值得记录）**：

- 首调输入 token P50 +32%（12.2K→16.1K）：转写保留全部历史消息，符合预期；绝对值仍远低于 80K 预算。
- 总输出 token **−74%**（263.6K→68.7K）、LLM 总调用 −34%（349→231）：真多轮下模型不再每回合重新推理全局，重复思考被消除——这是延迟改善的主要来源之一（另一来源是缓存命中提高了 TTFT）。
- 墙钟：T1 1415/1375s → 345/608s；步间隔 P50 24.8s→8.8s。
- M3 chunking 实际触发：6 会话共 21 个 HistoryChunk（里程碑切换 + 尺寸边界，步区间 1:1 无丢步，最大 7 步/块）；③ 机械账本全部就位，**①② LLM 段头全部停留 pending**（后台生成未落地，属 M3 未完成面，移交 M3 会话）。

## 5. 异常与发现清单

**a.（阻塞级，已最小修复——需 M2/M3 会话认领）M2 冷启动误判 bug**：`operator.py::_build_prompt_transcript` 的冷启动检查（`ledger.turn_count == 0 and steps`）在第 2 回合必然误命中——上一回合只 stage 未 commit，`turn_count` 仍为 0 而 steps 已非空，`set_restored_history` 被账本的空账本不变量拒绝抛异常，触发灰度安全网**每回合静默回退旧路径**。实测表现：首轮 flag-on 6 会话全部 operator 首调恒为 2 条消息、每会话 ~30 条 "Transcript prompt path failed … Restored history can only seed an empty ledger" 错误日志。**首轮 flag-on 数据作废**（它实测的是"旧路径+每回合异常开销"）。最小修复：冷启动条件增加 `not ledger.has_staged_turn`（operator.py，含注释共 +8 行）；修复后 68 个相关单测全绿，验证运行零回退、消息数跨回合正常累积（2→7→11→…）。**测试缺口**：`test_operator_transcript.py` 的"两回合四区结构"用例未覆盖"第 2 回合时 steps 非空"的真实形态，建议补一例。

**b.（质量级，两轮同等存在）Pro 步记录后图恒缺 → 视觉 lens 单图退化**：Pro 的 `record_step` 中 `post_image_name` 恒为 None（步 N 的后图即步 N+1 的前图，记录时不存在独立后图），M2 SummarizerNode 从 DataEngine 取图 dispatch 时 post 恒为 None，视觉 lens 只收到前图。flash-lite 在单图输入下高频退化——直接回显分隔符原文（`--- [2] AFTER ACTION SCREEN ---`）当作摘要。实测退化率：基线 52/181（29%）、flag 开 55/159（35%）。flag 开时这些坏摘要会在擦洗沿替换截图进入 Operator 上下文，是当前转写质量的最大缺口。建议（M3 前修）：lens dispatch 延迟到下一步感知就绪后，用步 N+1 的前图充当步 N 的后图；或 lens 契约对单图输入改为"描述前屏与动作意图"并在 prompt 中显式声明无后图。

**c.（计量缺口）Flash/Pro 视觉 lens 调用不经 LLM 网关**：`VisualStepSummarizer` 用 `get_google_llm` 原始模型（`self._llm.ainvoke`），绕过 `RobustChatModelWrapper` → **无 llm_usage 计量条目**（本轮 995 条 llm_usage 全部为 google:gemini-3.6-flash，无 flash-lite 来源）。M0 实施要点第 3 条"raw 模型旁路仅测试使用"的表述与现状不符。lens 成本目前只能从回调 llm_call trace 旁证。建议 lens 切网关或单独接计量。

**d.（长尾）摘要就绪延迟远超擦洗窗口**：基线 P50 60s / P90 214s；flag 开 P50 23.6s / P90 72.9s（转写降低了主调用排队干扰后明显改善）。长尾成因：`memory.runtime.max_concurrency=1` 串行队列 + 单次 25s 超时 + 有界重试。flag 开后步间隔 P50 仅 8.8s，K=3 的擦洗沿深度 ≈26s：P50 恰好赶上，P90 会吃满宽限（3 步 ≈26s）并落占位符。对定标的含义见 §6。

**e.（环境）gemini-3.7-flash 当日持续 503**（实验前探测 5/5 失败），整个实验固定用 3.6-flash；`config/artemis.jsonc` 已按备份还原为 3.7 原文。绝对延迟数字换模型后不可直接外推，A/B 相对结论不受影响。

**f.（样本）** T3 实测 14–27 步未达 30 步设计目标；每任务 2 对的样本量下所有结论按方向性措辞，未做置信区间。§8 "30 对任务、置信区间"的正式验收仍待更大样本。

**g.（工作区）** 实验在 worktree 留下的痕迹：`.env`（从主检出复制，gitignored）、`traces/`（12+3 次运行的 db 与 trace 目录，gitignored）、operator.py 最小修复（见 a）。`config/artemis.jsonc` 已还原。基线含 1 例摘要 failed（765305ba，M0 有界重试耗尽进显式 failed 态，符合设计）。

## 6. §3.4 临时默认值定标建议（基于本轮实测，供 M3/M4 采用）

| 参数 | 临时值 | 实测证据 | 建议 |
|---|---|---|---|
| `context_budget_tokens` | 80K | 40+ 步长任务转写路径首调输入 P95 ≈ 20.4K、最大 23.4K（含活跃窗口内截图）；稳态远低于预算 | **维持 80K**。当前形态下 ~100 步内不会触及软阈值；若未来放开图片保留深度或加大 XML 保留再复标 |
| 软/硬阈值 | 0.7 / 0.9 | 本轮从未接近（≤29%），未获得触发数据 | **维持 0.7/0.9**（无证据要求调整；触发行为的验收留给 M3 的 `[Loop:continuous]` 100+ 步试跑） |
| `image_scrub_depth` | 3 | flag 开步间隔 P50 8.8s → K=3 ≈ 26s 深度；摘要就绪 P50 23.6s、P90 72.9s → P50 恰好赶上、P90 吃满宽限落占位符 | **维持 K=3**，但配套二选一：`memory.runtime.max_concurrency` 1→2（压就绪长尾，lens 是独立 flash-lite 调用，并发 2 风险低），或 `pending_grace_steps` 3→5。优先前者（占位符是信息损失，宽限只是延迟损失） |
| `min_active_steps` | 5 | 本轮 L2 触发时活跃窗口底线未被逼近（chunk 最大 7 步、上下文远未满） | **维持 5**（无反证数据） |
| chunk 触发 ≥12 步 / ≥2K token | 12 / 2000 | 21 个 chunk 实际由里程碑切换主导（1–7 步/块），尺寸兜底未成为主触发器 | **维持**。里程碑触发工作正常；尺寸兜底的检验需要单里程碑长段任务（`[Loop:continuous]`），留 M3 验收项 |

## 7. 结论与建议

1. **M0 基线已录制**（§3 表格即正式基线），M2 A/B 待真机项已完成，红线全过（方向性）。
2. **转写路径收益远超预期**：延迟 −63%~−77%、缓存命中翻倍、输出 token −74%、调用数 −34%，且成功率持平。"真多轮 + 缓存命中"的架构判断被实测强烈支持。
3. **灰度建议**：flag 具备进入默认开的候选资格，但建议满足两个前置后再翻默认：① §5-a 修复被 M2/M3 会话正式认领合入（含补测试）；② 修复后再跑一轮 A/B 确认（本轮 A/B 的处理组叠加了 M3 chunking 与 operator.py 修复，归因不纯）。
4. **移交 M3 会话**：chunk ①② 段头 pending 未落地、lens 单图退化修复（§5-b）、lens 计量接入（§5-c）、`max_concurrency` 调参（§6）。
