# 快速上手

分两条路：先在本机跑一遍共享记忆体验，或者把已部署的 Gateway 接到真实 Agent。第一条路不需要账号、API key、容器或数据库；第二条路保留设备、工作区和凭证的安全边界。

---

## 先跑一次本地体验

需要 Python 3.10+。在 PowerShell 运行：

```powershell
git clone https://github.com/Buildlee/agent-memory-gateway.git
Set-Location agent-memory-gateway
.\scripts\setup-local-demo.ps1
```

第一次运行做三件事：

1. 在仓库下建 `.local-demo-venv`，装演示依赖。
2. 在 `%LOCALAPPDATA%\agent-memory-gateway-demo` 建临时主体配置、随机令牌和 SQLite 数据库。
3. 启动 Gateway（只监听 `127.0.0.1`），让两个模拟 Agent 完成一次写入和交叉检索。

终端输出一个对象。看这两个字段：

```text
status                : ready
cross_agent_results   : 1
```

`cross_agent_results` 大于 0 说明第二个 Agent 找到了第一个 Agent 写入的演示记忆。令牌不会打印到终端——只存在演示目录的本机文件里。脚本不碰已运行的 Codex、Hermes、Docker 或远程 Gateway。

### 停掉演示

Gateway 以后台进程继续跑。结束后，用脚本输出的 `process_id` 停掉它：

```powershell
Stop-Process -Id <process_id>
```

演示数据会保留，不自动删除。再次运行提示 `DemoHome 已存在` 时，指定一个新目录——避免覆盖令牌和数据库：

```powershell
.\scripts\setup-local-demo.ps1 `
  -DemoHome "$env:LOCALAPPDATA\agent-memory-gateway-demo-02" `
  -Port 18787
```

### 这次体验验证了什么

| 已验证 | 说明 |
|---|---|
| 共用工作区 | `demo-codex` 与 `demo-hermes` 都只能访问 `demo-workspace`。 |
| 身份匹配 | 两个 Agent 用不同随机令牌，Gateway 根据令牌哈希识别调用者。 |
| 写入与检索 | Agent1 写入一条事实，Agent2 通过搜索找回。 |
| 数据不出本机 | Gateway 只绑定 `127.0.0.1`；不调用第三方模型、向量 API 或远程数据库。 |

本地体验帮你理解工作方式。设备配对、短期令牌、加密 outbox、PostgreSQL 元数据和 HTTPS 部署属于正式服务，下一节说明怎么接入。

---

## 接入已部署的共享服务

管理员生成一次性配对码后，客户端运行一次安装向导。`-Agent` 格式：`安装实例 ID|类型|显示名`，可以重复填写多个：

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

向导提示输入配对码，然后把刷新凭证保存在 Windows Credential Manager。设备私钥、Sidecar outbox key 和本机 MCP 配置在文件已有时会跳过，不自动覆盖。首次执行还会在仓库中建 `.shared-memory-venv`，避免把 MCP 依赖装进全局 Python。

若配对成功但之后本地准备中断，用原命令加 `-UseExistingCredential` 继续。这要求原设备私钥还在，且只复用现有 Windows 凭证——不读取、不打印、不覆盖凭证，也不覆盖计划任务和 MCP JSON。

如果 Gateway 使用内部 CA，加 `-GatewayCaCertificate "<CA 证书路径>"`。公网受信任证书不需要此参数；证书不匹配时修正证书链，不要关闭 TLS 校验。

命令结束后列出生成的 MCP JSON 文件。把各自的 JSON 导入 Codex、Hermes 或其他 MCP 客户端，然后重启对应 Agent。JSON 只包含本机启动脚本、Agent ID、工作区和本机 key 文件路径，不保存 Gateway 令牌、刷新凭证、数据库地址或私钥。

Docker 中的 Agent 使用同一套身份和工作区协议，但不需要把 Windows 运行环境复制进容器。按[容器内 Agent 的统一接入](container-sidecar.md)运行 `-Mode container`——它会为目标容器建一个只监听容器回环地址的 MCP Bridge。

### 验证连接

配置后，在 Agent 中按这个顺序检查：

1. 调用 `memory_sync_status`，确认 Sidecar 在线并能识别当前 Agent。
2. 调用 `memory_remember` 写入一条测试信息（不含凭证）。
3. 用另一个已授权 Agent 调用 `memory_search` 或 `memory_context` 搜索这条信息。
4. 检查 Gateway 审计记录，确认它们属于预期工作区。

MCP 调用没带 `workspace_id` 时，系统用 `DefaultWorkspace`。没有配置时返回 `WORKSPACE_ID_REQUIRED`；设备或 Agent 不属于该工作区时返回 `WORKSPACE_FORBIDDEN`。这两个错误说明需要补齐或核对授权信息，而不是把工作区名称改成占位文本。

### 单独检查本机 Sidecar

确认计划任务是否还在运行时执行：

```powershell
.\scripts\setup-shared-memory.ps1 -Mode verify
```

只请求 `127.0.0.1` 的 Sidecar 健康接口，不读写记忆、不清 outbox、不连数据库。

---

## 常见情况

| 提示信息 | 先排查什么 |
|---|---|
| `DemoHome 已存在` | 脚本拒绝覆盖旧数据。指定新 `-DemoHome`，或确认旧演示数据是否还需要。 |
| 端口已被占用 | 指定另一个 `-Port`，例如 `18787`。 |
| 安装依赖失败 | Python 版本、网络、组织包源或 pip 配置。虚拟环境会保留，修复后直接重新运行脚本。 |
| `WORKSPACE_ID_REQUIRED` | Sidecar 和 MCP 启动参数都应提供同一个已登记工作区。 |
| `WORKSPACE_FORBIDDEN` | 管理端尚未把当前设备或 Agent 授予该工作区。 |
| `GATEWAY_UNAVAILABLE` | 本机 Sidecar 未运行、Gateway 地址不可达，或 TLS 证书链未配正确。 |
| MCP 配置已存在 | 安装向导拒绝覆盖。确认现有配置是否还在使用，再选新的 `-McpOutputDirectory`。 |
| 运行环境不完整 | `.shared-memory-venv` 已存在但缺少依赖。脚本不会自动删除它，检查原因后手动处理。 |
| 配对后安装中断 | 保留原设备私钥，用相同参数加 `-UseExistingCredential` 继续，不要再使用已失效的配对码。 |

---

## 下一步

- 需要服务端部署、迁移或上线核对 → [部署说明](deployment.md)
- 需要理解权限、审核、离线同步和检索口径 → [总体设计](design-v2.md)
- 需要看完整的 Codex、Hermes 或 OpenClaw 例子 → [接入示例](../examples/README.md)
