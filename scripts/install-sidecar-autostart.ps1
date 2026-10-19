[CmdletBinding()]
param(
    [string]$TaskName = "MemoryGatewaySidecar",

    [Parameter(Mandatory)]
    [string]$GatewayUrl,

    [Parameter(Mandatory)]
    [string]$AllowedAgents,

    [Parameter(Mandatory)]
    [string]$DeviceId,

    [Parameter(Mandatory)]
    [string]$DefaultWorkspace,

    [string]$HeartbeatAgent = "",

    [string]$CredentialTarget = "AgentMemoryGateway/local-device",

    [string]$SidecarKeyFile = "$env:LOCALAPPDATA\memory-gateway\secrets\pc-sidecar.env",

    [string]$MemoryHome = "$env:LOCALAPPDATA\memory-gateway\sidecar-v1",

    [int]$Port = 8766,

    [AllowEmptyString()]
    [string]$GatewayCaCertificate = "",

    [switch]$ReplaceExistingManagedTask,

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
$allowedAgentIds = @($AllowedAgents.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ([string]::IsNullOrWhiteSpace($HeartbeatAgent)) {
    if ($allowedAgentIds.Count -ne 1) {
        throw "AllowedAgents 包含多个 Agent 时，必须显式指定 HeartbeatAgent"
    }
    $HeartbeatAgent = $allowedAgentIds[0]
}
if ($HeartbeatAgent -notmatch "^[A-Za-z0-9_.@:-]+$") {
    throw "HeartbeatAgent 必须是已登记的 Agent 安装实例 ID"
}
if ($allowedAgentIds -notcontains $HeartbeatAgent) {
    throw "HeartbeatAgent 必须包含在 AllowedAgents 中"
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
$managedTaskDescription = "启动仅回环访问的 Memory Gateway Sidecar；内部 CA 仅在该进程中使用。"

function Test-ManagedSidecarTask($Task) {
    if ($Task.Description -ne $managedTaskDescription) {
        return $false
    }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        return $false
    }
    # 旧形态：任务直接跑 pwsh -File start-sidecar.ps1
    if ($actions[0].Arguments -match '(?i)(?:^|\s)-File\s+"?[^"]*\\scripts\\(?:start-sidecar(?:-host)?|ensure-sidecar)\.ps1"?') {
        return $true
    }
    # 现行形态：wscript 隐藏拉起 local\launchers\sidecar-hidden.vbs
    if ($actions[0].Execute -match '(?i)(?:^|\\)wscript\.exe$' -and $actions[0].Arguments -match '(?i)sidecar-hidden[^"]*\.vbs') {
        return $true
    }
    return $false
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    if (-not $ReplaceExistingManagedTask) {
        throw "计划任务已存在，拒绝覆盖：$TaskName"
    }
    if (-not (Test-ManagedSidecarTask $existingTask)) {
        throw "计划任务已存在，但不是本安装器创建的受管 Sidecar 任务，拒绝替换：$TaskName"
    }
    if ($existingTask.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
        $stopped = $false
        for ($attempt = 0; $attempt -lt 10; $attempt++) {
            Start-Sleep -Milliseconds 500
            $currentTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
            if ($currentTask.State -ne "Running") {
                $stopped = $true
                break
            }
        }
        if (-not $stopped) {
            throw "受管 Sidecar 任务未在 5 秒内停止，拒绝替换：$TaskName"
        }
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$startScript = Join-Path $PSScriptRoot "ensure-sidecar.ps1"
if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
    throw "找不到 Sidecar 幂等启动脚本：$startScript"
}
if (-not (Test-Path -LiteralPath $SidecarKeyFile -PathType Leaf)) {
    throw "找不到本机 Sidecar key 文件：$SidecarKeyFile"
}
if (-not [string]::IsNullOrWhiteSpace($GatewayCaCertificate) -and -not (Test-Path -LiteralPath $GatewayCaCertificate -PathType Leaf)) {
    throw "找不到 Gateway CA 证书文件：$GatewayCaCertificate"
}

$pwshCommand = Get-Command -Name "pwsh" -ErrorAction SilentlyContinue
if (-not $pwshCommand -or -not (Test-Path -LiteralPath $pwshCommand.Source -PathType Leaf)) {
    throw "找不到 PowerShell 7（pwsh）。请先安装 PowerShell 7，或将 pwsh 加入 PATH。"
}
$taskHost = $pwshCommand.Source
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
    # 隐藏宿主窗口：计划任务每 5 分钟触发会反复拉起 pwsh，
    # 不隐藏时桌面会出现持续可见的 Sidecar 控制台窗口。
    "-WindowStyle", "Hidden",
    "-File", (Quote-TaskArgument $startScript),
    "-GatewayUrl", (Quote-TaskArgument $GatewayUrl),
    "-AllowedAgents", (Quote-TaskArgument $AllowedAgents),
    "-DeviceId", (Quote-TaskArgument $DeviceId),
    "-DefaultWorkspace", (Quote-TaskArgument $DefaultWorkspace),
    "-HeartbeatAgent", (Quote-TaskArgument $HeartbeatAgent),
    "-CredentialTarget", (Quote-TaskArgument $CredentialTarget),
    "-SidecarKeyFile", (Quote-TaskArgument $SidecarKeyFile),
    "-MemoryHome", (Quote-TaskArgument $MemoryHome),
    "-Port", $Port,
    "-GatewayCaCertificate", (Quote-TaskArgument $GatewayCaCertificate),
    "-PythonExecutable", (Quote-TaskArgument $PythonExecutable)
) -join " "

# 计划任务动作指向 .cmd/.ps1 时，系统都会给动作进程分配控制台（桌面闪黑框），
# 只有 wscript 能以"窗口样式 0"真正隐藏。VBS 由本安装器生成，内容不含任何非 ASCII
# 字面量（路径经参数传入并转义），避免 wscript 按 ANSI 解析脚本时的编码问题。
$launcherDir = Join-Path $env:LOCALAPPDATA "memory-gateway\launchers"
if (-not (Test-Path -LiteralPath $launcherDir)) {
    New-Item -ItemType Directory -Path $launcherDir -Force | Out-Null
}
$launcherPath = Join-Path $launcherDir "sidecar-hidden.vbs"
$vbsCommand = (Quote-TaskArgument $taskHost) + " " + $arguments
$vbsBody = @"
Option Explicit
Dim shell, result
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "$projectRoot"
' NOTE: parentheses are REQUIRED when you take Run's return value in an assignment.
' Without them VBScript fails to compile and wscript exits 1 without writing any log.
result = shell.Run("$($vbsCommand.Replace('"', '""'))", 0, True)
WScript.Quit result
"@
[System.IO.File]::WriteAllText($launcherPath, $vbsBody, (New-Object System.Text.UnicodeEncoding($false, $true)))

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ("//B //NoLogo " + (Quote-TaskArgument $launcherPath)) -WorkingDirectory $projectRoot
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$recoveryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 365)
$principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$registration = @{
    TaskName = $TaskName
    Action = $action
    Trigger = @($logonTrigger, $recoveryTrigger)
    Principal = $principal
    Settings = $settings
    Description = $managedTaskDescription
}
Register-ScheduledTask @registration -Force | Out-Null

[pscustomobject]@{
    task_name = $TaskName
    user = $taskUser
    trigger = "AtLogOn"
    recovery_check = "every 5 minutes"
    launcher = $launcherPath
    launch_mode = "wscript 隐藏窗口 + 幂等检查（健康则直接退出）"
    gateway_url = $GatewayUrl
    default_workspace = $DefaultWorkspace
    gateway_ca_certificate = $GatewayCaCertificate
    listener = "localhost:$Port"
}
