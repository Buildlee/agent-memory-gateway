[CmdletBinding()]
param(
    [string]$ProfilePath = "",

    [string]$ProfileUrl = "",

    [string]$GatewayUrl = "",

    [string]$DefaultWorkspace = "",

    [string]$DeviceId = "",

    [string]$DeviceName = $env:COMPUTERNAME,

    [string[]]$Agent = @(),

    [AllowEmptyString()]
    [string]$GatewayCaCertificate = "",

    [switch]$Resume,

    [switch]$NoAutostart,

    [switch]$Plan
)

$ErrorActionPreference = "Stop"

function Read-RequiredValue([string]$Name, [string]$Value, [string]$Prompt) {
    $resolved = [string]$Value
    if ([string]::IsNullOrWhiteSpace($resolved)) {
        $resolved = Read-Host $Prompt
    }
    $resolved = $resolved.Trim()
    if ([string]::IsNullOrWhiteSpace($resolved)) {
        throw "缺少 $Name。"
    }
    return $resolved
}

function Normalize-HttpsUrl([string]$Value, [string]$Name) {
    try {
        $uri = [Uri]$Value
    }
    catch {
        throw "$Name 必须是 HTTPS 地址。"
    }
    if (
        -not $uri.IsAbsoluteUri -or
        $uri.Scheme -ne "https" -or
        [string]::IsNullOrWhiteSpace($uri.Host) -or
        -not [string]::IsNullOrWhiteSpace($uri.UserInfo) -or
        -not [string]::IsNullOrWhiteSpace($uri.Query) -or
        -not [string]::IsNullOrWhiteSpace($uri.Fragment)
    ) {
        throw "$Name 必须是不带账号、查询参数或片段的 HTTPS 地址。"
    }
    return $uri.AbsoluteUri.TrimEnd("/")
}

function ConvertFrom-InstallProfile([string]$Json, [string]$Source) {
    try {
        $profile = $Json | ConvertFrom-Json -AsHashtable -ErrorAction Stop
    }
    catch {
        throw "安装配置不是有效 JSON：$Source"
    }
    if ($profile -isnot [System.Collections.IDictionary]) {
        throw "安装配置必须是 JSON 对象：$Source"
    }
    return $profile
}

function Assert-NonSensitiveProfileValue([object]$Value, [string]$Path) {
    if ($null -eq $Value) {
        return
    }
    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($entry in $Value.GetEnumerator()) {
            $name = [string]$entry.Key
            if ($name -match '(?i)(secret|credential|token|pairing|password|private|refresh|dsn|connection)') {
                throw "安装配置不能包含敏感字段：$Path.$name"
            }
            Assert-NonSensitiveProfileValue -Value $entry.Value -Path "$Path.$name"
        }
        return
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        $index = 0
        foreach ($item in $Value) {
            Assert-NonSensitiveProfileValue -Value $item -Path "$Path[$index]"
            $index++
        }
    }
}

function Assert-InstallProfile([System.Collections.IDictionary]$Profile, [string]$Source) {
    $allowedKeys = @("version", "gateway_url", "default_workspace", "device_id_prefix", "agents")
    foreach ($entry in $Profile.GetEnumerator()) {
        $name = [string]$entry.Key
        if ($allowedKeys -notcontains $name) {
            throw "安装配置包含不支持的字段：$Source.$name"
        }
    }
    Assert-NonSensitiveProfileValue -Value $Profile -Path "profile"
    if ($Profile.Contains("version") -and [int]$Profile["version"] -ne 1) {
        throw "安装配置版本不受支持：$Source"
    }
    if ($Profile.Contains("gateway_url")) {
        $Profile["gateway_url"] = Normalize-HttpsUrl -Value ([string]$Profile["gateway_url"]) -Name "安装配置中的 gateway_url"
    }
    if ($Profile.Contains("default_workspace") -and [string]$Profile["default_workspace"] -notmatch '^[A-Za-z0-9_.@:-]+$') {
        throw "安装配置中的 default_workspace 无效：$Source"
    }
    if ($Profile.Contains("device_id_prefix") -and [string]$Profile["device_id_prefix"] -notmatch '^[A-Za-z0-9_.@:-]+$') {
        throw "安装配置中的 device_id_prefix 无效：$Source"
    }
    if ($Profile.Contains("agents")) {
        if ($Profile["agents"] -isnot [System.Collections.IEnumerable] -or $Profile["agents"] -is [string]) {
            throw "安装配置中的 agents 必须是数组：$Source"
        }
        foreach ($agent in $Profile["agents"]) {
            if ($agent -isnot [System.Collections.IDictionary]) {
                throw "安装配置中的 agents 条目无效：$Source"
            }
            $allowedAgentKeys = @("type", "display_name", "installation_id_template")
            foreach ($entry in $agent.GetEnumerator()) {
                if ($allowedAgentKeys -notcontains [string]$entry.Key) {
                    throw "安装配置中的 Agent 字段不受支持：$Source.$($entry.Key)"
                }
            }
            if ([string]$agent["type"] -notin @("codex", "hermes", "other")) {
                throw "安装配置中的 Agent 类型无效：$Source"
            }
            if ([string]::IsNullOrWhiteSpace([string]$agent["display_name"])) {
                throw "安装配置中的 Agent 显示名不能为空：$Source"
            }
        }
    }
}

function Get-InstallProfile {
    if (-not [string]::IsNullOrWhiteSpace($ProfilePath) -and -not [string]::IsNullOrWhiteSpace($ProfileUrl)) {
        throw "ProfilePath 和 ProfileUrl 只能选择其中一个。"
    }
    $candidate = $ProfilePath
    if ([string]::IsNullOrWhiteSpace($candidate) -and -not [string]::IsNullOrWhiteSpace($env:MEMORY_DEVICE_INSTALL_PROFILE)) {
        $candidate = $env:MEMORY_DEVICE_INSTALL_PROFILE
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $defaultPath = Join-Path $env:LOCALAPPDATA "memory-gateway\device-install.json"
        if (Test-Path -LiteralPath $defaultPath -PathType Leaf) {
            $candidate = $defaultPath
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($candidate)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "找不到安装配置：$candidate"
        }
        $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).ProviderPath
        $profile = ConvertFrom-InstallProfile -Json (Get-Content -LiteralPath $resolved -Raw -Encoding utf8) -Source $resolved
        Assert-InstallProfile -Profile $profile -Source $resolved
        return [pscustomobject]@{ Source = $resolved; Value = $profile }
    }
    if (-not [string]::IsNullOrWhiteSpace($ProfileUrl)) {
        $ProfileUrl = Normalize-HttpsUrl -Value $ProfileUrl -Name "ProfileUrl"
        try {
            $response = Invoke-WebRequest -Uri $ProfileUrl -TimeoutSec 15 -ErrorAction Stop
        }
        catch {
            throw "无法下载安装配置。请检查 ProfileUrl、网络和证书链。"
        }
        $profile = ConvertFrom-InstallProfile -Json $response.Content -Source $ProfileUrl
        Assert-InstallProfile -Profile $profile -Source $ProfileUrl
        return [pscustomobject]@{ Source = $ProfileUrl; Value = $profile }
    }
    return [pscustomobject]@{ Source = "interactive"; Value = @{} }
}

function New-DefaultDeviceId([string]$Name, [System.Collections.IDictionary]$Profile) {
    $prefix = if ($Profile.Contains("device_id_prefix")) { [string]$Profile["device_id_prefix"] } else { "windows" }
    $slug = $Name.ToLowerInvariant() -replace '[^a-z0-9]+', '-'
    $slug = $slug.Trim('-')
    if ([string]::IsNullOrWhiteSpace($slug)) {
        throw "无法从设备名称生成稳定设备 ID；请传入 -DeviceId。"
    }
    return "$prefix-$slug"
}

function Test-CommandAvailable([string]$Name) {
    return $null -ne (Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

function Get-DetectedAgentSpecs([string]$ResolvedDeviceId) {
    $detected = @()
    $codexHome = Join-Path $HOME ".codex"
    if ((Test-Path -LiteralPath $codexHome -PathType Container) -or (Test-CommandAvailable -Name "codex")) {
        $detected += "codex-$ResolvedDeviceId|codex|Codex"
    }
    $hermesSignals = @(
        (Join-Path $env:LOCALAPPDATA "hermes"),
        (Join-Path $env:LOCALAPPDATA "Hermes Studio"),
        (Join-Path $env:APPDATA "hermes"),
        (Join-Path $HOME ".hermes")
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    if ((Test-CommandAvailable -Name "hermes") -or @($hermesSignals | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0) {
        $detected += "hermes-$ResolvedDeviceId|hermes|Hermes"
    }
    return @($detected)
}

function Get-ProfileAgentSpecs([System.Collections.IDictionary]$Profile, [string]$ResolvedDeviceId) {
    if (-not $Profile.Contains("agents")) {
        return @()
    }
    $specs = @()
    foreach ($agent in $Profile["agents"]) {
        $agentType = [string]$agent["type"]
        $template = if ($agent.Contains("installation_id_template")) { [string]$agent["installation_id_template"] } else { "$agentType-{device_id}" }
        $installationId = $template.Replace("{device_id}", $ResolvedDeviceId)
        if ($installationId -notmatch '^[A-Za-z0-9_.@:-]+$') {
            throw "安装配置生成了无效的 Agent 安装实例 ID：$installationId"
        }
        $displayName = [string]$agent["display_name"]
        if ($displayName.Contains("|")) {
            throw "安装配置中的 Agent 显示名不能包含竖线。"
        }
        $specs += "$installationId|$agentType|$displayName"
    }
    return @($specs)
}

$profileInfo = Get-InstallProfile
$profile = $profileInfo.Value
$resolvedDeviceName = Read-RequiredValue -Name "DeviceName" -Value $DeviceName -Prompt "设备显示名"
$resolvedDeviceId = if (-not [string]::IsNullOrWhiteSpace($DeviceId)) { $DeviceId.Trim() } else { New-DefaultDeviceId -Name $resolvedDeviceName -Profile $profile }
$profileGatewayUrl = if ($profile.Contains("gateway_url")) { [string]$profile["gateway_url"] } else { "" }
$profileWorkspace = if ($profile.Contains("default_workspace")) { [string]$profile["default_workspace"] } else { "" }
$resolvedGatewayUrl = Read-RequiredValue -Name "GatewayUrl" -Value $(if ($GatewayUrl) { $GatewayUrl } elseif ($profileGatewayUrl) { $profileGatewayUrl } else { $env:MEMORY_GATEWAY_URL }) -Prompt "Gateway HTTPS 地址"
$resolvedWorkspace = Read-RequiredValue -Name "DefaultWorkspace" -Value $(if ($DefaultWorkspace) { $DefaultWorkspace } elseif ($profileWorkspace) { $profileWorkspace } else { $env:MEMORY_DEFAULT_WORKSPACE }) -Prompt "工作区 ID"
$resolvedGatewayUrl = Normalize-HttpsUrl -Value $resolvedGatewayUrl -Name "GatewayUrl"
if ($resolvedDeviceId -notmatch '^[A-Za-z0-9_.@:-]+$') {
    throw "DeviceId 无效。"
}
if ($resolvedWorkspace -notmatch '^[A-Za-z0-9_.@:-]+$') {
    throw "DefaultWorkspace 无效。"
}

$resolvedAgents = if ($Agent.Count -gt 0) {
    @($Agent)
} else {
    $fromProfile = Get-ProfileAgentSpecs -Profile $profile -ResolvedDeviceId $resolvedDeviceId
    if ($fromProfile.Count -gt 0) { $fromProfile } else { Get-DetectedAgentSpecs -ResolvedDeviceId $resolvedDeviceId }
}
if ($resolvedAgents.Count -eq 0) {
    throw "没有检测到可接入的 Agent。请让管理员在安装配置中提供 agents，或传入通用 -Agent '实例ID|类型|显示名'。"
}
if (($resolvedAgents | ForEach-Object { ($_ -split '\|', 3)[0] } | Select-Object -Unique).Count -ne $resolvedAgents.Count) {
    throw "Agent 安装实例 ID 不能重复。"
}

$installPlan = [pscustomobject]@{
    status = if ($Plan) { "ready_to_install" } else { "installing" }
    profile_source = $profileInfo.Source
    device_id = $resolvedDeviceId
    device_name = $resolvedDeviceName
    default_workspace = $resolvedWorkspace
    agent_installation_ids = @($resolvedAgents | ForEach-Object { ($_ -split '\|', 3)[0] })
    autostart = -not $NoAutostart
    next_step = "确认后输入一次性配对码；配对码只在隐藏输入中使用，不写入配置或命令行。"
}
if ($Plan) {
    $installPlan
    exit 0
}

$setupScript = Join-Path $PSScriptRoot "setup-shared-memory.ps1"
if (-not (Test-Path -LiteralPath $setupScript -PathType Leaf)) {
    throw "找不到受控安装向导：$setupScript"
}

Write-Output "正在准备共享记忆端侧：$($resolvedAgents.Count) 个 Agent，计划任务=$(-not $NoAutostart)。"
$setupArguments = @{
    Mode = "device"
    GatewayUrl = $resolvedGatewayUrl
    DeviceId = $resolvedDeviceId
    DeviceName = $resolvedDeviceName
    DeviceType = "windows"
    Agent = $resolvedAgents
    DefaultWorkspace = $resolvedWorkspace
    InstallAutostart = -not $NoAutostart
    UseExistingCredential = [bool]$Resume
}
if (-not [string]::IsNullOrWhiteSpace($GatewayCaCertificate)) {
    $setupArguments.GatewayCaCertificate = $GatewayCaCertificate
}
& $setupScript @setupArguments
exit $LASTEXITCODE
