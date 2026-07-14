[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$AgentInstallationId,

    [string]$SidecarKeyFile = "$env:LOCALAPPDATA\memory-gateway\secrets\pc-sidecar.env",

    [int]$Port = 8766,

    [string]$McpExecutable = "memory-sidecar-mcp"
)

$ErrorActionPreference = "Stop"

if ($AgentInstallationId -notmatch "^[A-Za-z0-9_.@:-]+$") {
    throw "AgentInstallationId 必须是已登记的 Agent 安装实例 ID"
}
if ($Port -lt 1024 -or $Port -gt 65535) {
    throw "Port 必须在 1024 到 65535 之间"
}
if (-not (Test-Path -LiteralPath $SidecarKeyFile -PathType Leaf)) {
    throw "找不到本机 Sidecar key 文件：$SidecarKeyFile"
}

$keyValues = @{}
Get-Content -LiteralPath $SidecarKeyFile | ForEach-Object {
    if ($_ -match "^([A-Z0-9_]+)=(.+)$") {
        $keyValues[$Matches[1]] = $Matches[2]
    }
}
if (-not $keyValues.ContainsKey("MEMORY_OUTBOX_KEY")) {
    throw "Sidecar key 文件缺少 MEMORY_OUTBOX_KEY"
}

# MCP 只能访问本机 Sidecar，不能继承 Gateway 或刷新凭据。
@(
    "MEMORY_GATEWAY_URL",
    "MEMORY_GATEWAY_TOKEN",
    "MEMORY_REFRESH_CREDENTIAL_TARGET",
    "MEMORY_DEVICE_ID",
    "MEMORY_HOME",
    "MEMORY_GATEWAY_CA_CERTIFICATE",
    "MEMORY_OUTBOX_KEY_VERSION",
    "MEMORY_ALLOW_EMBEDDED_SIDECAR"
) | ForEach-Object {
    Remove-Item -LiteralPath "Env:$_" -ErrorAction SilentlyContinue
}

$env:MEMORY_AGENT_INSTALLATION_ID = $AgentInstallationId
$env:MEMORY_OUTBOX_KEY = $keyValues["MEMORY_OUTBOX_KEY"]
$env:MEMORY_SIDECAR_PORT = [string]$Port

& $McpExecutable
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
