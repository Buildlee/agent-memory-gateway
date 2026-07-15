[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$AgentInstallationId,

    [Parameter(Mandatory)]
    [string]$DefaultWorkspace,

    [string]$SidecarKeyFile = "$env:LOCALAPPDATA\memory-gateway\secrets\pc-sidecar.env",

    [int]$Port = 8766,

    [ValidateRange(1, 3600)]
    [int]$MaxHeartbeatAgeSeconds = 90,

    [string]$CheckExecutable = "memory-admin-check",

    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"

if ($AgentInstallationId -notmatch "^[A-Za-z0-9_.@:-]+$") {
    throw "AgentInstallationId 必须是已登记的 Agent 安装实例 ID"
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
if (-not $keyValues.ContainsKey("MEMORY_OUTBOX_KEY")) {
    throw "Sidecar key 文件缺少 MEMORY_OUTBOX_KEY"
}

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
$env:MEMORY_DEFAULT_WORKSPACE = $DefaultWorkspace
$env:MEMORY_OUTBOX_KEY = $keyValues["MEMORY_OUTBOX_KEY"]
$env:MEMORY_SIDECAR_PORT = [string]$Port

$checkCommand = Get-Command -Name $CheckExecutable -ErrorAction SilentlyContinue
if ($checkCommand) {
    & $CheckExecutable --max-heartbeat-age-seconds $MaxHeartbeatAgeSeconds
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    exit 0
}

if ($CheckExecutable -ne "memory-admin-check") {
    throw "找不到管理健康检查命令：$CheckExecutable"
}

# 从源码目录直接运行时，命令入口可能尚未安装。只在当前仓库的 src 下回退，
# 不安装依赖、不修改全局环境，也不会继承 Gateway 或刷新凭据。
$pythonCommand = Get-Command -Name $PythonExecutable -ErrorAction SilentlyContinue
$sourceDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) "src"
if (-not $pythonCommand -or -not (Test-Path -LiteralPath (Join-Path $sourceDirectory "agent_memory_gateway") -PathType Container)) {
    throw "找不到 memory-admin-check。请安装当前项目，或在包含 src 的仓库目录中运行此脚本。"
}

$hadPythonPath = Test-Path -LiteralPath "Env:PYTHONPATH"
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = if ($previousPythonPath) { "$sourceDirectory;$previousPythonPath" } else { $sourceDirectory }
    & $pythonCommand.Path -m agent_memory_gateway.admin_check --max-heartbeat-age-seconds $MaxHeartbeatAgeSeconds
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
