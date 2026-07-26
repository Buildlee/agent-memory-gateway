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

    [ValidateRange(16, 1024)]
    [int]$MaximumReleaseMegabytes = 256,

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

function Test-ProjectRoot([string]$Path) {
    return (
        (Test-Path -LiteralPath (Join-Path $Path "pyproject.toml") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Path "src\agent_memory_gateway") -PathType Container) -and
        (Test-Path -LiteralPath (Join-Path $Path "scripts\setup-shared-memory.ps1") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Path "scripts\start-sidecar.ps1") -PathType Leaf)
    )
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
    $allowedKeys = @("version", "gateway_url", "default_workspace", "device_id_prefix", "agents", "release")
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
    if ($Profile.Contains("release")) {
        $release = $Profile["release"]
        if ($release -isnot [System.Collections.IDictionary]) {
            throw "安装配置中的 release 必须是对象：$Source"
        }
        $allowedReleaseKeys = @("release_id", "archive_url", "sha256")
        foreach ($entry in $release.GetEnumerator()) {
            if ($allowedReleaseKeys -notcontains [string]$entry.Key) {
                throw "安装配置中的发布包字段不受支持：$Source.$($entry.Key)"
            }
        }
        foreach ($required in $allowedReleaseKeys) {
            if (-not $release.Contains($required) -or [string]::IsNullOrWhiteSpace([string]$release[$required])) {
                throw "安装配置中的发布包缺少 $required：$Source"
            }
        }
        if ([string]$release["release_id"] -notmatch '^[A-Za-z0-9._-]{1,96}$') {
            throw "安装配置中的 release_id 无效：$Source"
        }
        $release["archive_url"] = Normalize-HttpsUrl -Value ([string]$release["archive_url"]) -Name "安装配置中的 release.archive_url"
        $release["sha256"] = ([string]$release["sha256"]).ToLowerInvariant()
        if ($release["sha256"] -notmatch '^[a-f0-9]{64}$') {
            throw "安装配置中的 release.sha256 必须是 64 位十六进制摘要：$Source"
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

function Get-ReleaseSpec([System.Collections.IDictionary]$Profile) {
    if (-not $Profile.Contains("release")) {
        return $null
    }
    return $Profile["release"]
}

function Get-FileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
}

function Save-VerifiedReleaseArchive([System.Collections.IDictionary]$Release) {
    $cacheRoot = Join-Path $env:LOCALAPPDATA "memory-gateway\downloads"
    New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
    $expectedHash = [string]$Release["sha256"]
    $archivePath = Join-Path $cacheRoot "$expectedHash.zip"
    if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
        if ((Get-FileSha256 -Path $archivePath) -ne $expectedHash) {
            throw "已有发布包摘要不匹配，拒绝使用或覆盖：$archivePath"
        }
        return $archivePath
    }

    $partialPath = "$archivePath.partial"
    if (Test-Path -LiteralPath $partialPath) {
        throw "发现未完成的发布包下载，拒绝覆盖：$partialPath"
    }
    $maximumBytes = [Int64]$MaximumReleaseMegabytes * 1MB
    $client = [System.Net.Http.HttpClient]::new()
    $response = $null
    $input = $null
    $output = $null
    try {
        $response = $client.GetAsync([Uri]$Release["archive_url"], [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "发布包下载返回 HTTP $([int]$response.StatusCode)。"
        }
        if ($response.Content.Headers.ContentLength -and $response.Content.Headers.ContentLength -gt $maximumBytes) {
            throw "发布包超过允许大小：$MaximumReleaseMegabytes MiB"
        }
        $input = $response.Content.ReadAsStream()
        $output = [System.IO.File]::Open($partialPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $buffer = New-Object byte[] 81920
        $total = [Int64]0
        while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $total += $read
            if ($total -gt $maximumBytes) {
                throw "发布包超过允许大小：$MaximumReleaseMegabytes MiB"
            }
            $output.Write($buffer, 0, $read)
        }
    }
    finally {
        if ($output) { $output.Dispose() }
        if ($input) { $input.Dispose() }
        if ($response) { $response.Dispose() }
        $client.Dispose()
    }
    if ((Get-FileSha256 -Path $partialPath) -ne $expectedHash) {
        throw "发布包 SHA-256 不匹配，已保留下载文件以便排查：$partialPath"
    }
    Move-Item -LiteralPath $partialPath -Destination $archivePath -ErrorAction Stop
    return $archivePath
}

function Resolve-ExtractedProjectRoot([string]$ReleaseDirectory) {
    if (Test-ProjectRoot -Path $ReleaseDirectory) {
        return $ReleaseDirectory
    }
    $candidates = @(
        Get-ChildItem -LiteralPath $ReleaseDirectory -Directory -ErrorAction Stop |
            Where-Object { Test-ProjectRoot -Path $_.FullName }
    )
    if ($candidates.Count -ne 1) {
        throw "发布包没有唯一、完整的项目根目录：$ReleaseDirectory"
    }
    return $candidates[0].FullName
}

function Resolve-ProjectRoot([System.Collections.IDictionary]$Profile) {
    $localRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
    if (Test-ProjectRoot -Path $localRoot) {
        return [pscustomobject]@{ Source = "local"; Root = $localRoot }
    }
    $release = Get-ReleaseSpec -Profile $Profile
    if ($null -eq $release) {
        throw "当前安装脚本旁没有完整项目代码。请在安装配置中提供经 SHA-256 校验的 release，或从完整发布副本运行脚本。"
    }
    $releaseRoot = Join-Path $env:LOCALAPPDATA "memory-gateway\releases\$($release["release_id"])-$($release["sha256"].Substring(0, 16))"
    if (Test-Path -LiteralPath $releaseRoot) {
        return [pscustomobject]@{ Source = "verified_release_cache"; Root = (Resolve-ExtractedProjectRoot -ReleaseDirectory $releaseRoot) }
    }
    $archivePath = Save-VerifiedReleaseArchive -Release $release
    New-Item -ItemType Directory -Path $releaseRoot -ErrorAction Stop | Out-Null
    try {
        Expand-Archive -LiteralPath $archivePath -DestinationPath $releaseRoot -ErrorAction Stop
    }
    catch {
        throw "发布包已校验，但解压失败。为避免覆盖诊断现场，目录已保留：$releaseRoot"
    }
    return [pscustomobject]@{ Source = "verified_release_download"; Root = (Resolve-ExtractedProjectRoot -ReleaseDirectory $releaseRoot) }
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
    project_source = if (Test-ProjectRoot -Path ([System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))) ) { "local" } elseif ($profile.Contains("release")) { "verified_release_download" } else { "missing" }
    next_step = "确认后输入一次性配对码；配对码只在隐藏输入中使用，不写入配置或命令行。"
}
if ($Plan) {
    $installPlan
    exit 0
}

$projectResolution = Resolve-ProjectRoot -Profile $profile
$setupScript = Join-Path $projectResolution.Root "scripts\setup-shared-memory.ps1"

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
