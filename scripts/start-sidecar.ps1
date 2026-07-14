[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$GatewayUrl,

    [Parameter(Mandatory)]
    [string]$AllowedAgents,

    [Parameter(Mandatory)]
    [string]$DeviceId,

    [string]$CredentialTarget = "AgentMemoryGateway/local-device",

    [string]$SidecarKeyFile = "$env:LOCALAPPDATA\memory-gateway\secrets\pc-sidecar.env",

    [string]$MemoryHome = "$env:LOCALAPPDATA\memory-gateway\sidecar-v1",

    [int]$Port = 8766,

    [AllowEmptyString()]
    [string]$GatewayCaCertificate = "$env:LOCALAPPDATA\memory-gateway\certs\gateway-root.crt",

    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"

if ($GatewayUrl -notmatch "^https?://[^\s/]+") {
    throw "GatewayUrl 必须是 HTTP 或 HTTPS 地址"
}
$gatewayUri = [Uri]$GatewayUrl
if ($CredentialTarget.Length -gt 256 -or [string]::IsNullOrWhiteSpace($CredentialTarget)) {
    throw "CredentialTarget 无效"
}
if ($AllowedAgents -notmatch "^[A-Za-z0-9_.@,:-]+$") {
    throw "AllowedAgents 只能使用 Agent 安装实例 ID，以逗号分隔"
}
if ($DeviceId -notmatch "^[A-Za-z0-9_.@:-]+$") {
    throw "DeviceId 必须是已登记的设备 ID"
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
if (-not $keyValues.ContainsKey("MEMORY_OUTBOX_KEY") -or -not $keyValues.ContainsKey("MEMORY_OUTBOX_KEY_VERSION")) {
    throw "Sidecar key 文件缺少必要字段"
}

$env:MEMORY_GATEWAY_URL = $GatewayUrl.TrimEnd("/")
Remove-Item -LiteralPath "Env:MEMORY_GATEWAY_CA_CERTIFICATE" -ErrorAction SilentlyContinue
if ($gatewayUri.Scheme -eq "https" -and -not [string]::IsNullOrWhiteSpace($GatewayCaCertificate)) {
    if (-not (Test-Path -LiteralPath $GatewayCaCertificate -PathType Leaf)) {
        throw "找不到 Gateway CA 证书文件：$GatewayCaCertificate"
    }
    $env:MEMORY_GATEWAY_CA_CERTIFICATE = (Resolve-Path -LiteralPath $GatewayCaCertificate).ProviderPath
}
$env:MEMORY_REFRESH_CREDENTIAL_TARGET = $CredentialTarget
$env:MEMORY_SIDECAR_ALLOWED_AGENTS = $AllowedAgents
$env:MEMORY_DEVICE_ID = $DeviceId
$env:MEMORY_OUTBOX_KEY = $keyValues["MEMORY_OUTBOX_KEY"]
$env:MEMORY_OUTBOX_KEY_VERSION = $keyValues["MEMORY_OUTBOX_KEY_VERSION"]
$env:MEMORY_HOME = $MemoryHome
$env:MEMORY_SIDECAR_PORT = [string]$Port

& $PythonExecutable -m agent_memory_gateway.sidecar_daemon --host 127.0.0.1 --port $Port
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
