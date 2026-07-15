[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$AgentInstallationId,

    [Parameter(Mandatory)]
    [string]$DefaultWorkspace,

    [string]$SidecarKeyFile = "$env:LOCALAPPDATA\memory-gateway\secrets\pc-sidecar.env",

    [int]$SidecarPort = 8766,

    [int]$AdminPort = 8767,

    [string]$ConsoleExecutable = "memory-admin-console",

    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"

if ($AgentInstallationId -notmatch "^[A-Za-z0-9_.@:-]+$") {
    throw "AgentInstallationId 必须是已登记的 Agent 安装实例 ID"
}
if ($DefaultWorkspace -notmatch "^[A-Za-z0-9_.@:-]+$") {
    throw "DefaultWorkspace 必须是已登记的工作区 ID"
}
if ($SidecarPort -lt 1024 -or $SidecarPort -gt 65535) {
    throw "SidecarPort 必须在 1024 到 65535 之间"
}
if ($AdminPort -lt 1024 -or $AdminPort -gt 65535) {
    throw "AdminPort 必须在 1024 到 65535 之间"
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

# 管理页只能访问本机 Sidecar，不能继承 Gateway 或刷新凭据。
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
$env:MEMORY_SIDECAR_PORT = [string]$SidecarPort

$consoleCommand = Get-Command -Name $ConsoleExecutable -ErrorAction SilentlyContinue
if ($consoleCommand) {
    & $ConsoleExecutable --workspace $DefaultWorkspace --host 127.0.0.1 --port $AdminPort
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    exit 0
}

if ($ConsoleExecutable -ne "memory-admin-console") {
    throw "找不到管理控制台命令：$ConsoleExecutable"
}

# 从源码目录直接运行时，命令入口可能尚未安装。只在当前仓库的 src 下回退，
# 不安装依赖、不修改全局环境，也不会继承 Gateway 或刷新凭据。
$pythonCommand = Get-Command -Name $PythonExecutable -ErrorAction SilentlyContinue
$sourceDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) "src"
if (-not $pythonCommand -or -not (Test-Path -LiteralPath (Join-Path $sourceDirectory "agent_memory_gateway") -PathType Container)) {
    throw "找不到 memory-admin-console。请安装当前项目，或在包含 src 的仓库目录中运行此脚本。"
}

$hadPythonPath = Test-Path -LiteralPath "Env:PYTHONPATH"
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = if ($previousPythonPath) { "$sourceDirectory;$previousPythonPath" } else { $sourceDirectory }
    & $pythonCommand.Path -m agent_memory_gateway.admin_console --workspace $DefaultWorkspace --host 127.0.0.1 --port $AdminPort
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
