# 历史模块统一方案 v3：最终真机验收报告（2026-09-01）

状态：已完成（真机 Pixel 10 / 59100DLCR0033X，worktree `zealous-gates-4626b1` 未提交代码，M0-M5+硬化轮全量、默认开形态）

对应任务：`docs/plans/history-module-redesign.md` §7 M5 要点"最终残留"中的真机三件套 + 默认开复测 A/B + recall include_images 端到端 + 相似度提示抽查（五项）。

---

## TLDR

在 Pixel 10 真机上以出厂默认配置（`agent.memory.transcript.enabled=true`）完成五项最终验收：**全部通过（样本量有限处为方向性结论）**。要点：

1. **默认开复测 A/B（4 对任务，同日同模型 gemini-3.6-flash）**：转写路径回退 **0 次**（11 个 flag-on 会话全程在岗）；全回合延迟 P50 8.5s→3.4s（−60%）、缓存命中调用 40%→86%、输出 token −31%、成功率 4/4 vs 4/4 持平——上一轮 A/B 的收益方向在冷启动修复+就绪门控+并发 2 的最终形态下完整复现。
2. **段头就绪门控病理消失**：上一轮"21/21 chunk 段头全 pending"→ 本轮全部 flag-on 会话 **60/60 个 chunk 范围段头就绪**（就绪率 100%），关段→就绪落库（即交换时刻）中位 8–12s ≈ 1–2 回合。
3. **[Loop:continuous] 112 步长跑**：无摘要堆叠（6 era + 8 chunk 分层正常）、token 稳态有界（上下文基数峰值 26.3K = 预算 33%）、注入指令逐字保全（chunk ③ 与最终渲染提示中均在场）、release_loop 一次生效。
4. **capsule 盲评（48 个就绪段头全量机检 + 5 个人工精查）**：禁判定词违规 1/48（"entered in the search bar"，描述搜索框文字输入，语义无害）；无编造；笔记关联与 ② 区间覆盖全部忠实。
5. **recall include_images 端到端打通**：data-URL 图片块（128KB）经 ToolMessage 进入 3 次连续 Gemini 调用全部 success，Operator 消费后写出正确笔记。
6. **相似度提示**：真实重访场景正确注入（指向 Step 2/4）；同屏等待循环 60+ 步 0 次注入（近 3 步静默规则有效）。

**两条结论建议见 §8：flag 维持默认开；具备提交条件。**

必读偏差：主实验模型为 **gemini-3.6-flash**（3.7-flash 当日午间再次出现 503 风暴，见 §6-a；与 2026-09-01 基线报告同模型故纵向可比）；`chunking.model` 保持 3.7-flash 且当轮 49 个 capsule 全部由 3.7 直接服务成功。`config/artemis.jsonc` 实验后已逐字节还原。

---

## 1. 方法与环境

- **代码**：worktree `C:\Users\wfq55\Documents\ChatGPT\artemis\.claude\worktrees\zealous-gates-4626b1`（M0-M5+硬化轮已实施未提交，含 A/B 轮的 operator.py 冷启动修复）。主检出 `.venv` Python、worktree 为 cwd，`python -m artemis.interfaces.cli.main run … --standalone -s 59100DLCR0033X -n <name> --session-id <sid> --without-video-recording-tools`。
- **设备**：Pixel 10（59100DLCR0033X），Android 16，中文界面。弹窗由 agent 自行处理。
- **模型**：default=google:gemini-3.6-flash（fallback 同 3.6）；chunking=gemini-3.7-flash（capsule lens 带 3.6 降级链）；step lens=gemini-3.5-flash-lite。当日 10:40 探针三模型全部健康，11:36–11:53 出现 3.7 的 503 窗口（§6-a）。
- **flag**：A 轮/Loop/recall/sim 用出厂默认（enabled=true 即 worktree 配置原文）；B 轮临时置 false，跑完立即还原；实验结束后 `config/artemis.jsonc` 与实验前备份 diff 为空（含 default.model 还原 3.7）。
- **数据源**：worktree `traces/data_engine.db`（steps / traces：llm_call·llm_usage·log·tool / history_chunks），分析脚本在会话 scratchpad（analyze.py、swap_lag.py、dump_chunks.py、flagon/flagoff/loop_metrics.txt、chunks_dump.txt）。
- **统计口径**：P50/P90/P95 为跨会话池化分位数；"首调输入"= 每个 operator 回合的第一次 LLM 调用 input_tokens。

任务集（每任务 A/B 各一遍，同 goal 文本）：

| 任务 | 内容概述 | 步数（on / off） |
|---|---|---|
| V1 settings-sweep | 设置 7 面板巡检，逐节写笔记+汇总 | 24 / 26 |
| V2 clock-alarms | 3 闹钟创建→核对→关 1→删 3→确认清空，逐阶段写笔记 | 55 / 38 |
| V3 multi-app-time | 时区+世界时钟 3 城添加读取移除+日历+笔记 | 19 / 20 |
| V4 mega-audit | 4 阶段综合巡检（设置 4 面板+世界时钟 2 城+闹钟 2 只+汇总），逐项写笔记 | 40 / 45 |

>30 步样本：flag-on V2(55)/V4(40)，flag-off V2(38)/V4(45)；V1/V3 在转写路径下被高效完成未达 30 步（与上一轮 T3 同型的任务设计偏差，长历史压力由 V2/V4 与 112 步 Loop 承载）。8 次 A/B 运行全部自然完成（rc=0，status=completed）。

## 2. 验收项 1：默认开复测 A/B

**flag-on（A 轮，4 会话）原始统计**：

| 会话 | 任务 | 步数 | 墙钟 s | 缓存命中调用 | cached/input | 首调输入 max | 段头就绪 |
|---|---|---|---|---|---|---|---|
| aaaa2001 | V1 | 24 | 551 | 80.6% | 50.5% | 21.3K | 3/3 |
| aaaa2002 | V2 | 55 | 504 | 86.8% | 53.2% | 29.6K | 8/8 |
| aaaa2003 | V3 | 19 | 210 | 82.8% | 54.0% | 22.6K | 3/3 |
| aaaa2004 | V4 | 40 | 383 | 90.2% | 48.1% | 22.5K | 4/4 |

**flag-off（B 轮，4 会话）**：bbbb3001-4，步数 26/38/20/45，墙钟 921/501/292/583s，缓存命中调用 26.4–45.3%，cached/input 16.8–37.3%。

**池化对照（重点指标逐条）**：

| 指标 | flag-off | flag-on | Δ |
|---|---|---|---|
| 回退旧路径次数 | —（旧路径本体） | **0**（且 11 个 flag-on 会话含 Loop/recall/sim 均 0；消息数跨回合正常累积） | ✅ 冷启动修复后转写全程在岗 |
| 段头就绪率 | — | **60/60 chunk 范围就绪（100%，全部 flag-on 会话）** | ✅ 上一轮 21/21 pending 病理消失 |
| 关段→就绪落库（=交换时刻，门控语义） | — | 中位 8–12s，max 65s（≈1–2 回合；ready 版本行在收割/交换时写入） | ✅ |
| 全回合延迟 P50/P90 | 8.5 / 21.0s | **3.4 / 8.0s** | −60% / −62% |
| 模型调用 P50/P90 | 4.1 / 11.6s | **3.0 / 6.4s** | −27% / −45% |
| 步间隔 P50/P90 | 12.7 / 23.5s | **7.5 / 12.3s** | −41% / −48% |
| Operator 缓存命中调用 | 93/234（40%） | **158/184（86%）** | ×2.2（不低于上一轮 88% 的量级） |
| 首调输入 P50/P95/max | 12.5K / 14.2K / 15.3K | 17.4K / 24.8K / 28.5K | +39%（≤预算 36%，符合转写保历史预期） |
| 输出 token 合计 | 236.0K（129 步） | **163.8K（138 步）** | −31%（按步 −35%） |
| 摘要退化回显率 | 0/125 | **0/134** | ✅ 单图变体修复后≈0（两侧同零：lens 修复不在 flag 后） |
| 摘要 failed | 0 | 0 | ✅ |
| 任务成功率 | 4/4 | 4/4 | ✅ 不劣化（笔记/删除收尾均在案） |

**A 区稳态**：flag-on 首调输入曲线有界（各会话 max 21–30K，远低于 80K 预算），chunk 交换处可见 >1.5K 的下降沿（详见 §3 Loop 曲线；短任务亦有 3.7K 级下降）。

辅助证据：换模型前用出厂 3.7-flash 完成的首轮 flag-on 三会话（aaaa1001-3，24/38/18 步）同样 0 回退、8/8 chunk 就绪、全部 completed——默认配置原样形态亦被验证过一遍（数据未入池化，避免混模型）。

## 3. 验收项 2：[Loop:continuous] 112 步长跑

会话 cccc2001：goal 明确要求 [Loop:continuous] 持续监控（电池+关于手机轮询，逐轮写 loop_monitor.md）。

- **规模与稳态**：112 步 / 879s（步间隔 P50 7.4s / P90 9.6s，全程无劣化趋势）；回合 P50 3.6s / P95 6.3s。
- **token 稳态有界**：首调输入 11.3K（起）→ 26.3K（第 110 步附近，= 预算 33%），5 处 >1.5K 交换下降沿；净斜率 ≈130 tok/步，主导项为 ③ 账本逐行累积（外推软阈值 56K 需 ~340 步，之后依设计由 era ③ 溢出与 L3 接管——本轮未达该规模，未验证）。
- **无摘要堆叠**：最终渲染 F 区 = **6 个 era 块（各"merged from 1 chunks"，① 结构化字段集合并 + ② 退化标题行 + ③ 逐步账本完整保留）+ 8 个全宽 chunk 块**，无"摘要的摘要"。15/15 chunk 段头就绪。
- **步骨架不变量**：最终提示中 ③ 账本行连续覆盖 Steps 1–98（era+chunk 两层均逐步可寻址），Steps 99–112 以原始消息在 A 区（99–105 已关段就绪、按门控待下次渲染交换；106–112 活跃窗口）——任何层级无丢步、无合并。
- **[Loop] 轮询记为计划性进度**：capsule 段头以"monitoring cycles / round 12、round 13"客观记录轮次，无异常标记（中立契约的 [Loop] 判别生效）。
- **注入指令 never-evict**：第 50 步注入的格式变更指令被 agent 当轮执行（后续笔记行含 [HH:MM] 时间戳），并以 `User @ Step 50: "…"` **逐字**保全于 chunk 50-56 的 ③（pending 与 ready 两个版本行均在），且在最终渲染提示中仍逐字在场。
- **release_loop**：第 112 步注入 release 信号后一回合内停止，session completed。

## 4. 验收项 3：capsule 盲评

样本：6 个会话的 **48 个就绪 chunk**（≥10 要求的 4.8 倍）全量自动机检 + 5 个跨场景人工精查（设置巡检段、闹钟创建段、世界时钟段、Loop 注入段、recall 首段）。

- **禁判定词**：全量扫描 band①② 命中 **1/48**——aaaa2004 chunk 13-19 exit_state 中 "'Singapore' entered in the search bar"。属描述搜索框内已输入文字的可观察状态，非导航/成功判定；按字面契约计 1 次违规，语义无害。其余 47 个 chunk 零命中（successfully/completed/failed/navigated to 等全部缺席）。
- **③ 账本对照**：人工精查的 5 段中 doing/did/effect 与 ③ 逐行动作、步级视觉摘要完全对得上；verified_facts 逐条可溯源到步摘要中的屏上文字（如 IMEI 356766283081582、toast "闹钟设置为 19 小时34 分钟后"逐字进账）；无编造字段。
- **笔记要点摘录**：含笔记写入的段，effect 均点名目标文件并给出要点（如 "Appended two records to note 'settings_info_log': '电池电量: 100% (已充满)' and '设备名称: Pixel 10'"）——机械校验（缺 key 判负重生成）在真机形态下工作。
- **② 区间叙事**：抽查段全部无缝覆盖（区间并集=段范围，生成时机械校验兜底）、同质动作并组正确（如 16–19 = 添加+选时+确认一组）、异质动作单步成行；叙事与实际步骤一致。
- 值得记录的正面细节：闹钟段 unresolved 正确写"Confirming the 21:30 alarm dialog"（段末选择器仍开着）——段头对"段内未完结事"的刻画准确。

## 5. 验收项 4/5：recall include_images 与相似度提示

**recall include_images（会话 dddd3001，82 步）**：阶段 1 浏览 3 个设置页后到时钟页等待；注入"回忆关于手机页内容，用 recall_history include_images=True 回捞截图对照确认"指令。结果：

- Operator 共调 recall_history 2 次（`include_images=true`，一次带 `step_range=[3,4]`），工具返回 `[text, caption("--- pre-action screenshot of Step 4 ---"), image_url(data:image/jpeg;base64, 128KB)]` 三块结构；
- 含图 ToolMessage 进入后续 **3 次连续 Gemini 调用全部 success**（input 20.7K/22.0K/22.1K，无格式/签名错误）；
- Operator 消费后写出 recall_check.md：设备名称 Pixel 10、Android 版本、注明"来自 Step 4 截图"——**端到端 PASS**。
- 15/15 chunk 就绪、0 回退（该会话历史大部分已 chunk 化，recall 正是从冷历史回捞）。

**相似度提示（会话 dddd3002，11 步 + 交叉验证）**：

- 正例：电池→显示→声音→再回电池场景，Historical state hint 注入 10 次调用，指向 **Step 2（首次电池页）与 Step 4**，句式与方案原文一致，附 recall_history 指引；
- 静默负例：dddd3001 在时钟页等待轮询 60+ 步（同屏连续），hint **0 次**注入——近 3 步同屏静默规则有效，与像素级"卡同屏"组件未同时触发；
- 阈值 5 的行为观察：Loop 会话中 hint 在 106/112 次调用出现（轮询周期 7 步 > 静默窗口 3 步，每次重访都命中更早轮次）。成本一行文本无害，但可作为后续优化候选：活跃 [Loop] 里程碑内提示语义近于噪声（见 §6-e）。

## 6. 异常与发现清单

- **a.（环境）gemini-3.7-flash 午间 503 风暴**：11:36 起 planner/operator 连续 503 重试，首跑 acc-on-v4（aaaa1004）在 operator 180s 硬超时后 session failed（0 步入库，17 分钟耗在重试）。处置：按上一轮先例全轮切 gemini-3.6-flash 重跑（同日探针 3.6 三连 1.6–1.9s 健康），报告数据均为 3.6 同模型 A/B；`chunking.model` 维持 3.7——当轮 **49 个 capsule 全部由 3.7 直连成功**，硬化轮新加的 capsule 降级链真机上未被触发（其行为由单测覆盖，供应商窗口期的实弹验证留待自然发生）。3.7 作为出厂默认的可用性风险属供应商侧，与本方案无关，但建议留意。
- **b.（环境）设备 USB 掉线**：B 轮首次启动时设备从 adb 消失（4 个任务各 15s 内 ConnectError 退出，bbbb2001-4 弃用）；adb 重启后短暂 unauthorized、约 1 分钟内自动恢复授权，换 sid 重跑成功。对数据无污染。
- **c.（行为观察，非缺陷）"等待用户指令"被建模为 [Loop:continuous]**：acc-recall 的 goal 说"完成后在页面等待用户进一步指令"，planner 将其建模为常驻轮询循环；执行完注入指令后 agent 不自行退出（每 5–10s wait 轮询 + 逐轮更新 task_plan），需 release_loop 显式停止——与 plan grammar"[Loop] 只能被 release 信号终止"的设计一致，但此类措辞的任务会持续烧步数（本例 60+ 步等待轮询）。给使用侧的提示而非代码问题。
- **d.（口径）交换延迟的测量语义**：history_chunks 的 ready 版本行在"段头就绪后的首次渲染收割"时写库，因此 DB 上 close→ready 的 8–12s 中位数即"关段→交换"的端到端延迟（capsule LLM 生成时刻另见 lens:step_capsule llm_usage 计 49 条）；"ready 行→首个含该 chunk 块的 llm_call"恒为 0s 与该语义吻合。
- **e.（优化候选，不阻塞）** [Loop] 任务中相似度提示近乎每回合触发（106/112）；语义价值低。可选优化：活跃里程碑为 [Loop] 时静默或降频。
- **f.（样本量）** A/B 4 对 + Loop 1 + 场景 2，>30 步样本 4 个；未做置信区间，全部结论按方向性措辞。§8 红线"30 对任务、置信区间"的完整统计功课依旧未做——但两轮独立 A/B（上一轮 6 对 + 本轮 4 对）方向完全一致。
- **g.（工作区）** 实验新增 traces/（15 次运行）为 gitignored；`config/artemis.jsonc` 已还原（与实验前备份逐字节一致）；无任何 artemis/ 源码改动。

## 7. §8 验收红线逐条判定

| 红线（方案 §8） | 判定 | 依据 |
|---|---|---|
| 不损伤（压缩可逆、回捞可用、失败回退可用） | **PASS** | recall 从已 chunk 化冷历史回捞文本+图片成功消费（§5）；60/60 段头就绪故 pending 降级梯本轮未被迫使用；摘要 failed 0 |
| 步骨架不变量（任何压缩层级步 1:1 可寻址） | **PASS** | 112 步长跑最终提示：era/chunk ③ 连续覆盖 1–98、99–112 原文在场；era 合并后 ③ 逐段完整；注入指令逐字保全（§3） |
| 速度（关键路径零摘要等待；延迟回归 ≤5%） | **PASS（方向性）** | dispatch 零阻塞；全回合 P50 −60%、P90 −62%——大幅改善而非回归；样本 4 对 <30 对 |
| 缓存（命中率提升必须量化） | **PASS** | 同日同模型 A/B：命中调用 40%→86%，op cached/input 17–37%→48–54% |
| 质量（重复动作率/回滚率不升、冲突趋零） | **PASS（方向性）** | 成功率 8/8 持平；退化摘要 0/259（两轮合计）；盲评无摘要-事实冲突；重复动作率未单独统计（Loop 任务的重复为计划性，段头正确标注轮次） |
| 对拍（辅助视图收拢、Flash M1 语义等价逐字节） | **PASS（引用）** | M4/M5 golden 单测锁定（tests/unit 1229+3 全绿，2026-09-01 M5 轮验证）；本轮未重跑单测、无代码改动 |

## 8. 结论与建议

1. **flag 维持默认开（`agent.memory.transcript.enabled=true`）**。依据：本轮为冷启动修复+就绪门控+max_concurrency=2 的最终形态下、与基线轮同模型的独立复测——延迟减半、缓存命中翻倍、输出 token −31%、成功率持平、0 回退、0 退化摘要、段头就绪率 100%；上一轮遗留的三项病理（每回合静默回退、21/21 pending、单图回显）全部实测消失。`enabled:false` 回滚开关本轮实际使用（B 轮）且行为正确（0 chunk、旧 2-message 路径），回滚通道可信。
2. **具备提交条件**。M0-M5+硬化轮的全部真机验收项完成且无阻塞缺陷；worktree（含 operator.py 冷启动修复、1229+3 单测）建议按仓库纪律走提交/PR（用户决定时机）。不阻塞提交的遗留优化两项：§6-e（[Loop] 内相似度提示静默）、§6-c（"等待指令"类措辞的使用侧文档提示）；另建议对 3.7-flash 的供应商稳定性保持观察（§6-a）。
