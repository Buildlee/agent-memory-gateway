[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$GatewayUrl,

    [Parameter(Mandatory)]
    [string]$AllowedAgents,

    [Parameter(Mandatory)]
    [string]$DeviceId,

    [Parameter(Mandatory)]
    [string]$DefaultWorkspace,

    [string]$CredentialTarget = "AgentMemoryGateway/local-device",

    [string]$SidecarKeyFile = "$env:LOCALAPPDATA\memory-gateway\secrets\pc-sidecar.env",

    [string]$MemoryHome = "$env:LOCALAPPDATA\memory-gateway\sidecar-v1",

    [int]$Port = 8766,

    [AllowEmptyString()]
    [string]$GatewayCaCertificate = "",

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
if ($DefaultWorkspace -notmatch "^[A-Za-z0-9_.@:-]+$") {
    throw "DefaultWorkspace 必须是已登记的工作区 ID"
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
$env:MEMORY_DEFAULT_WORKSPACE = $DefaultWorkspace
$env:MEMORY_OUTBOX_KEY = $keyValues["MEMORY_OUTBOX_KEY"]
$env:MEMORY_OUTBOX_KEY_VERSION = $keyValues["MEMORY_OUTBOX_KEY_VERSION"]
$env:MEMORY_HOME = $MemoryHome
$env:MEMORY_SIDECAR_PORT = [string]$Port

$pythonCommand = Get-Command -Name $PythonExecutable -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    throw "找不到 Python 解释器：$PythonExecutable"
}

# 已登记的 Windows 任务以当前仓库作为工作目录。优先使用该发布副本的源码，
# 这样更新后的 Sidecar 不依赖替换正在被 MCP 客户端占用的启动程序；不会安装依赖
# 或改写全局环境。没有源码目录时，保留已安装包的原有启动方式。
$sourceDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) "src"
$sourcePackage = Join-Path $sourceDirectory "agent_memory_gateway"
$hadPythonPath = Test-Path -LiteralPath "Env:PYTHONPATH"
$previousPythonPath = $env:PYTHONPATH
try {
    if (Test-Path -LiteralPath $sourcePackage -PathType Container) {
        $env:PYTHONPATH = if ($previousPythonPath) { "$sourceDirectory;$previousPythonPath" } else { $sourceDirectory }
    }
    & $pythonCommand.Path -m agent_memory_gateway.sidecar_daemon --host 127.0.0.1 --port $Port
} finally {
    if ($hadPythonPath) {
        $env:PYTHONPATH = $previousPythonPath
    } else {
        Remove-Item -LiteralPath "Env:PYTHONPATH" -ErrorAction SilentlyContinue
    }
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
