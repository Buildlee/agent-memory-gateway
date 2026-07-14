[CmdletBinding()]
param(
    [string]$TaskName = "MemoryGatewaySidecar",

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

if ($TaskName -notmatch "^[A-Za-z0-9_. -]{1,128}$") {
    throw "TaskName 只能使用字母、数字、空格、点、下划线或连字符"
}
if ($GatewayUrl -notmatch "^https?://[^\s/]+") {
    throw "GatewayUrl 必须是 HTTP 或 HTTPS 地址"
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
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "计划任务已存在，拒绝覆盖：$TaskName"
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$startScript = Join-Path $PSScriptRoot "start-sidecar.ps1"
if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
    throw "找不到 Sidecar 启动脚本：$startScript"
}
if (-not (Test-Path -LiteralPath $SidecarKeyFile -PathType Leaf)) {
    throw "找不到本机 Sidecar key 文件：$SidecarKeyFile"
}
if (-not [string]::IsNullOrWhiteSpace($GatewayCaCertificate) -and -not (Test-Path -LiteralPath $GatewayCaCertificate -PathType Leaf)) {
    throw "找不到 Gateway CA 证书文件：$GatewayCaCertificate"
}

$pwsh = (Get-Command pwsh -ErrorAction Stop).Source
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($taskUser)) {
    throw "无法识别当前 Windows 用户"
}

function Quote-TaskArgument([string]$Value) {
    '"' + $Value.Replace('"', '""') + '"'
}

$arguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-TaskArgument $startScript),
    "-GatewayUrl", (Quote-TaskArgument $GatewayUrl),
    "-AllowedAgents", (Quote-TaskArgument $AllowedAgents),
    "-DeviceId", (Quote-TaskArgument $DeviceId),
    "-CredentialTarget", (Quote-TaskArgument $CredentialTarget),
    "-SidecarKeyFile", (Quote-TaskArgument $SidecarKeyFile),
    "-MemoryHome", (Quote-TaskArgument $MemoryHome),
    "-Port", $Port,
    "-GatewayCaCertificate", (Quote-TaskArgument $GatewayCaCertificate),
    "-PythonExecutable", (Quote-TaskArgument $PythonExecutable)
) -join " "

$action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$registration = @{
    TaskName = $TaskName
    Action = $action
    Trigger = $trigger
    Principal = $principal
    Settings = $settings
    Description = "启动仅回环访问的 Memory Gateway Sidecar；内部 CA 仅在该进程中使用。"
}
Register-ScheduledTask @registration | Out-Null

[pscustomobject]@{
    task_name = $TaskName
    user = $taskUser
    trigger = "AtLogOn"
    gateway_url = $GatewayUrl
    gateway_ca_certificate = $GatewayCaCertificate
    listener = "localhost:$Port"
}
