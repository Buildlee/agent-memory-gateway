# 从这里选择你的接入方式

`examples/` 里的文件都使用虚构的名称和路径，不含任何真实账号、地址或密钥。第一次配置时，先确定你是在做本地原型，还是接入已经部署好的共享服务；两种方式的准备工作不同。

## 先分清三个名字

| 名称 | 用来做什么 | 例子 |
|---|---|---|
| 设备 ID | 区分哪一台机器在发起请求 | `local-pc` |
| Agent 安装实例 ID | 区分这台机器上的 Codex、Hermes 或 OpenClaw | `codex-desktop` |
| 工作区 ID | 决定一组 Agent 可以共用哪一批记忆 | `shared-workspace` |

同一工作区里的 Agent 可以读取彼此已经获准共享的记忆；设备 ID 和 Agent 安装实例 ID 不要混用，也不要随意改名。

## 只在本机体验：SQLite 原型

这条路径不需要数据库服务器或容器，适合先验证写入、搜索和权限边界。

1. 生成一个随机令牌，并保存好明文值。它只在测试时放入当前终端或受保护的本机存储。

   ```powershell
   $token = [guid]::NewGuid().ToString("N")
   $bytes = [Text.Encoding]::UTF8.GetBytes($token)
   $tokenHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
   $tokenHash
   ```

2. 复制 `principals.example.json` 为 `principals.local.json`。为每个要接入的 Agent 建一条配置；将 `token_sha256` 替换为上一步的哈希，将设备、Agent 和工作区名称统一替换为自己的值。

   ```powershell
   Copy-Item .\examples\principals.example.json .\principals.local.json
   ```

   示例中的占位符不是可用哈希。少一个字符、使用大写字母或留下占位文字，Gateway 都会拒绝启动。

3. 启动 Gateway，并单独保留这个窗口。

   ```powershell
   memory-gateway --host 127.0.0.1 --port 8787 --db .\memory.db --principals-file .\principals.local.json
   ```

4. 选择 [OpenClaw HTTP 示例](openclaw-http.md) 验证一次写入和读取，或继续配置 Codex、Hermes 的 MCP 入口。

`principals.local.json`、`memory.db` 和测试令牌都不能提交到 Git。

## 接入已经部署好的共享服务：先准备 Sidecar

每台设备只运行一个 Sidecar。它保存本机的离线队列和受保护凭据，多个 Agent 通过它访问同一个 Gateway。

1. 管理端先登记设备、Agent 安装实例和工作区。生产环境使用配对或 `memory-gateway bootstrap`，不要拿 SQLite 的 `principals.local.json` 当作生产授权清单。
2. 在这台客户端机器生成 Sidecar 的 outbox 密钥。输出文件已存在时命令会拒绝覆盖；请把它保留在仅本机可读的位置。

   ```powershell
   memory-gateway sidecar-keygen --output "$env:LOCALAPPDATA\memory-gateway\secrets\pc-sidecar.env"
   ```

3. 在一个独立 PowerShell 窗口启动 Sidecar。下面的三个值必须和管理端登记的信息一致。

   ```powershell
   .\scripts\start-sidecar.ps1 `
     -GatewayUrl "https://memory-gateway.example.internal" `
     -DeviceId "local-pc" `
     -AllowedAgents "codex-desktop,hermes-desktop"
   ```

4. Sidecar 保持运行后，再把对应 MCP 配置复制到 Agent 的设置中。MCP 进程只连本机 Sidecar，不应看到 Gateway 的刷新凭据、数据库地址或证书私钥。

若 Gateway 使用内部 CA，把 CA 证书放在受保护的本机位置，再通过 `-GatewayCaCertificate` 传给启动脚本。不要关闭证书校验来绕过配置问题。

## Codex 和 Hermes 的 MCP 配置

| 使用者 | 复制的文件 | 需要改的两项 |
|---|---|---|
| Codex | [codex-mcp.json](codex-mcp.json) | `start-sidecar-mcp.ps1` 的真实路径、`codex-desktop` 的真实安装实例 ID。 |
| Hermes | [hermes-mcp.json](hermes-mcp.json) | `start-sidecar-mcp.ps1` 的真实路径、`hermes-desktop` 的真实安装实例 ID。 |

两份 JSON 都只描述怎样启动 MCP 桥接进程。替换完成后重启对应 Agent，并调用 `memory_sync_status`。如果 Sidecar 没有运行、密钥文件缺失或 Agent 安装实例 ID 未登记，MCP 会明确报错；先修正这些前置条件，不要把密钥加到 JSON 里。

## OpenClaw 的 HTTP 接入

[openclaw-http.md](openclaw-http.md) 给出了完整的本地原型请求、所需字段以及常见错误的排查方法。生产环境中，OpenClaw 的路由层应把凭据留在受保护的本机配置或 Sidecar 中，不能把固定 `Authorization` 值写进工作流定义。

## 提交代码前最后检查一次

- 只提交这些示例文件和脱敏后的说明。
- 不提交 `principals.local.json`、`.env`、证书、私钥、数据库文件或运行日志。
- 用搜索确认没有真实域名、内网地址、账号、令牌、连接串和本机绝对路径。
