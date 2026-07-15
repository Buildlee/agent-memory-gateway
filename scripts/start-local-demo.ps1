[CmdletBinding()]
param(
    [string]$DemoHome = "$env:LOCALAPPDATA\agent-memory-gateway-demo",

    [ValidateRange(1024, 65535)]
    [int]$Port = 8787,

    [string]$PythonExecutable = "python",

    [bool]$RunVerification = $true
)

$ErrorActionPreference = "Stop"

function Get-TokenHash([string]$Token) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Token)
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}

function New-LocalToken {
    [byte[]]$bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Write-LocalJson([string]$Path, [object]$Value) {
    $json = $Value | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}

$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$demoRoot = [IO.Path]::GetFullPath($DemoHome)
$projectPrefix = $projectRoot.TrimEnd("\") + "\"
if ($demoRoot.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "DemoHome 不能位于仓库内，避免把本机令牌和数据库放进 Git 工作区。"
}
if (Test-Path -LiteralPath $demoRoot) {
    throw "DemoHome 已存在，拒绝覆盖：$demoRoot。请保留现有演示数据，或改用新的目录。"
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "本机端口 $Port 已被占用，请换一个端口。"
}

$python = Get-Command $PythonExecutable -ErrorAction Stop
if ($python.CommandType -notin @("Application", "ExternalScript")) {
    throw "PythonExecutable 必须是可执行文件：$PythonExecutable"
}

[void][IO.Directory]::CreateDirectory($demoRoot)
$configDirectory = Join-Path $demoRoot "config"
$dataDirectory = Join-Path $demoRoot "data"
$logDirectory = Join-Path $demoRoot "logs"
foreach ($directory in @($configDirectory, $dataDirectory, $logDirectory)) {
    [void][IO.Directory]::CreateDirectory($directory)
}

$gatewayUrl = "http://127.0.0.1:$Port"
$workspaceId = "demo-workspace"
$deviceId = "demo-local-pc"
$codexAgentId = "demo-codex"
$hermesAgentId = "demo-hermes"
$capabilities = @(
    "memory.read_context",
    "memory.search",
    "memory.write_event",
    "memory.feedback",
    "memory.forget"
)
$codexToken = New-LocalToken
$hermesToken = New-LocalToken
$principalsPath = Join-Path $configDirectory "principals.local.json"
$credentialsPath = Join-Path $configDirectory "demo-credentials.local.json"
$databasePath = Join-Path $dataDirectory "memory.db"

Write-LocalJson $principalsPath @{
    principals = @(
        @{
            token_sha256 = Get-TokenHash $codexToken
            tenant_id = "demo"
            user_id = "demo-user"
            device_id = $deviceId
            agent_installation_id = $codexAgentId
            workspace_ids = @($workspaceId)
            capabilities = $capabilities
        },
        @{
            token_sha256 = Get-TokenHash $hermesToken
            tenant_id = "demo"
            user_id = "demo-user"
            device_id = $deviceId
            agent_installation_id = $hermesAgentId
            workspace_ids = @($workspaceId)
            capabilities = $capabilities
        }
    )
}
Write-LocalJson $credentialsPath @{
    gateway_url = $gatewayUrl
    workspace_id = $workspaceId
    device_id = $deviceId
    codex_agent_id = $codexAgentId
    hermes_agent_id = $hermesAgentId
    codex_token = $codexToken
    hermes_token = $hermesToken
    next_device_seq = 1
}

$stdoutPath = Join-Path $logDirectory "gateway.stdout.log"
$stderrPath = Join-Path $logDirectory "gateway.stderr.log"
$gatewayProcess = Start-Process `
    -FilePath $python.Source `
    -ArgumentList @(
        "-m", "agent_memory_gateway.gateway",
        "--host", "127.0.0.1",
        "--port", "$Port",
        "--db", $databasePath,
        "--principals-file", $principalsPath
    ) `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -WindowStyle Hidden `
    -PassThru

$deadline = (Get-Date).AddSeconds(20)
$health = $null
do {
    try {
        $health = Invoke-RestMethod -Method Get -Uri "$gatewayUrl/v1/health" -TimeoutSec 2
        break
    } catch {
        if ($gatewayProcess.HasExited) {
            throw "本地 Gateway 提前退出。请查看日志：$stderrPath"
        }
        Start-Sleep -Milliseconds 500
    }
} while ((Get-Date) -lt $deadline)
if ($null -eq $health) {
    throw "Gateway 在 20 秒内没有通过健康检查。请查看日志：$stderrPath"
}

$verification = $null
if ($RunVerification) {
    $credentials = Get-Content -LiteralPath $credentialsPath -Raw | ConvertFrom-Json
    $sequence = [int]$credentials.next_device_seq
    $headers = @{
        Authorization = "Bearer $($credentials.codex_token)"
        "Content-Type" = "application/json"
    }
    $event = @{
        event_id = [guid]::NewGuid().ToString()
        device_seq = $sequence
        occurred_at = [DateTime]::UtcNow.ToString("o")
        workspace_id = $workspaceId
        agent_id = $codexAgentId
        scope = "workspace"
        kind = "fact"
        content = "本地演示：认证访问令牌的有效期设为 15 分钟。"
        evidence = "user_confirmed"
        confidence = 0.9
        metadata = @{ source = "local-demo" }
    } | ConvertTo-Json -Depth 6
    $receipt = Invoke-RestMethod -Method Post -Uri "$gatewayUrl/v1/events" -Headers $headers -Body $event

    $searchHeaders = @{
        Authorization = "Bearer $($credentials.hermes_token)"
        "Content-Type" = "application/json"
    }
    $searchRequest = @{
        workspace_id = $workspaceId
        agent_id = $hermesAgentId
        query = "认证令牌有效期"
        limit = 3
    } | ConvertTo-Json
    $search = Invoke-RestMethod `
        -Method Post `
        -Uri "$gatewayUrl/v1/memories/search" `
        -Headers $searchHeaders `
        -Body $searchRequest
    $found = @($search.memories).Count
    if ($found -lt 1) {
        throw "演示写入成功，但第二个 Agent 没有检索到共享记忆。"
    }
    $credentials.next_device_seq = $sequence + 1
    Write-LocalJson $credentialsPath $credentials
    $verification = [pscustomobject]@{
        event_status = $receipt.status
        cross_agent_results = $found
    }
}

[pscustomobject]@{
    status = "ready"
    gateway_url = $gatewayUrl
    demo_home = $demoRoot
    process_id = $gatewayProcess.Id
    verification = $verification
    note = "令牌只保存在 DemoHome 的本机文件中，未输出到终端。关闭 Gateway 时只停止进程，不会自动删除演示数据。"
}
