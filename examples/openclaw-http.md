# OpenClaw 通过 HTTP 接入：本地原型示例

这份示例用于本机 SQLite 原型。生产环境优先让 OpenClaw 通过本机 Sidecar 或受控的内部连接访问 Gateway，不要把固定令牌写进路由规则、JSON 配置或仓库文件。

## 开始前确认

1. Gateway 已在 `http://127.0.0.1:8787` 启动。
2. `principals.local.json` 中有一条 `agent_installation_id` 为 `openclaw-desktop` 的主体配置，并且 `workspace_ids` 包含 `shared-workspace`。
3. 本次测试用的明文令牌只放在当前终端变量中；配置文件里保存的是它的 SHA-256，不是令牌本身。

## 写入一条候选记忆

先在当前 PowerShell 窗口中准备地址和令牌。不要把令牌写进 `.ps1`、JSON 或 Markdown 文件。

```powershell
$gatewayUrl = "http://127.0.0.1:8787"
$env:MEMORY_GATEWAY_TOKEN = "<仅在当前终端输入本次测试令牌>"

if ([string]::IsNullOrWhiteSpace($env:MEMORY_GATEWAY_TOKEN)) {
    throw "请先在当前终端设置 MEMORY_GATEWAY_TOKEN"
}

$headers = @{
    Authorization = "Bearer $env:MEMORY_GATEWAY_TOKEN"
    "Content-Type" = "application/json"
}

$event = @{
    event_id = [guid]::NewGuid().ToString()
    device_seq = 1
    occurred_at = [DateTime]::UtcNow.ToString("o")
    workspace_id = "shared-workspace"
    agent_id = "openclaw-desktop"
    scope = "workspace"
    kind = "fact"
    content = "OpenClaw 使用共享工作区的本机 Gateway。"
    evidence = "user_confirmed"
    confidence = 0.9
    metadata = @{
        source = "openclaw-http-example"
    }
} | ConvertTo-Json -Depth 6

Invoke-RestMethod `
    -Method Post `
    -Uri "$gatewayUrl/v1/events" `
    -Headers $headers `
    -Body $event
```

成功后会返回事件回执。重复发送同一个 `event_id` 时，系统会返回首次形成的结果，不会再写入第二条记忆。真实客户端应把 `device_seq` 持久化并单调递增，不能每次都从 1 开始。

## 读取当前工作区的上下文

```powershell
$contextRequest = @{
    workspace_id = "shared-workspace"
    agent_id = "openclaw-desktop"
    query = "当前工作区有哪些已经确认的共享信息？"
    limit = 5
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "$gatewayUrl/v1/context" `
    -Headers $headers `
    -Body $contextRequest
```

## 出错时先看哪里

| 返回错误 | 先检查什么 |
|---|---|
| `AUTH_INVALID` 或 `AUTH_REQUIRED` | 当前终端中的令牌，以及 `principals.local.json` 里对应的 SHA-256。 |
| `IDENTITY_MISMATCH` | 请求中的 `agent_id` 是否和主体配置中的 `agent_installation_id` 完全一致。 |
| `WORKSPACE_FORBIDDEN` | `workspace_id` 是否已列入该主体的 `workspace_ids`。 |
| `SENSITIVE_CONTENT` | 正文或 `metadata` 中是否带入了令牌、私钥、连接串等敏感内容。 |

测试结束后关闭这个 PowerShell 窗口，或执行 `Remove-Item Env:MEMORY_GATEWAY_TOKEN` 清掉临时令牌。
