# 快速上手

这份指南分成两条路：先在本机体验共享记忆，或把已经部署好的 Gateway 接到真实 Agent。前者不需要账号、API key、容器或数据库服务器；后者保留设备、工作区和凭据的安全边界。

## 先体验一次共享记忆

准备 Python 3.10 或更高版本，然后在 PowerShell 中运行：

```powershell
git clone https://github.com/Buildlee/agent-memory-gateway.git
Set-Location agent-memory-gateway
.\scripts\setup-local-demo.ps1
```

第一次运行会完成三件事：

1. 在仓库下建立 `.local-demo-venv` 并安装演示依赖。
2. 在 `%LOCALAPPDATA%\agent-memory-gateway-demo` 创建临时主体配置、随机令牌和 SQLite 数据库。
3. 启动仅监听 `127.0.0.1` 的 Gateway，让两个模拟 Agent 完成一次写入和交叉检索。

终端会输出一个对象。下面两个字段是最重要的：

```text
status                : ready
cross_agent_results   : 1
```

`cross_agent_results` 大于 0 表示第二个 Agent 找到了第一个 Agent 写入的演示记忆。令牌不会打印到终端；它们只保存在演示目录的本机文件中。脚本不会接触已运行的 Codex、Hermes、Docker 或远程 Gateway。

### 停止演示

Gateway 会以后台进程继续运行。确认体验完成后，使用脚本输出的 `process_id` 停止这一个进程：

```powershell
Stop-Process -Id <process_id>
```

演示数据会保留，脚本不会自动删除。如果再次运行时提示 `DemoHome 已存在`，请指定一个新的目录；这是为了避免覆盖原来的令牌和数据库。

```powershell
.\scripts\setup-local-demo.ps1 `
  -DemoHome "$env:LOCALAPPDATA\agent-memory-gateway-demo-02" `
  -Port 18787
```

### 这次体验实际验证了什么

| 已验证的内容 | 说明 |
|---|---|
| 两个 Agent 共用工作区 | `demo-codex` 与 `demo-hermes` 都只获准访问 `demo-workspace`。 |
| 身份匹配 | 两个 Agent 使用不同的随机令牌，Gateway 根据令牌哈希识别调用者。 |
| 写入和检索 | 第一个 Agent 写入无敏感信息的事实，第二个 Agent 通过搜索找回它。 |
| 数据不出本机 | Gateway 只绑定 `127.0.0.1`；演示不调用第三方模型、向量 API 或远程数据库。 |

本地体验用于理解工作方式。设备配对、短期访问令牌、加密 outbox、PostgreSQL 元数据和 HTTPS 部署属于正式服务的组成部分，下一节会说明如何接入。

## 接入已经部署好的共享服务

管理员生成一次性配对码后，客户端只需要运行一次安装向导。`-Agent` 的格式是“安装实例 ID|类型|显示名”，可以重复填写多个 Agent：

```powershell
.\scripts\setup-shared-memory.ps1 `
  -Mode device `
  -GatewayUrl "https://memory-gateway.example.internal" `
  -DeviceId "local-pc" `
  -DefaultWorkspace "shared-workspace" `
  -Agent @(
    "codex-desktop|codex|Codex Desktop"
    "hermes-desktop|hermes|Hermes Desktop"
  ) `
  -InstallAutostart
```

向导会提示输入配对码，随后把刷新凭据保存在 Windows Credential Manager。设备私钥、Sidecar outbox key 和本机 MCP 配置都在已有文件时停止，不会自动覆盖。首次执行还会在仓库中建立 `.shared-memory-venv`，避免把 MCP 依赖装进全局 Python。

若配对已经成功、但随后在本机准备阶段中断，使用原命令加 `-UseExistingCredential` 继续。这个开关要求原设备私钥仍在，且只复用现有 Windows 凭据；它不读取、打印或覆盖凭据，也不覆盖计划任务和 MCP JSON。

如果 Gateway 使用内部 CA，再增加 `-GatewayCaCertificate "<本机 CA 证书路径>"`。公网受信任证书不需要这个参数；证书不匹配时应修正证书链，不要关闭 TLS 校验。

命令结束时会列出生成的 MCP JSON 文件。把各自的 JSON 导入 Codex、Hermes 或其他 MCP 客户端后，重启对应 Agent。JSON 只包含本机启动脚本、Agent ID、工作区和本机 key 文件路径，不保存 Gateway 令牌、刷新凭据、数据库地址或私钥。

Docker 中的 Agent 使用同一套身份和工作区协议，但不需要把 Windows 运行环境复制进容器。请按[容器内 Agent 的统一接入](container-sidecar.md)运行 `-Mode container`；它会为目标容器建立一个只监听容器回环地址的 MCP Bridge。

### 验证真实 Agent 的连接

配置后，在 Agent 中按这个顺序检查：

1. 调用 `memory_sync_status`，确认 Sidecar 在线并能识别当前 Agent。
2. 调用 `memory_remember` 写入一条经过确认、没有凭据的测试信息。
3. 使用另一个已授权 Agent 的 `memory_search` 或 `memory_context` 搜索这条信息。
4. 检查 Gateway 审计记录，确认它们属于预期工作区。

若 MCP 调用没有携带 `workspace_id`，系统使用 `DefaultWorkspace`。没有配置时返回 `WORKSPACE_ID_REQUIRED`；当前设备或 Agent 不属于该工作区时返回 `WORKSPACE_FORBIDDEN`。这两个错误都意味着需要补齐或核对授权信息，而不是把工作区名称改成占位文本。

### 单独检查本机 Sidecar

安装后需要确认计划任务是否仍在运行时，执行：

```powershell
.\scripts\setup-shared-memory.ps1 -Mode verify
```

它只请求 `127.0.0.1` 的 Sidecar 健康接口，不会读写记忆、清理 outbox 或连接数据库。

## 常见情况

| 看到的提示 | 先检查什么 |
|---|---|
| `DemoHome 已存在` | 脚本拒绝覆盖旧数据。指定新的 `-DemoHome`，或先人工确认旧演示数据是否还需要。 |
| 端口已被占用 | 指定另一个 `-Port`，例如 `18787`。 |
| 安装依赖失败 | Python 版本、网络、组织的包源或 pip 配置。虚拟环境会保留，修复后可直接重新运行脚本。 |
| `WORKSPACE_ID_REQUIRED` | Sidecar 和 MCP 启动参数都应提供同一个已登记工作区。 |
| `WORKSPACE_FORBIDDEN` | 管理端尚未把当前设备或 Agent 授予该工作区。 |
| `GATEWAY_UNAVAILABLE` | 本机 Sidecar 未运行、Gateway 地址不可达，或 TLS 证书链未配置正确。 |
| MCP 配置已存在 | 安装向导拒绝覆盖。先确认现有配置是否还在使用，再选择新的 `-McpOutputDirectory`。 |
| 运行环境不完整 | `.shared-memory-venv` 已存在但缺少依赖。脚本不会自动删除它；检查原因后再人工处理。 |
| 配对后安装中断 | 保留原设备私钥，用相同参数加 `-UseExistingCredential` 继续，不要再使用已经失效的配对码。 |

## 下一步

- 需要服务端部署、迁移或上线核对时，阅读 [部署说明](deployment.md)。
- 需要理解权限、审核、离线同步和检索口径时，阅读 [总体设计](design-v2.md)。
- 需要看完整的 Codex、Hermes 或 OpenClaw 例子时，阅读 [接入示例](../examples/README.md)。
