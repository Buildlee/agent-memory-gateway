# 从这里选择你的接入方式

`examples/` 里的文件都使用虚构的名称和路径，不含任何真实账号、地址或密钥。第一次使用时，建议先跑一遍 [快速上手](../docs/quickstart.md) 的本地演示：它会自动验证两个模拟 Agent 的共享检索，不需要手工生成令牌哈希。

这里的示例用于两类情况：想研究本地原型的 HTTP 请求，或把真实 Codex、Hermes、OpenClaw 接到已经部署好的共享服务。真实服务的设备登记、凭据和工作区授权始终由管理端完成，示例文件只描述客户端如何连接本机 Sidecar。

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

## 接入已经部署好的共享服务：用安装向导完成本机准备

生产环境不用 SQLite 的 `principals.local.json`。管理员创建一次性配对码后，在客户端运行：

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

`-Agent` 中的三个字段依次是 Agent 安装实例 ID、类型和显示名。向导隐藏输入配对码，生成本机私钥与 Sidecar key，保存刷新凭据并启动只监听回环地址的 Sidecar。它还会生成完成替换的 MCP JSON 文件；已有 key、计划任务或 JSON 文件时停止，不会覆盖。

如果配对已成功但电脑在后续步骤中断，用同一条命令加 `-UseExistingCredential` 继续。该开关要求原设备私钥仍在，只复用已有的 Windows 凭据；不会把凭据写回终端或配置文件。

若 Gateway 使用内部 CA，把 CA 证书放在受保护的本机位置，再增加 `-GatewayCaCertificate "<本机 CA 证书路径>"`。不要关闭证书校验来绕过配置问题。

## Codex 和 Hermes 的 MCP 配置

| 使用者 | 复制的文件 | 需要改的两项 |
|---|---|---|
| Codex | 安装向导生成的 `%LOCALAPPDATA%\memory-gateway\mcp\codex-desktop-mcp.json` | 直接导入或复制其中内容，不需要填写凭据。 |
| Hermes | 安装向导生成的 `%LOCALAPPDATA%\memory-gateway\mcp\hermes-desktop-mcp.json` | 直接导入或复制其中内容，不需要填写凭据。 |

需要手动配置或研究字段时，仍可参考 [Codex 示例](codex-mcp.json) 和 [Hermes 示例](hermes-mcp.json)。两份 JSON 都只描述怎样启动 MCP 桥接进程。导入后重启对应 Agent，并调用 `memory_sync_status`。如果 Sidecar 没有运行、密钥文件缺失或 Agent 安装实例 ID 未登记，MCP 会明确报错；先修正这些前置条件，不要把密钥加到 JSON 里。

`DefaultWorkspace` 要和启动 Sidecar 时使用的值完全一致。工具没有传 `workspace_id` 时，这个值就是请求使用的工作区；它不能写成 `default` 之类的占位文本。

运行在 Docker 内的 Agent 使用同一套工作区与工具协议，不需要复制 Windows 示例。请参考[容器内 Agent 的统一接入](../docs/container-sidecar.md)：Bridge 的 MCP 地址固定为容器内的 `http://127.0.0.1:8767/mcp`，凭据仍留在受保护的状态目录。

## OpenClaw 的 HTTP 接入

[openclaw-http.md](openclaw-http.md) 给出了完整的本地原型请求、所需字段以及常见错误的排查方法。生产环境中，OpenClaw 的路由层应把凭据留在受保护的本机配置或 Sidecar 中，不能把固定 `Authorization` 值写进工作流定义。

## 提交代码前最后检查一次

- 只提交这些示例文件和脱敏后的说明。
- 不提交 `principals.local.json`、`.env`、证书、私钥、数据库文件或运行日志。
- 用搜索确认没有真实域名、内网地址、账号、令牌、连接串和本机绝对路径。
