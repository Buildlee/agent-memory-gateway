# Agent Memory Gateway

一个面向多 Agent、多设备的共享记忆系统原型。

这个项目来自一个很直接的问题：当同一个人同时使用 Hermes Agent、Codex、OpenClaw、Claude Code、Cursor 或自研 agent 时，记忆很快会被拆散在不同电脑、不同工具、不同会话里。每个 agent 都“记得一点”，但没有一个地方能统一判断哪些记忆可靠、哪些已经过期、哪些只属于某台电脑、哪些可以跨 agent 共享。

本项目的目标是做一个通用、低配置、可自托管的共享记忆底座：

```text
Codex / Hermes Agent / OpenClaw / 其他 Agent
        |
        | MCP / HTTP
        v
本机 Memory Sidecar
        |
        | 局域网 / Tailscale / WireGuard / SSH Tunnel
        v
私有 Memory Gateway
        |
        +-- 作用域隔离
        +-- 冲突处理
        +-- 遗忘曲线
        +-- 记忆结晶
        +-- 本地旧记忆导入
        |
        +-- SQLite 原型存储
        +-- 后续可接 PostgreSQL + pgvector / Mem0 / Graphiti
```

## 为什么做这个

我们萌生这个想法，是因为多 agent 工作流里出现了几个反复问题：

- Hermes、Codex、OpenClaw 等工具各自有上下文，但长期记忆不共享。
- 多台电脑上安装 agent 后，设备路径、端口、项目状态容易混在一起。
- 旧记忆可能和新决策冲突，例如之前考虑 Obsidian 同步，后来决定不把 Obsidian 放进核心链路。
- 直接共享一个向量库太危险，缺少权限、来源、作用域、冲突退役和遗忘机制。
- 用户希望配置尽量简单：本机只接一个 MCP，中心服务放在 NAS 或内网机器上。

所以这里选择了 **Memory Gateway + Memory Sidecar + MCP/HTTP** 的组合式架构。

## 当前状态

这是一个最小可运行原型，适合用于验证架构和继续开发。

已包含：

- HTTP Memory Gateway。
- SQLite 存储。
- 简单记忆写入、搜索、上下文包生成。
- 遗忘曲线评分。
- 记忆反馈与遗忘接口。
- 本地 Sidecar outbox。
- MCP server 入口。
- 本地 Markdown / Hermes 记忆导入扫描器。

尚未包含：

- PostgreSQL + pgvector 正式后端。
- Mem0 adapter。
- Graphiti / Zep 时间图谱 adapter。
- Web UI。
- 完整权限 UI。

## 快速开始

### 1. 创建虚拟环境

```powershell
cd agent-memory-gateway
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp]"
```

### 2. 启动 Gateway

```powershell
memory-gateway --host 127.0.0.1 --port 8787 --db .\memory.db
```

健康检查：

```powershell
curl http://127.0.0.1:8787/v1/health
```

### 3. 启动 MCP Sidecar

```powershell
$env:MEMORY_GATEWAY_URL="http://127.0.0.1:8787"
memory-sidecar-mcp
```

在支持 MCP 的 agent 中，将它配置为一个 stdio MCP server。

示例见：

- `examples/codex-mcp.json`
- `examples/hermes-mcp.json`

## HTTP API

### 写入事件

```http
POST /v1/events
```

```json
{
  "content": "用户默认要求文档、报告、代码注释使用中文，除非特别说明。",
  "scope": "user",
  "kind": "preference",
  "agent_id": "codex",
  "device_id": "desktop-4090",
  "workspace_id": "HermesStudio"
}
```

### 搜索记忆

```http
POST /v1/memories/search
```

```json
{
  "query": "共享记忆系统的默认架构是什么？",
  "workspace_id": "HermesStudio",
  "limit": 8
}
```

### 获取上下文包

```http
POST /v1/context
```

```json
{
  "query": "帮我继续设计多 agent 共享记忆系统",
  "agent_id": "hermes-agent",
  "device_id": "desktop-4090",
  "workspace_id": "HermesStudio",
  "max_items": 8
}
```

## MCP Tools

MCP server 暴露以下工具：

- `memory_context`
- `memory_remember`
- `memory_search`
- `memory_feedback`
- `memory_forget`
- `memory_sync_status`

## 本地旧记忆导入

大量旧记忆不能直接写入 active memory。本项目提供导入扫描器，先进入暂存预览。

```powershell
memory-import scan --source .\some-memory-folder --batch import_2026_07_03
```

导入流程：

```text
本地已有记忆
  -> 扫描
  -> 切块
  -> 敏感信息检测
  -> 作用域推断
  -> 生成预览 JSONL
  -> 后续人工确认或批量导入
```

## 设计文档

- `docs/design.md`
- `docs/rationale.md`
- `docs/importing-existing-memory.md`

## 默认安全原则

- Gateway 默认只监听 `127.0.0.1`。
- NAS 或内网部署时，不建议直接暴露公网。
- 远程访问建议使用 Tailscale、WireGuard 或 SSH tunnel。
- API key、token、密码、私钥不会进入长期记忆。
- 当前用户指令优先于所有历史记忆。
- 不同 device/workspace/agent scope 默认隔离。

## 许可证

MIT
