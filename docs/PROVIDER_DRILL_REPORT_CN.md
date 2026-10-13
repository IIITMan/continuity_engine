# 火山方舟 60 轮验收复跑报告（v0.25.1.dev0 修复版）· 中文

**结论：`context_continuation` 已恢复 PASS；输入 token 仍比无预算基线低 15.4%，P50/P95 延迟分别低 10.7%/15.8%，记忆召回与故障率全部保持良好。判定 17/17 全部通过。**

本报告记录上下文预算回归被修复后，在火山方舟上复跑的同一套 60 轮验收，并与前两次构建逐项对比。所有数字均来自真实供应商调用，不含估算替代；报告中只有状态、耗时、计数器与布尔结果，不含任何 prompt 或回复原文。

---

## 1. 三次运行的身份

| 项目 | A 基线（无预算） | B 预算版（有回归） | C 修复版 |
| --- | --- | --- | --- |
| 运行标识 | `b31cfa24…` | `c11a70ed…` | `c7cdb9a9…` |
| 开始时间 | 16:35:09 | 18:03:02 | **18:46:57** |
| 构建 | v0.25.0-dev（可靠性层，无上下文预算） | v0.25.0-dev（+上下文预算，有回归） | **v0.25.1.dev0（+修复）** |
| 命令 | 三次完全相同 | `--turns 60 --disable-thinking --enable-reliability` | 同左 |
| 端点 / 模型 | `ark.cn-beijing.volces.com` / `doubao-seed-2-1-pro-260915` | 同左 | 同左 |
| 预检 | 1.68 s | 2.28 s | 就绪 |
| 总耗时 | 512.6 s | 418.3 s | 440.3 s |
| 判定 | 17/17 | **16/17** | **17/17** |

> 关于「v0.24 基线」：本仓库没有 v0.24 的实测验收数据。A 是前一次 v0.25 构建的实跑记录（含可靠性层，不含上下文预算），因此对比衡量的是上下文预算这一项变更的净效果。

---

## 2. 回归是否恢复：**是**

| 探针 | A 基线 | B 有回归 | **C 修复版** |
| --- | --- | --- | --- |
| 长程上下文接续（第 10 轮说目标，第 13 轮问目标） | 正确，1 次尝试 | **失败，3 次尝试** | **正确，1 次尝试（4.20 s，64 字）** |

`context_continuation` 从 B 的失败恢复为 **PASS**，且只需 1 次尝试——与 A 基线的表现一致。

### 修复机制（你的改动）

`DialogueContextBudgeter` 做了两处针对性修改：

1. **助手消息单独设更紧的上限**（`max_assistant_message_tokens=120`，用户消息仍为 220）。助手长篇回复不再吃满整个滚动窗口，给用户陈述（对话事实的主要来源）留出空间。
2. **超限消息改为中间截断并继续向后扫描**，而非 `break` 丢弃全部更早上下文；若截断后仍放不下，则 `continue` 跳过该条继续找更旧的短事实。

离线用演练实际记录的回复长度验证（仅字符数，不用原文）：

```
修复前（B）：10 条 -> 仅保留 2 条，「马拉松」陈述被丢弃 = False
修复后（C）：10 条 -> 保留 8 条，截断 4 条助手消息（各约 119 token），「马拉松」陈述保留 = True
```

三次运行中 `CompanionMetrics` 报告的逐轮估算可变上下文也印证了这一点：B 为 P50 489 / P95 730，**C 为 P50 523 / P95 632**——保留的消息更多，同时分布更集中（助手消息被稳定裁到约 120 token）。

---

## 3. 五项重点指标

### 3.1 实际输入 token（供应商计费）

| 指标 | A 基线 | B 有回归 | **C 修复版** | C vs A | C vs B |
| --- | --- | --- | --- | --- | --- |
| 输入 token 合计 | 111,493 | 92,465 | **94,282** | **−15.4%** | +2.0% |
| 单次响应平均输入 token | 1,639.6 | 1,340.1 | **1,366.4** | **−16.7%** | +2.0% |
| 计入响应数 | 68 | 69 | 69 | — | — |

### 3.2 输出 token（供应商计费）

| 指标 | A 基线 | B 有回归 | **C 修复版** | C vs A | C vs B |
| --- | --- | --- | --- | --- | --- |
| 输出 token 合计 | 10,187 | 7,529 | **8,766** | **−13.9%** | +16.4% |
| 单次响应平均输出 token | 149.8 | 109.1 | **127.0** | **−15.2%** | +16.4% |

### 3.3 延迟（端到端单轮）

| 指标 | A 基线 | B 有回归 | **C 修复版** | C vs A | C vs B |
| --- | --- | --- | --- | --- | --- |
| **P50** | 6.590 s | 5.171 s | **5.883 s** | **−10.7%** | +13.8% |
| **P95** | 14.436 s | 11.193 s | **12.150 s** | **−15.8%** | +8.5% |
| 均值 | 7.544 s | 5.742 s | 6.478 s | −14.1% | +12.8% |
| 最大 | 17.558 s | 17.258 s | 14.889 s | −15.2% | −13.7% |

### 3.4 记忆召回正确率

| 探针 | A | B | **C** |
| --- | --- | --- | --- |
| 长期记忆召回（第 1 轮存入，第 5 轮问） | 正确，1 次 | 正确，1 次 | **正确，1 次（2.78 s）** |
| 重启后召回 | 正确，1 次 | 正确，1 次 | **正确，1 次** |
| 无实时数据不得编造 | 通过 | 通过 | **通过** |

### 3.5 故障率

| 指标 | A | B | **C** |
| --- | --- | --- | --- |
| 对话硬失败 | 0/60 | 0/60 | **0/60** |
| 对话显式降级 | 0/60 | 0/60 | **0/60** |
| 对话错误率 | 0.0 | 0.0 | **0.0** |
| 轮次通过率 | 59/60（1 轮为记忆写入） | 58/60 | **60/60** |
| 供应商传输失败 / 重试 / 熔断拒绝 | 3 / 2 / 1 | 3 / 2 / 1 | **3 / 2 / 1**（均来自注入的中断演练） |

---

## 4. 其余判定项（C 全部通过）

| 组 | 结果 |
| --- | --- |
| 输出契约 | 7/7 purpose `contract_ok=true`，**0 违规** |
| 思考模式 | `reasoning_tokens=0`，token 计账确认关闭 |
| 传输演练 | 受控重试 / 鉴权不重试 / 副作用请求不重放 / 熔断恢复 / 截止时间，5/5 通过 |
| 取消与打断 | 请求确认在途、`CancelledError`、`停止` 后 `STALE`、**无任务泄漏** |
| 重启 | 关闭重建后召回正确、历史恢复（4 → 6 条） |
| 供应商中断 | 2 轮显式降级（诚实失败，无编造），恢复后 1 次尝试即 `COMPLETED`（1.59 s） |
| 资源 | RSS 38.0–78.3 MB（均值 66.7 MB），Python 堆峰值 8.8 MB，线程 1，asyncio 任务回落至 1 |

---

## 5. 哪些可以下结论，哪些不能

**可以归因于本次变更（有机制解释 + 计费数字证实）**

- 输入 token 相对 A 降 15.4%。来源：(a) 当前用户输入不再在 `recent_dialogue` 中重复；(b) 对话/记忆有界裁剪。C 比 B 只贵 2.0%，说明修复的代价很小——因为它只是把原本被整体丢弃的旧消息重新纳入，并把助手消息裁到 120 token，总量仍然受控。
- `context_continuation` 恢复 PASS 是确定性结果：离线复现 + 3 个新增回归测试 + 实跑探针三重印证。

**方向一致但不能单独归因（n=1）**

- P50 −10.7% / P95 −15.8%（相对 A）。输入变短方向合理，但该端点延迟方差极大（同一 prompt 20 次测量跨 1.26–15.26 s），每种构建只跑 1 次，无法把延迟改善从供应商波动中分离。要下结论需每种构建 3 次以上独立运行。
- 输出 token −13.9%（相对 A）同时是行为变化（回复长度），同样受 n=1 限制。

**未回归**：记忆召回、故障率、输出契约、取消/打断、重启、中断恢复、资源占用，全部与 A 一致或更好。

---

## 6. 测试

原测试命令 `PYTHONPATH=src pytest -q --disable-warnings --maxfail=1` 首次运行时被一个**既有 Windows 平台测试**中断（`test_macos_notification_quotes_strings_for_applescript`，与本次改动无关）。完整跑完的结果：

**347 passed / 2 failed / 1 skipped**

- 2 个失败均为既有 Windows 平台测试（`osascript` 路径模拟；内联 `python -c` stdin JSON 编码），在早期 Linux 容器报告中通过，与本次改动无关。
- 1 个 skip 是 Fernet 测试的 `pytest.importorskip("cryptography")` 保护。
- 你新增的 3 个回归测试全部通过，且正对应本次根因：
  - `test_long_assistant_reply_does_not_drop_older_context_needed_for_continuation`
  - `test_over_budget_old_message_is_trimmed_and_scan_continues_to_older_short_fact`
  - `test_assistant_prose_cannot_evict_user_goal_from_recent_context`

> 附带修复：`src/continuity_engine/__init__.py` 此前被回退成了旧版本，缺少 `provider_errors` / `reliability` / `validation.provider_drill` 三条 `import *` 及对应 `__all__` 条目，导致 `tests/test_provider_reliability.py` 无法收集、`examples/real_provider_companion.py` 导入 `CircuitBreaker` 失败。已按现有代码恢复这三条导入与导出名。

---

## 7. 产物

| 文件 | 内容 |
| --- | --- |
| `data/drill_reports/ark-drill.json` | A 基线（无上下文预算） |
| `data/drill_reports/ark-drill-v025-contextbudget.json` | B 预算版（`context_continuation` 失败） |
| `data/drill_reports/ark-drill-v0251-fixed.json` | **C 修复版（17/17）** |
| `data/drill_reports/ark-drill-v0251-fixed.md` | C 的原始 Markdown 报告 |
| `examples/drill_compare.py` | 对比工具 |

英文版见 [`PROVIDER_DRILL_REPORT_EN.md`](PROVIDER_DRILL_REPORT_EN.md)；设计与边界见
[`PROVIDER_RELIABILITY.md`](PROVIDER_RELIABILITY.md)；上下文预算设计见
[`ACCEPTANCE_OPTIMIZATIONS.md`](ACCEPTANCE_OPTIMIZATIONS.md)。
