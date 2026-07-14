# 想法来源与设计动机

这个项目不是为了再做一个向量数据库，而是解决多 Agent 一起工作时记忆彼此断开的实际问题。

当一个人同时使用 Hermes Agent、Codex、OpenClaw、Claude Code、Cursor 和自研脚本时，记忆会自然分散：

- Codex 记得代码任务里的偏好。
- Hermes 记得 Hermes Studio 的配置和 Profile。
- OpenClaw 可能记得本地 gateway、inbox 或自动化路由。
- 不同电脑还有不同路径、端口、硬件和网络条件。

如果每个 Agent 各自保存记忆，时间一长就容易出现：

- 记忆重复。
- 记忆冲突。
- 旧事实污染新决策。
- 设备事实被误当成全局事实。
- 召回过程不可追踪。
- 用户不知道 agent 为什么“想起”某件事。

因此，系统采用“中心负责裁决、客户端优先保留本地状态”的混合结构：

```text
本机 Sidecar 负责接入和离线队列
中心 Gateway 负责长期记忆裁决
```

这样既不会把权限判断交给向量库，也不用让每个 Agent 重复维护一套记忆系统。

## 为什么不把 Obsidian 放进核心链路

Obsidian 很适合做人类可读的知识库和 Markdown 导出层，但不适合作为多 agent 并发写入的事实源。

原因：

- Markdown 文件不适合高并发写入。
- 冲突裁决、幂等、权限、作用域很难靠文件同步解决。
- 记忆召回需要结构化字段、embedding、时间有效期和来源追踪。

所以设计上保留 Markdown 导出能力，但不把 Obsidian 放进 v1 核心架构。

## 为什么不直接采用 Letta / LangGraph

Letta 和 LangGraph 都很优秀，但它们更像 Agent 运行时或工作流运行时。这里需要的是能跨运行时使用的共享记忆层。

系统可以接入：

- Hermes Agent
- Codex
- OpenClaw
- 其他任意支持 MCP/HTTP 的 agent

所以共享记忆系统应当作为独立基础设施，而不是被锁在某一个 Agent 框架内部。
