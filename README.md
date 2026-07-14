# Agent Memory Gateway

一个可自托管的多 Agent 共享记忆底座。它让 Codex、Hermes、OpenClaw、Cursor、Claude Code 或自研 Agent 在明确授权的前提下，共用长期记忆，同时保留设备、工作区和 Agent 的边界。

## 它解决什么问题

多 Agent 工作时，长期记忆通常散落在不同工具、电脑和会话里。直接共用一个数据库又会带来三个问题：权限无法控制、过时信息无法处理、Agent 可能把不可信文本当成指令。

Agent Memory Gateway 将这件事拆开处理：

- Agent 通过 MCP 或 HTTP 写入和检索记忆；
- 本机 Sidecar 保存离线队列并管理本机凭据；
- Gateway 负责身份、授权、审计、幂等和同步；
- 记忆进入长期库前可经过敏感信息过滤、冲突审核和人工确认；
- 每次召回都先按主体和作用域过滤，不能越权读取。

```text
任意 MCP Agent
       |
       | 本机 MCP / HTTP
       v
Memory Sidecar
       |
       | HTTPS 或受保护的内网连接
       v
Memory Gateway  ---->  元数据与审计库
       |
       +------------->  长期记忆后端（可选）
```

## 已实现的能力

- HTTP Gateway：健康检查、事件写入、搜索、上下文包、反馈和遗忘接口。
- MCP 工具：上下文、写入、搜索、反馈、遗忘、同步状态、清理确认、审核和结晶重算。
- 设备配对、Ed25519 设备证明、短期访问令牌、刷新轮换和撤销 epoch。
- SQLite 本地原型，以及 PostgreSQL 元数据库、迁移检查和连接池。
- 加密事件账本、固定终态回执、重复提交幂等、跨库对账和死信重试。
- 加密 Sidecar outbox、离线 push/pull、同步游标、墓碑和近期缓存。
- 候选审核、冲突处理、补偿撤销、生命周期历史和记忆结晶。
- 敏感信息分类、拒绝指纹、命令式内容隔离和结构化引用返回。
- 可选的 PostgreSQL 长期记忆后端适配器与迁移校验。

这是一套可运行的基础设施，不是“自动把所有聊天记录存起来”的工具。是否形成长期记忆，始终应由规则和用户确认控制。

## 快速开始：本地原型

要求：Python 3.10 或更高版本。Windows 环境可直接使用 PowerShell。

```powershell
git clone https://github.com/Buildlee/agent-memory-gateway.git
Set-Location agent-memory-gateway
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp]"
```

创建一个仅在本机保存的主体配置。先生成随机令牌及其 SHA-256：

```powershell
$token = [guid]::NewGuid().ToString("N")
$bytes = [Text.Encoding]::UTF8.GetBytes($token)
$tokenHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
$tokenHash
```

将 `examples/principals.example.json` 复制为 `principals.local.json`，填入令牌哈希以及你的设备、Agent、工作区边界。该文件已被忽略，不能提交。

启动本地 Gateway：

```powershell
memory-gateway --host localhost --port 8787 --db .\memory.db --principals-file .\principals.local.json
```

另开一个终端做健康检查：

```powershell
Invoke-WebRequest http://localhost:8787/v1/health
```

SQLite 模式用于开发和演示。多设备或生产部署请使用 PostgreSQL 元数据库，并先执行显式的 `check`、`apply`、`verify` 迁移流程，详见 [部署说明](docs/deployment.md)。

## 接入 MCP Agent

先启动唯一的本机 Sidecar：

```powershell
.\scripts\start-sidecar.ps1 `
  -GatewayUrl "https://memory-gateway.example.internal" `
  -DeviceId "your-registered-device-id" `
  -AllowedAgents "your-agent-installation-id"
```

然后从 `examples/` 选择与你的使用方式相符的配置。先看 [示例使用说明](examples/README.md)，再替换项目路径和 Agent 安装实例 ID。

- Codex：`examples/codex-mcp.json`
- Hermes：`examples/hermes-mcp.json`

MCP 配置只引用启动脚本和 Agent 标识，不能保存 Gateway 令牌、刷新凭据、数据库连接串或 outbox 密钥。Sidecar 只监听本机回环地址；多个 Agent 共用一个 Sidecar，避免产生互相竞争的离线队列。

## MCP 工具

| 工具 | 用途 |
|---|---|
| `memory_context` | 按当前任务召回经过授权的上下文包 |
| `memory_remember` | 提交一条候选记忆或明确记忆 |
| `memory_search` | 搜索当前作用域内的记忆 |
| `memory_feedback` / `memory_forget` | 调整记忆质量或请求遗忘 |
| `memory_sync_status` | 查看本机同步状态 |
| `memory_cleanup_confirmed` | 用户确认后清理已同步的本地密文 |
| `memory_list_reviews` / `memory_resolve_review` / `memory_revert_review` | 审核、处理和撤销候选 |
| `memory_rebuild_crystal` | 显式重建一页结晶记忆 |

## 使用时要守住的安全规则

- 不把密码、令牌、私钥、连接串或本机凭据写入 Git、MCP 配置、日志或长期记忆。
- Gateway 不信任请求体里自称的用户、设备、Agent 或工作区；这些身份由凭据和绑定关系决定。
- 记忆先过权限过滤，再进入检索候选集。
- 不可信的命令式文本不会作为系统指令返回给 Agent。
- 本地离线队列必须加密；缺少密钥时拒绝启动，而不是退回为明文存储。
- 内网部署也使用 HTTPS。外网访问应走 VPN、零信任网络或受控隧道，不直接暴露数据库。

## 运行与验证

安装开发依赖后执行完整测试：

```powershell
pip install -e ".[mcp,postgres,dev]"
python -m unittest discover -s tests
python -m compileall -q src
```

对 PostgreSQL 或外部记忆后端执行变更前，先运行只读检查；确认后再执行迁移。迁移脚本只允许新增，不应改写已经在现场登记的版本。

## 文档

- [总体设计](docs/design-v2.md)：职责边界、数据流、安全模型和同步协议。
- [部署说明](docs/deployment.md)：本地、容器化和生产部署检查表。
- [配置示例](examples/README.md)：本地原型、Codex、Hermes 与 OpenClaw 的接入方式。
- `schema/`：元数据库与可选后端适配器的基线和增量迁移。
- `scripts/`：Sidecar 启动、MCP 接入、部署与验证脚本。

## 不提交的本地文件

`.gitignore` 已排除本机环境文件、证书和密钥、数据库文件、主体配置以及现场操作记录。请只提交示例文件和脱敏后的配置说明。

## 许可证

MIT
