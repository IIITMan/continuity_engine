# Continuity Engine — 可执行的 MVP 脚手架

[English](README.md) | [中文](README_zh.md)

本仓库包含 Continuity Engine v0.2 工程基线的第一个可执行实现。它是一个**确定性核心 + 显式异步适配器**，而不是一个已经成品的机器人产品。

## 已实现的内容

- 版本化的 `Event` 数据模型，带 JSON 兼容的 payload 校验。
- 有界事件路由器：受保护的控制通道、快速控制闩锁、去重、合并（coalescing）以及显式过载上报。
- 保守的、基于证据的世界模型，状态包括 `CONFIRMED`、`PROBABLE`、`UNKNOWN`、`STALE` 与 `CONFLICT`。
- 有界驱力动力学，采用精确的一阶更新。
- 意图管理器：分数边际、驻留时间、连续确认、紧急覆盖，以及最多两轮澄清的限制。
- 动作提案、本地安全检查、幂等模拟动作执行器与终态回执。
- 面向 LLM 请求的按用途（purpose）只读状态视图；不暴露任意原始快照序列化。
- LLM 网关抽象：有界并发、超时、schema 校验、过期结果处理与子任务取消。
- 基于 LLM 的对话桥：交互代际（generation）、过滤后的上下文，以及在语音提案之前拒绝过期响应。
- 显式事件 schema 迁移，以及 SQLite 快照 / 事件 / 记忆持久化。
- 记忆谱系删除，带持久化的待处理预测重建任务与 fail-closed 模型对账 API。
- 回放模式：要求使用 fixture LLM 与模拟执行器适配器，并在激活期间屏蔽常见 Python socket API。
- 确定性测试与一个可运行的端到端 demo。

## 有意未实现的内容

- 真实的云 / 本地 LLM provider 适配器。
- 真实的麦克风、摄像头、电机、扬声器或 ROS 集成。
- 物理安全认证或保证硬件急停。
- 生产级身份识别或经过校准的多传感器贝叶斯融合。
- 完整的生产级 OS 沙箱。回放模式下的 socket monkeypatching 只是纵深防御；仍应配合进程 / 容器级别的网络出站限制使用。
- 面向特定司法辖区的生产隐私政策，或跨第三方备份的完整删除管线。
- 后台调度服务。宿主应用负责调用 `step()` 并按时钟处理返回的提案；外部 I/O 保持在 `step()` 之外。

以上都是明确的适配器 / 产品集成任务，本仓库没有把它们伪装成已经可用。

## 环境要求

- Python 3.11+
- PyYAML
- pytest（仅开发）

## 安装与测试

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[test]"
pytest -q
```

## 运行 demo

```bash
python examples/demo.py
```

demo 使用 `ReplayClock`、`ReplayActionExecutor` 以及回放模式隔离。它不会调用真实的 LLM、网络服务或物理硬件。

## 最小集成模式

本地回退路径（无需任何 provider）：

```python
from continuity_engine import ConfigManager, ContinuityEngine, EventKind

config = ConfigManager.from_yaml("config/default.yaml")
engine = ContinuityEngine(config)  # 默认使用模拟执行器
try:
    engine.inject(engine.new_event(EventKind.USER_MESSAGE.value, {"text": "你好"}, source="mic"))
    step = engine.step(dt=0.0)
    for proposal in step.proposals:
        # 在宿主应用中，异步派发动作并处理 ActionResult / 终态回执。
        ...
finally:
    engine.close()
```

基于 LLM 的对话使用按会话划分的代际（generation）与显式的"动作就绪"事件：

```python
message = engine.new_event(
    EventKind.USER_MESSAGE.value,
    {"text": "你好", "interaction_id": "chat-1"}, source="mic",
)
engine.inject(message)
step = engine.step(dt=0.0)
intent = step.state.current_intent  # WAITING / awaiting_llm；此时还不会真正发声
if intent is not None:
    generation = engine.current_generation("chat-1")
    result = await engine.request_llm(
        purpose="simple_response", interaction_id="chat-1", generation=generation,
        user_input="你好", intent_id=intent.intent_id,
    )
    if result.status.value == "COMPLETED":
        accepted = engine.submit_llm_response(
            intent_id=intent.intent_id, interaction_id="chat-1", generation=generation,
            response_text=result.payload["response_text"],
        )
        if accepted:
            step = engine.step(dt=0.0)  # 针对已校验的响应返回一个 SPEAK 提案
```

宿主应用随后必须以异步方式派发返回的提案。上述示例假设已存在一个带 provider 适配器的 `LLMGateway`；本仓库有意不附带任何真实 provider 客户端。

在生产环境中，请使用显式的宿主调度器，处理取消 ID，在有序停机时持久化状态，并且绝不要让真实执行器绕过 `SafetySupervisor` 与硬件安全控制器。

## 回放保证及其局限

回放模式拒绝除具体 `ReplayLLMAdapter` 与 `ReplayActionExecutor` 类型之外的适配器，并在回放引擎的生命周期内对常见 socket API 做 monkeypatch。这适用于测试，但**不是**针对任意原生扩展或恶意进程的安全边界。如需更强的保证，请在网络隔离的测试进程 / 容器中运行回放。

## 开发命令

```bash
python -m compileall -q src
PYTHONPATH=src pytest -q
```

参见 `docs/` 中的 v0.2 产品约束、架构决策、引擎契约、LLM 集成与验收测试基线。其中的数值目标除非已在代码中经过测试，否则均为初始假设。参见 `TEST_REPORT.md` 了解当前的本地验证结果与已知缺口。
