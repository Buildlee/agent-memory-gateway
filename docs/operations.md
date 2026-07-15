# 日常运维与恢复

这份说明面向已经部署 Gateway 和本机 Sidecar 的管理员。所有检查默认只读，不会重放事件、清理 outbox、修改审核结果或删除数据。

## 先看运行状态

在已安装项目、并且本机 Sidecar 正在运行的电脑上执行：

```powershell
.\scripts\check-admin-health.ps1 `
  -AgentInstallationId "your-admin-agent-id" `
  -DefaultWorkspace "your-workspace-id"
```

脚本从本机受保护的 Sidecar key 文件读取回环鉴权所需的 key，再调用 Sidecar。它不会打印 key、Gateway 令牌、刷新凭据、数据库连接串或记忆正文。

如果是在克隆的源码目录中运行，且尚未安装命令入口，脚本会只对当前仓库的 `src` 目录做本次进程内回退；不会安装依赖、改写全局环境或读取 Gateway 凭据。正式安装后的行为不变。

输出是一段 JSON：

- `ok: true` 表示 worker 心跳正常，当前没有待重试事件和未处理死信。
- `WORKER_HEARTBEAT_STALE` 表示 worker 最近一次心跳超过阈值。先检查 Gateway 和 worker 的健康状态与日志。
- `RETRYABLE_EVENTS_PRESENT` 表示有事件等待下一次重试。先确认网络、数据库和后端依赖是否可用。
- `DEAD_LETTERS_PRESENT` 表示某些事件已停止自动重试，需要人工判断原因。

退出码 `0` 是正常，`1` 是发现运行问题，`2` 表示 Sidecar、工作区配置或管理授权不可用。Windows 计划任务、监控平台或通知脚本可以据此发出提醒；不要把检查命令的输出连同环境变量写进公共日志。

## 打开本机管理页

管理页适合人工处理审核和日常排查。它只监听 `127.0.0.1`，需要本机 Sidecar 已经运行，并且当前 Agent 安装实例拥有 `memory.manage`。

```powershell
.\scripts\start-admin-console.ps1 `
  -AgentInstallationId "your-admin-agent-id" `
  -DefaultWorkspace "your-workspace-id"
```

脚本会读取本机 Sidecar key 文件，只设置访问本机 Sidecar 所需的环境变量，并清掉继承来的 Gateway 令牌、刷新凭据、设备 ID 和 CA 配置。控制台启动后会打印一个 `http://127.0.0.1:<port>/?session=...` 地址；这个地址只用于首次换取本机会话 Cookie，不能重复使用。

管理页、Sidecar 和 Gateway 要使用同一版已发布功能。若页面提示 `LOCAL_METHOD_UNSUPPORTED`，说明本机 Sidecar 仍是旧进程：先完成版本更新，再在维护窗口仅重启这一台设备的 Sidecar，随后重新运行健康检查。不要通过浏览器直连 Gateway 来绕过这个提示。

页面包含六个入口：

- 概览：待审核、待重试、死信、活跃设备、健康检查和近期活动。
- 记忆：按关键词检索当前工作区内、当前 Agent 已获授权的记忆；不会新建、删除或批量改写记忆。
- 审核：确认原文、编辑后确认、保留双方、取代冲突、拒绝和归档。
- 设备与权限：设备、Agent、工作区绑定、能力和状态，不显示公钥或凭据。
- 运行：待重试、未处理死信和只读恢复检查。
- 活动：近期管理与审核记录，不显示正文或敏感详情。

审核操作会在页面上再次确认，并携带 revision 和幂等键。管理页不提供删除、批量重放、清理 outbox 或直接改数据库的按钮；这些动作仍然需要单独受控流程。

## 找到问题后怎么查

拥有 `memory.manage` 的 Agent 可以使用下面的只读 MCP 工具：

- `memory_admin_overview` 查看审核、重试、死信和设备数量，以及 worker 心跳时间。
- `memory_admin_dead_letters` 查看未处理死信的稳定 ID、错误码、错误类别和创建时间。
- `memory_admin_audit` 查看近期操作的时间、操作者、结果码和 trace ID。
- `memory_admin_devices` 查看设备、Agent、工作区绑定和权限状态。

这些工具不会返回设备公钥、任何凭据、`details_json`、事件正文或密文。排查时先用 trace ID 和错误码关联受保护环境里的服务日志；不要把日志中的令牌、连接串或用户内容复制到 issue、聊天记录或 Git。

## 恢复顺序

worker 心跳过期时，先确认 HTTPS 入口和 Gateway 健康检查，再看 worker 日志是否有数据库连接、迁移版本或后端依赖错误。服务恢复后重新运行只读检查，确认心跳更新时间已推进。

待重试事件通常会由 worker 按既有退避策略处理。不要手工重复提交原始写入请求；先等依赖恢复，再观察数量是否下降。持续增长时，先暂停新的变更操作，保留现有审计和事件账本，定位首个错误码。

死信出现后，不要直接删除记录或清空 outbox。先确认该事件是否已经在后端产生效果，再依据审核记录、固定回执和审计记录选择受控修复流程。当前管理入口不会提供“一键重放”或批量清理按钮，避免在故障期间扩大影响。

## 恢复演练

在维护窗口或隔离环境中演练。准备可恢复的数据库备份和一条无敏感信息的测试事件后，按下面顺序检查：

1. 停止测试 worker，确认检查命令出现 `WORKER_HEARTBEAT_STALE`。
2. 恢复 worker，确认健康检查和心跳恢复，再确认没有产生新的重复事件。
3. 模拟一个可恢复的后端故障，确认事件先进入待重试状态，依赖恢复后只处理一次。
4. 记录演练日期、错误码、恢复时间和验证结果到本地操作记录。不要把现场地址、账号、密钥或日志原文提交到仓库。

演练不需要、也不应触碰真实用户内容。若必须处理生产死信，先备份并记录当前状态；涉及删除、清理或批量重放时，必须走单独审批。
