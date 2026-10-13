# ACCEPTANCE_TESTS.md

版本：0.2.0  
状态：首版测试计划与候选阈值，需在目标硬件和真实场景中验证。

## 1. 测试层级

1. **单元测试**：算法、边界条件、状态机转移、schema、数据保留规则。
2. **集成测试**：事件路由、状态更新、策略、执行器、LLM 网关与持久化的协作。
3. **场景测试**：以可重放事件序列验证整体行为是否符合产品规则。
4. **故障注入**：网络中断、传感器错误、动作超时、存储失败、无效数值和进程重启。
5. **Soak 测试**：长时间运行、资源监测与状态演化。
6. **设备验收**：目标硬件上的时延、可靠性、音频/视觉实际误差和用户体验。

## 2. 单元测试矩阵

| 模块 | 必测用例 | 通过标准 |
|---|---|---|
| Event Router | 重复 ID、乱序、过期、队列满 | 去重与过载策略可预测；不重复执行副作用 |
| World Model | 冲突观测、过期观测、来源不可信 | 能表达 UNKNOWN/STALE/CONFLICT，不直接平均冲突置信度 |
| Drives | 固定目标收敛、强刺激、负/极大 dt、NaN | 状态有界；非法输入不提交 |
| Prediction | 冷启动、低置信输入、单次异常、纠错 | 单次异常不造成大幅模型漂移；学习来源可追溯 |
| Policy | 切换阈值、最短持续、中断、恢复、过期 | 按状态机规则转移；每次切换有原因 |
| Action Executor | success/failure/timeout/cancel/unknown | 结果状态清晰；命令发送不等于成功 |
| Memory | 冲突、重复、过期、删除、派生内容 | 可追溯、可去重；删除按产品策略传播 |
| LLM Gateway | timeout、stale result、bad schema、retry | 无效结果不能造成动作或记忆写入 |
| Safety | hard deny、临时拒绝、信息不足 | 硬拒绝无法被效用评分或 LLM 覆盖 |
| Persistence | 崩溃中断写入、版本不匹配、恢复 | 无半提交状态；恢复后先校验再开放主动行为 |

## 3. 场景测试

每个场景应保存：初始状态、事件序列、配置版本、随机种子（如有）、预期行为、不允许行为、最终快照和日志。

### S01：用户正在对话时出现普通探索刺激

- 预期：普通探索不能无理由中断对话。
- 不允许：语音反复打断、同一意图快速切换。

### S02：用户明确要求安静

- 预期：普通主动发言立即禁止；范围按 TURN/SESSION/PERSISTENT 规则生效。
- 不允许：普通驱力或 LLM 文本覆盖静默设置。

### S03：用户取消正在等待的 LLM 回答

- 预期：请求取消或标记失效；迟到结果不播放、不提交记忆、不触发工具。

### S04：同一人员检测事件重复到达

- 预期：不会重复问候、重复重置需求或重复启动同一动作。

### S05：人员观测过期

- 预期：世界状态转为 STALE/UNKNOWN；不得直接断言人员离开。

### S06：冲突身份观测

- 预期：产生冲突或待确认状态；按产品规则选择低风险行为或询问，不强行平均置信度。

### S07：动作结果为 UNKNOWN

- 预期：不记录为成功；根据动作类型等待确认、检查状态或重新规划。

### S08：LLM 超时或网络断开

- 预期：本地状态循环、停止、取消与安静控制仍工作；复杂计划暂停或降级。

### S09：非法内部状态

- 预期：候选状态不提交；非关键模块按规则隔离；关键动作不会使用损坏状态。

### S10：重启恢复

- 预期：持久偏好和关键任务经过校验后恢复；过期观测变为未知；不会虚构停机期间事件。

### S11：长时间无用户互动

- 预期：允许静默；不会不断主动说话，不通过责备或内疚话术追求互动。

### S12：记忆删除

- 预期：目标事件、索引、摘要及相关派生状态按删除策略处理，并生成可审计结果。

## 4. 候选量化门槛（BASELINE）

以下阈值是首版工程目标，需在目标设备、真实输入和明确的标注场景中验证后方可冻结。

| 指标 | 首版目标 | 测量口径 |
|---|---|---|
| 内部状态更新延迟 | P95 ≤ 10 ms | 不含 LLM、外部感知模型和硬件动作延迟 |
| 普通事件处理延迟 | P95 ≤ 100 ms | 从事件进入本地处理队列到决策可用 |
| 本地取消路径 | P95 ≤ 200 ms | 从本地控制层接受取消到取消状态生效；实体停止另行测量 |
| 重复事件造成重复副作用 | 0 次 | 在去重测试集合中统计 |
| 非法状态提交 | 0 次 | 包括 NaN、Inf、越界和 schema 错误 |
| 安静模式普通主动发言 | 0 次 | 覆盖完整静默窗口和重启恢复 |
| 30 分钟受控场景中的非必要切换 | ≤ 1 次 | 需要预先人工标注“非必要”切换定义 |
| 模拟器确定性用例 | 100% 通过 | 相同状态、输入和配置得到一致结果 |
| 模拟动作成功率 | ≥ 98% | 仅统计具备前置条件的已尝试动作；不代表真实硬件成功率 |
| 初始连续运行 | 72 小时 | 同时记录内存、CPU、队列长度、错误和状态漂移 |

P95 表示 95% 的样本不超过阈值。单项阈值不能替代真实设备的整体安全论证。对于安全相关硬件动作，必须另行制定风险分析和时限。

## 5. 故障注入测试

必须包括：

- LLM 断网、慢响应、错误结构、迟到结果。
- 麦克风/摄像头事件重复或长时间缺失。
- 事件队列达到上限。
- 数据库写入失败、磁盘空间不足、快照损坏。
- 单个模块连续产生非法数值。
- 单调时间异常输入、墙钟跳变、重启后时钟不确定。
- 取消动作未得到硬件停止确认。

通过标准：系统能进入预定义的降级状态；错误有可追踪 ID；不会把未知状态当成功；关键控制不会依赖普通 LLM 流程。

## 6. 随机化、回放与长期测试

- 至少运行多组随机种子；记录并保留失败种子。
- 同一场景要测试事件顺序变化和重复观测。
- 对每个故障案例支持事件回放。
- 长期状态测试至少覆盖 30 天模拟演化；若个性/关系机制宣称长期有效，需补充更长窗口和消融对照。
- 初期 72 小时设备 soak 测试通过后，再逐步延长运行时间。

## 7. 指标定义必须先锁定

在报告数字前，必须定义：

- “不必要意图切换”的标注标准。
- 动作成功率的分母和前置条件。
- 主动发言的统计窗口与排除类别。
- 取消延迟的起止时间点。
- 预测准确率、校准误差以及 UNKNOWN 的计入方式。
- 状态恢复成功的字段集合与允许丢失的临时状态。

不得只挑选通过的场景汇报，也不得用仿真成功率替代真实设备结果。


## 8. 高优先级架构决策验收（v0.2）

### A01：LLM 状态视图最小化
- 对每种 `purpose` 验证只能序列化 schema allowlist 字段。
- 加入任意未授权字段时，构建/验证必须失败或明确丢弃并报错；不得把完整 StateSnapshot 直接传给 adapter。
- 日志中不得泄漏被过滤的字段值。

### A02：事件 schema 迁移
- 提供至少一个旧版本事件 fixture，并验证迁移后的字段语义和版本。
- 模拟迁移中断，旧数据必须仍然可恢复；不能读取半迁移数据。
- 未知未来 major schema 必须拒绝恢复并留存原始记录。

### A03：回放模式无外部副作用
- 测试运行时拦截网络出站、真实 LLM provider 和真实 Action Executor 调用。
- 回放中任一真实 adapter 注入都必须使启动失败。
- 相同 fixture、配置、seed 和回放时钟应产生一致决策轨迹。

### A04：热配置重载
- 合法白名单参数原子切换，在下一决策边界生效。
- 无效配置整份拒绝，不得部分更新。
- 安全规则、隐私白名单、tool allowlist、schema、队列容量、存储 backend 的热重载必须被拒绝。
- 活动中的动作不因普通参数重载而被追溯修改。

### A05：记忆删除与派生状态
- 删除目标记忆后，查询、摘要、索引和缓存均不可再返回它。
- 有 lineage 的预测分区必须依据剩余样本重建。
- 无法重建时必须重置受影响分区，不得继续保留无法追溯的统计贡献。
- 删除作业部分失败时不能返回“全部删除成功”。

### A06：事件队列过载
- 普通队列满时，安全/STOP/CANCEL/MUTE 仍通过保留通道或独立 latch 生效。
- 测试过期事件丢弃、持续状态合并、低优先级事件丢弃与计数。
- 动作终态回执不得静默丢失；如果保留通道不可用，系统必须进入明确降级路径。

### A07：澄清轮次上限
- 单个 intent 最多提出 2 轮澄清问题。
- 同义问题不得重复提出；进程重启后计数仍保留。
- 超出上限后低风险任务进入安全默认/说明状态，高风险任务停止执行并说明缺失信息。
- 重新生成内部 intent ID 不得重置同一用户任务的澄清预算。

## 9. Sequential task manager (v0.4)

- A valid linear plan yields its first step as an ordinary policy candidate; no step bypasses the safety supervisor.
- Plan submissions reject unknown action types, unallowlisted observation keys, malformed arguments, duplicate step IDs, excessive step counts, oversized payloads, and unsafe retry settings.
- A `SUCCEEDED` receipt advances exactly one step; final success marks the plan `SUCCEEDED`.
- A failed step is retried only when the action type is configured retry-safe, retries remain, and the executor returns `details.retryable=true`.
- `CANCELLED`, `TIMED_OUT`, and `UNKNOWN` do not blindly replay side effects. Ambiguous step results require an explicit `TASK_RESOLVE_STEP` decision.
- `TASK_CANCEL` cancels the active plan and requests cancellation of a matching in-flight task action; `TASK_PAUSE` and `TASK_RESUME` obey their state transitions.
- A task snapshot saved while an action is running must restore as `NEEDS_RECONCILIATION`, never auto-dispatching the prior action.
- A `TASK_PLAN_SUBMITTED` plan cannot preempt safety or user-control events, and plan steps remain subject to normal intent switching and policy checks.


## 10. Sequential task manager, replan and observation feedback (v0.4)

- A definite failed step yields a stable `EngineStepResult.replan_request`; the same failure yields the same request ID until a replacement is accepted.
- A replacement plan must use `replaces_task_id` exactly matching the current definitely failed task; mismatched IDs or replacement of an unknown/active task are rejected.
- `TIMED_OUT`/`UNKNOWN` produces reconciliation, not an automatic replan or side-effect replay.
- A manual retry after reconciliation requires explicit confirmation of non-completion and is limited to one human-authorized retry per step.
- Task snapshot recovery rejects unknown action types, invalid args, duplicate step IDs, invalid/non-finite timestamps and index/status inconsistencies.
- A safety denial after task action binding but before executor entry unbinds the action and rolls the attempt counter back; no false attempt is recorded.
- Successful `OBSERVE` result accepts only a single object with `value`, `confidence`, and optional `ttl_s`, binds it to the requested key, and queues a normal allowlisted world-observation event. Extra keys, invalid confidence/TTL or non-allowlisted target produce no world update.
- Task deadline expiring while an action is running becomes `NEEDS_RECONCILIATION`; it must not be marked expired and then treated as a definite non-execution.
- Host-side replan demo must pass in replay mode without invoking a real LLM, network or hardware.


## 11. LLM task planner + provider adapter (v0.5)

- A `task_plan` view contains exactly the purpose allowlist fields; raw engine state and unapproved fields never reach the adapter.
- Candidate outputs must be linear plans with 1..max_steps entries and exactly `description`, `action_type`, `args` per step. Unsupported action types or malformed arguments are rejected before queueing.
- Final TaskManager preflight must reject unallowlisted `OBSERVE` keys; the live TaskManager validates again when the event is consumed.
- A plan-generation result becomes `QUEUED`, not `ACCEPTED`; core diagnostics/state are authoritative.
- Stable queued request IDs do not call the LLM or enqueue twice. Inflight duplicate requests return `IN_PROGRESS`; cancellation racing with event enqueue is serialized.
- Replanning requires the exact current task to be in definite `FAILED`; `UNKNOWN`/timeout is not automatically retried or replanned.
- An active task that starts while a different plan is being generated causes the stale plan to be discarded.
- The OpenAI-compatible adapter is tested only with `httpx.MockTransport`; tests verify JSON mode request construction, response parsing, authorization header behavior, and refusal of remote plaintext HTTP. No live endpoint/network is used.
- A real endpoint must be separately tested for purpose-specific output schemas, response-format support, deadlines/cancellation, provider retention policy, rate limits, and privacy approval.
