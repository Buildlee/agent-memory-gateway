[CmdletBinding()]
param(
    [string]$ProfilePath = "",

    [string]$ProfileUrl = "",

    [string]$GatewayUrl = "",

    [string]$DefaultWorkspace = "",

    [string]$DeviceId = "",

    [string]$DeviceName = [Environment]::MachineName,

    [string[]]$Agent = @(),

    [AllowEmptyString()]
    [string]$GatewayCaCertificate = "",

    [switch]$Resume,

    [switch]$NoAutostart,

    [ValidateRange(16, 1024)]
    [int]$MaximumReleaseMegabytes = 256,

    [ValidateSet("stable", "development")]
    [string]$Channel = "stable",

    [switch]$Plan
)

$ErrorActionPreference = "Stop"

function Get-UserHomeDirectory {
    $candidate = if (-not [string]::IsNullOrWhiteSpace($HOME)) {
        $HOME
    } else {
        [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        throw "无法确定当前用户目录。"
    }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Get-LocalDataDirectory([string]$UserHome) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        return [System.IO.Path]::GetFullPath($env:LOCALAPPDATA)
    }
    $knownFolder = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if (-not [string]::IsNullOrWhiteSpace($knownFolder)) {
        return [System.IO.Path]::GetFullPath($knownFolder)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:XDG_DATA_HOME)) {
        return [System.IO.Path]::GetFullPath($env:XDG_DATA_HOME)
    }
    if ($PSVersionTable.PSVersion.Major -ge 6 -and $IsMacOS) {
        return Join-Path $UserHome "Library/Application Support"
    }
    return Join-Path $UserHome ".local/share"
}

$userHome = Get-UserHomeDirectory
$localDataRoot = Get-LocalDataDirectory -UserHome $userHome
$roamingDataRoot = if (-not [string]::IsNullOrWhiteSpace($env:APPDATA)) {
    [System.IO.Path]::GetFullPath($env:APPDATA)
} else {
    $localDataRoot
}

# 默认安装稳定发布；只有明确选择 development 时才跟随 main。
# 安装配置中的 release（含 SHA-256）始终具有最高优先级。
$DefaultStableManifestUrl = "https://github.com/Buildlee/agent-memory-gateway/releases/latest/download/release-manifest.json"
$DefaultMainArchiveUrl = "https://github.com/Buildlee/agent-memory-gateway/archive/refs/heads/main.zip"
$resolvedChannel = if ($PSBoundParameters.ContainsKey("Channel")) {
    $Channel
} elseif (-not [string]::IsNullOrWhiteSpace($env:MEMORY_DEVICE_CHANNEL)) {
    $env:MEMORY_DEVICE_CHANNEL.Trim().ToLowerInvariant()
} else {
    $Channel
}
if ($resolvedChannel -notin @("stable", "development")) {
    throw "安装通道无效；只接受 stable 或 development。"
}

function Read-RequiredValue([string]$Name, [string]$Value, [string]$Prompt) {
    $resolved = if ($null -eq $Value) { "" } else { [string]$Value }
    if ([string]::IsNullOrWhiteSpace($resolved)) {
        $resolved = Read-Host $Prompt
    }
    $resolved = if ($null -eq $resolved) { "" } else { ([string]$resolved).Trim() }
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

function Assert-SafeDownloadRedirect([string]$InitialUrl, [Uri]$FinalUri, [string]$Label) {
    $initial = [Uri](Normalize-HttpsUrl -Value $InitialUrl -Name $Label)
    if (
        $null -eq $FinalUri -or
        -not $FinalUri.IsAbsoluteUri -or
        $FinalUri.Scheme -ne "https" -or
        [string]::IsNullOrWhiteSpace($FinalUri.Host) -or
        -not [string]::IsNullOrWhiteSpace($FinalUri.UserInfo) -or
        -not [string]::IsNullOrWhiteSpace($FinalUri.Fragment)
    ) {
        throw "$Label 重定向后的地址不安全。"
    }
    $githubAssetRedirect = (
        $initial.Host -eq "github.com" -and
        ($FinalUri.Host -eq "githubusercontent.com" -or $FinalUri.Host.EndsWith(".githubusercontent.com"))
    )
    if (-not [string]::IsNullOrWhiteSpace($FinalUri.Query) -and -not $githubAssetRedirect) {
        throw "$Label 重定向后的地址包含查询参数，拒绝下载。"
    }
}

function Test-ProjectRoot([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $false
    }
    return (
        (Test-Path -LiteralPath (Join-Path $Path "pyproject.toml") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Path "src\agent_memory_gateway") -PathType Container) -and
        (Test-Path -LiteralPath (Join-Path $Path "scripts\setup-shared-memory.ps1") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Path "scripts\start-sidecar.ps1") -PathType Leaf)
    )
}

function ConvertFrom-InstallProfile([string]$Json, [string]$Source) {
    try {
        $profileObject = $Json | ConvertFrom-Json -ErrorAction Stop
        $profile = ConvertTo-Hashtable -Value $profileObject
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
            if ([string]$agent["type"] -notin @("codex", "hermes", "openclaw", "other")) {
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
        $defaultPath = Join-Path $localDataRoot "memory-gateway\device-install.json"
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
    $codexHome = Join-Path $userHome ".codex"
    if ((Test-Path -LiteralPath $codexHome -PathType Container) -or (Test-CommandAvailable -Name "codex")) {
        $detected += "codex-$ResolvedDeviceId|codex|Codex"
    }
    $hermesSignals = @(
        (Join-Path $localDataRoot "hermes"),
        (Join-Path $localDataRoot "Hermes Studio"),
        (Join-Path $roamingDataRoot "hermes"),
        (Join-Path $userHome ".hermes")
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    if ((Test-CommandAvailable -Name "hermes") -or @($hermesSignals | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0) {
        $detected += "hermes-$ResolvedDeviceId|hermes|Hermes"
    }
    $openClawSignals = @(
        (Join-Path $localDataRoot "openclaw"),
        (Join-Path $roamingDataRoot "openclaw"),
        (Join-Path $userHome ".openclaw")
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    if ((Test-CommandAvailable -Name "openclaw") -or @($openClawSignals | Where-Object { Test-Path -LiteralPath $_ }).Count -gt 0) {
        $detected += "openclaw-$ResolvedDeviceId|openclaw|OpenClaw"
    }
    return @($detected)
}

function ConvertTo-Hashtable([object]$Value) {
    if ($null -eq $Value) {
        return $null
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $result = @{}
        foreach ($entry in $Value.GetEnumerator()) {
            $result[[string]$entry.Key] = ConvertTo-Hashtable -Value $entry.Value
        }
        return $result
    }
    if ($Value -is [pscustomobject]) {
        $result = @{}
        foreach ($property in $Value.PSObject.Properties) {
            $result[$property.Name] = ConvertTo-Hashtable -Value $property.Value
        }
        return $result
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        return @($Value | ForEach-Object { ConvertTo-Hashtable -Value $_ })
    }
    return $Value
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Save-OnboardProfile([System.Collections.IDictionary]$Profile, [string]$Gateway, [string]$Workspace, [string]$ResolvedDeviceId, [string[]]$AgentSpecs) {
    $agents = @()
    foreach ($spec in $AgentSpecs) {
        $parts = $spec.Split("|", 3)
        $template = $parts[0].Replace($ResolvedDeviceId, "{device_id}")
        if (($template.Split("{device_id}").Count - 1) -ne 1) {
            throw "Agent 安装实例 ID 必须包含设备 ID；自定义静态 ID 请使用 setup-shared-memory.ps1 高级入口。"
        }
        $agents += [ordered]@{
            type = $parts[1]
            display_name = $parts[2]
            installation_id_template = $template
        }
    }
    $value = [ordered]@{
        version = 1
        gateway_url = $Gateway
        default_workspace = $Workspace
        device_id_prefix = if ($Profile.Contains("device_id_prefix")) { [string]$Profile["device_id_prefix"] } else { "windows" }
        agents = $agents
    }
    $targetDirectory = Join-Path $localDataRoot "memory-gateway"
    New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    $target = Join-Path $targetDirectory "device-onboard.json"
    if (Test-Path -LiteralPath $target) {
        $existing = Get-Content -LiteralPath $target -Raw -Encoding utf8
        $expected = $value | ConvertTo-Json -Depth 5
        if ($existing.Trim() -ne $expected.Trim()) {
            throw "默认安装配置已存在且内容不同，拒绝覆盖：$target"
        }
        return $target
    }
    Write-Utf8NoBom -Path $target -Content ($value | ConvertTo-Json -Depth 5)
    return $target
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

function Get-StableReleaseSpec() {
    $manifestUrl = Normalize-HttpsUrl -Value $DefaultStableManifestUrl -Name "稳定发布清单"
    try {
        $response = Invoke-WebRequest -Uri $manifestUrl -TimeoutSec 20 -ErrorAction Stop
    }
    catch {
        throw "无法获取稳定发布清单。项目尚未发布稳定版本时，请等待 Release；仅开发测试可显式传入 -Channel development。"
    }
    $finalUri = if ($null -ne $response.BaseResponse.RequestMessage) {
        $response.BaseResponse.RequestMessage.RequestUri
    } elseif ($null -ne $response.BaseResponse.ResponseUri) {
        $response.BaseResponse.ResponseUri
    } else {
        $null
    }
    Assert-SafeDownloadRedirect -InitialUrl $manifestUrl -FinalUri $finalUri -Label "稳定发布清单"
    if ($response.RawContentLength -gt 65536 -or ([string]$response.Content).Length -gt 65536) {
        throw "稳定发布清单超过 64 KiB，拒绝使用。"
    }
    $manifest = ConvertFrom-InstallProfile -Json ([string]$response.Content) -Source $manifestUrl
    if ($manifest.Count -ne 2 -or -not $manifest.Contains("version") -or -not $manifest.Contains("release")) {
        throw "稳定发布清单结构无效：$manifestUrl"
    }
    if ([int]$manifest["version"] -ne 1 -or $manifest["release"] -isnot [System.Collections.IDictionary]) {
        throw "稳定发布清单版本或 release 无效：$manifestUrl"
    }
    $release = $manifest["release"]
    $allowedReleaseKeys = @("release_id", "archive_url", "sha256")
    if ($release.Count -ne $allowedReleaseKeys.Count) {
        throw "稳定发布清单包含不支持的字段：$manifestUrl"
    }
    foreach ($required in $allowedReleaseKeys) {
        if (-not $release.Contains($required) -or [string]::IsNullOrWhiteSpace([string]$release[$required])) {
            throw "稳定发布清单缺少 $required：$manifestUrl"
        }
    }
    if ([string]$release["release_id"] -notmatch '^[A-Za-z0-9._-]{1,96}$') {
        throw "稳定发布清单中的 release_id 无效。"
    }
    $release["archive_url"] = Normalize-HttpsUrl -Value ([string]$release["archive_url"]) -Name "稳定发布清单中的 archive_url"
    $release["sha256"] = ([string]$release["sha256"]).ToLowerInvariant()
    if ($release["sha256"] -notmatch '^[a-f0-9]{64}$') {
        throw "稳定发布清单中的 sha256 必须是 64 位十六进制摘要。"
    }
    return $release
}

function Save-ReleaseArchiveDownload([string]$ArchiveUrl, [string]$PartialPath, [string]$Label) {
    $maximumBytes = [Int64]$MaximumReleaseMegabytes * 1MB
    $client = [System.Net.Http.HttpClient]::new()
    $response = $null
    $input = $null
    $output = $null
    try {
        $response = $client.GetAsync([Uri]$ArchiveUrl, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "$Label 下载返回 HTTP $([int]$response.StatusCode)。"
        }
        Assert-SafeDownloadRedirect -InitialUrl $ArchiveUrl -FinalUri $response.RequestMessage.RequestUri -Label $Label
        if ($response.Content.Headers.ContentLength -and $response.Content.Headers.ContentLength -gt $maximumBytes) {
            throw "$Label 超过允许大小：$MaximumReleaseMegabytes MiB"
        }
        $input = $response.Content.ReadAsStream()
        $output = [System.IO.File]::Open($PartialPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        $buffer = New-Object byte[] 81920
        $total = [Int64]0
        while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $total += $read
            if ($total -gt $maximumBytes) {
                throw "$Label 超过允许大小：$MaximumReleaseMegabytes MiB"
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
}

function Save-VerifiedReleaseArchive([System.Collections.IDictionary]$Release) {
    $cacheRoot = Join-Path $localDataRoot "memory-gateway\downloads"
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
    Save-ReleaseArchiveDownload -ArchiveUrl ([string]$Release["archive_url"]) -PartialPath $partialPath -Label "发布包"
    if ((Get-FileSha256 -Path $partialPath) -ne $expectedHash) {
        throw "发布包 SHA-256 不匹配，已保留下载文件以便排查：$partialPath"
    }
    Move-Item -LiteralPath $partialPath -Destination $archivePath -ErrorAction Stop
    return $archivePath
}

function Save-DefaultMainArchive() {
    $cacheRoot = Join-Path $localDataRoot "memory-gateway\downloads"
    New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
    $downloadId = "main-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff'))-$PID"
    $partialPath = Join-Path $cacheRoot "$downloadId.zip.partial"
    if (Test-Path -LiteralPath $partialPath) {
        throw "发现未完成的 main 源码包下载，拒绝覆盖：$partialPath"
    }
    $archiveUrl = Normalize-HttpsUrl -Value $DefaultMainArchiveUrl -Name "默认 main 源码包"
    Save-ReleaseArchiveDownload -ArchiveUrl $archiveUrl -PartialPath $partialPath -Label "main 源码包"
    $archiveHash = Get-FileSha256 -Path $partialPath
    $archivePath = Join-Path $cacheRoot "$downloadId-$($archiveHash.Substring(0, 16)).zip"
    if (Test-Path -LiteralPath $archivePath) {
        throw "发现同名 main 源码包，拒绝覆盖：$archivePath"
    }
    Move-Item -LiteralPath $partialPath -Destination $archivePath -ErrorAction Stop
    return [pscustomobject]@{
        Id = $downloadId
        Path = $archivePath
        Sha256 = $archiveHash
        Url = $archiveUrl
    }
}

function Expand-SafeReleaseArchive([string]$ArchivePath, [string]$DestinationPath, [string]$Label) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    $destination = [System.IO.Path]::GetFullPath($DestinationPath).TrimEnd("\")
    $prefix = "$destination\"
    $maximumExpandedBytes = [Int64]$MaximumReleaseMegabytes * 4MB
    $maximumEntries = 10000
    $maximumEntryNameLength = 1024
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        if ($archive.Entries.Count -lt 1 -or $archive.Entries.Count -gt $maximumEntries) {
            throw "$Label 文件数量无效；最多允许 $maximumEntries 个条目。"
        }
        $total = [Int64]0
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace("/", "\")
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            if ($name.Length -gt $maximumEntryNameLength) {
                throw "$Label 包含过长的文件名，拒绝解压。"
            }
            if ([System.IO.Path]::IsPathRooted($name)) {
                throw "$Label 包含绝对路径，拒绝解压。"
            }
            if ($name.Contains(":")) {
                throw "$Label 包含不安全的文件名，拒绝解压。"
            }
            $target = [System.IO.Path]::GetFullPath((Join-Path $destination $name))
            if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "$Label 包含越界路径，拒绝解压。"
            }
            $unixMode = [int](($entry.ExternalAttributes -shr 16) -band 0xF000)
            if ($unixMode -eq 0xA000) {
                throw "$Label 包含符号链接，拒绝解压。"
            }
            if ($unixMode -notin @(0x0000, 0x4000, 0x8000)) {
                throw "$Label 包含特殊文件，拒绝解压。"
            }
            if ([Int64]$entry.Length -gt $maximumExpandedBytes) {
                throw "$Label 包含过大的单个文件，拒绝解压。"
            }
            $total += [Int64]$entry.Length
            if ($total -gt $maximumExpandedBytes) {
                throw "$Label 展开后超过允许大小：$($MaximumReleaseMegabytes * 4) MiB"
            }
        }
    }
    finally {
        $archive.Dispose()
    }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($ArchivePath, $DestinationPath)
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
    $release = Get-ReleaseSpec -Profile $Profile
    $source = "verified_release"
    if ($null -eq $release -and -not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        $localRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
        if (Test-ProjectRoot -Path $localRoot) {
            return [pscustomobject]@{ Source = "local"; Root = $localRoot }
        }
    }
    if ($null -eq $release -and $resolvedChannel -eq "stable") {
        try {
            $release = Get-StableReleaseSpec
            $source = "stable_release"
        }
        catch {
            Write-Warning $_.Exception.Message
            $fallback = Read-Host "是否明确改用开发版 main？仅建议开发测试使用 [y/N]"
            if ([string]$fallback -notmatch '^(?i:y|yes)$') {
                throw "没有可用稳定发布，且未确认使用开发通道。安装已停止。"
            }
            $script:resolvedChannel = "development"
        }
    }
    if ($null -ne $release) {
        $releaseRoot = Join-Path $localDataRoot "memory-gateway\releases\$($release["release_id"])-$($release["sha256"].Substring(0, 16))"
        if (Test-Path -LiteralPath $releaseRoot) {
            return [pscustomobject]@{ Source = "${source}_cache"; Root = (Resolve-ExtractedProjectRoot -ReleaseDirectory $releaseRoot); ReleaseId = $release["release_id"] }
        }
        $archivePath = Save-VerifiedReleaseArchive -Release $release
        New-Item -ItemType Directory -Path $releaseRoot -ErrorAction Stop | Out-Null
        try {
            Expand-SafeReleaseArchive -ArchivePath $archivePath -DestinationPath $releaseRoot -Label "发布包"
        }
        catch {
            throw "发布包已校验，但解压失败。为避免覆盖诊断现场，目录已保留：$releaseRoot"
        }
        return [pscustomobject]@{ Source = "${source}_download"; Root = (Resolve-ExtractedProjectRoot -ReleaseDirectory $releaseRoot); ReleaseId = $release["release_id"] }
    }

    $mainArchive = Save-DefaultMainArchive
    $releaseRoot = Join-Path $localDataRoot "memory-gateway\releases\$($mainArchive.Id)-$($mainArchive.Sha256.Substring(0, 16))"
    if (Test-Path -LiteralPath $releaseRoot) {
        throw "发现同名 main 源码包目录，拒绝覆盖：$releaseRoot"
    }
    New-Item -ItemType Directory -Path $releaseRoot -ErrorAction Stop | Out-Null
    try {
        Expand-SafeReleaseArchive -ArchivePath $mainArchive.Path -DestinationPath $releaseRoot -Label "main 源码包"
    }
    catch {
        throw "main 源码包已下载，但解压失败。为避免覆盖诊断现场，目录已保留：$releaseRoot"
    }
    return [pscustomobject]@{
        Source = "default_main_archive"
        Root = (Resolve-ExtractedProjectRoot -ReleaseDirectory $releaseRoot)
        ArchiveSha256 = $mainArchive.Sha256
        ArchiveUrl = $mainArchive.Url
    }
}

$existingRuntimePython = Join-Path $localDataRoot "memory-gateway\runtime\Scripts\python.exe"
$existingRuntimeConfig = Join-Path $localDataRoot "memory-gateway\runtime.json"
$legacySidecarKey = Join-Path $localDataRoot "memory-gateway\secrets\pc-sidecar.env"
$explicitInstallInput = (
    -not [string]::IsNullOrWhiteSpace($ProfilePath) -or
    -not [string]::IsNullOrWhiteSpace($ProfileUrl) -or
    -not [string]::IsNullOrWhiteSpace($GatewayUrl) -or
    -not [string]::IsNullOrWhiteSpace($DefaultWorkspace) -or
    -not [string]::IsNullOrWhiteSpace($DeviceId) -or
    $Agent.Count -gt 0 -or
    -not [string]::IsNullOrWhiteSpace($GatewayCaCertificate) -or
    $NoAutostart
)
if (
    -not $Plan -and
    -not $Resume -and
    -not $explicitInstallInput -and
    (Test-Path -LiteralPath $existingRuntimePython -PathType Leaf) -and
    (Test-Path -LiteralPath $existingRuntimeConfig -PathType Leaf)
) {
    Write-Output "共享记忆设备已经安装。当前状态："
    & $existingRuntimePython -m agent_memory_gateway.device_runtime status --platform windows
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Output "需要检查或修复时运行：memory-device doctor / memory-device repair"
    exit 0
}
if (
    -not $Plan -and
    -not (Test-Path -LiteralPath $existingRuntimeConfig -PathType Leaf) -and
    (Test-Path -LiteralPath $legacySidecarKey -PathType Leaf)
) {
    throw "检测到旧版 Windows 安装，未发现新版 runtime.json。为避免重复配对，安装器不会自动覆盖；请先保留现有配置并按部署文档执行迁移。"
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
    $selectedTypes = (Read-Host "没有自动检测到 Agent。请输入 codex、hermes、openclaw，可用逗号分隔").Split(",") |
        ForEach-Object { $_.Trim().ToLowerInvariant() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    if ($selectedTypes.Count -eq 0 -or @($selectedTypes | Where-Object { $_ -notin @("codex", "hermes", "openclaw") }).Count -gt 0) {
        throw "Agent 类型无效；只接受 codex、hermes、openclaw。"
    }
    $resolvedAgents = @($selectedTypes | Select-Object -Unique | ForEach-Object {
        $displayName = if ($_ -eq "codex") { "Codex" } elseif ($_ -eq "hermes") { "Hermes" } else { "OpenClaw" }
        "$_-$resolvedDeviceId|$_|$displayName"
    })
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
    project_source = if ($profile.Contains("release")) { "verified_release_download" } elseif (
        -not [string]::IsNullOrWhiteSpace($PSScriptRoot) -and
        (Test-ProjectRoot -Path ([System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))))
    ) { "local" } elseif ($resolvedChannel -eq "stable") { "stable_release_download" } else { "default_main_archive" }
    next_step = "确认后输入一次性配对码；配对码只在隐藏输入中使用，不写入配置或命令行。"
}
if ($Plan) {
    $installPlan
    exit 0
}

$projectResolution = Resolve-ProjectRoot -Profile $profile
if ($projectResolution.Source -eq "default_main_archive") {
    Write-Output "已下载 GitHub main 源码包，SHA-256：$($projectResolution.ArchiveSha256)"
}
elseif ($projectResolution.Source -like "stable_release_*") {
    Write-Output "已校验稳定发布：$($projectResolution.ReleaseId)"
}
Write-Output "正在准备共享记忆端侧：$($resolvedAgents.Count) 个 Agent，自动启动=$(-not $NoAutostart)。"
$runtimeRoot = Join-Path $localDataRoot "memory-gateway\runtime"
$runtimePython = Join-Path $runtimeRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    if (Test-Path -LiteralPath $runtimeRoot) {
        throw "共享记忆运行环境不完整：$runtimeRoot。请先运行 memory-device doctor，不会自动删除已有目录。"
    }
    $bootstrapPython = (Get-Command -Name "python" -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    & $bootstrapPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    if ($LASTEXITCODE -ne 0) { throw "需要 Python 3.10 或更高版本。安装或升级 Python 后重新运行同一条命令。" }
    & $bootstrapPython -m venv $runtimeRoot
    if ($LASTEXITCODE -ne 0) { throw "无法创建共享记忆独立运行环境。" }
}
& $runtimePython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "共享记忆运行环境需要 Python 3.10 或更高版本：$runtimeRoot" }
& $runtimePython -m pip install --disable-pip-version-check --upgrade "$($projectResolution.Root)[mcp]"
if ($LASTEXITCODE -ne 0) { throw "无法安装共享记忆运行依赖。请检查网络和 pip 配置。" }
# 主分支快照可能沿用相同版本号；强制刷新本项目代码，但不重复安装第三方依赖。
& $runtimePython -m pip install --disable-pip-version-check --force-reinstall --no-deps $projectResolution.Root
if ($LASTEXITCODE -ne 0) { throw "无法更新共享记忆运行程序。" }
$onboardProfile = Save-OnboardProfile -Profile $profile -Gateway $resolvedGatewayUrl -Workspace $resolvedWorkspace -ResolvedDeviceId $resolvedDeviceId -AgentSpecs $resolvedAgents
$onboardArguments = @(
    "-m", "agent_memory_gateway.device_runtime", "onboard",
    "--profile", $onboardProfile,
    "--platform", "windows",
    "--device-id", $resolvedDeviceId,
    "--device-name", $resolvedDeviceName,
    "--python-executable", $runtimePython
)
if ($NoAutostart) { $onboardArguments += "--no-autostart" }
if ($Resume) { $onboardArguments += "--resume" }
if (-not [string]::IsNullOrWhiteSpace($GatewayCaCertificate)) {
    $onboardArguments += @("--gateway-ca-certificate", $GatewayCaCertificate)
}
& $runtimePython @onboardArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$runtimeScripts = Split-Path -Parent $runtimePython
$launcherRoot = Join-Path $localDataRoot "memory-gateway\bin"
New-Item -ItemType Directory -Path $launcherRoot -Force | Out-Null
$launcherPython = Join-Path $launcherRoot "memory-device-launcher.py"
$launcherCommand = Join-Path $launcherRoot "memory-device.cmd"
$launcherSource = Join-Path $projectResolution.Root "scripts\memory-device-launcher.py"
if (-not (Test-Path -LiteralPath $launcherSource -PathType Leaf)) {
    throw "发布包缺少稳定维护启动器：$launcherSource"
}
Copy-Item -LiteralPath $launcherSource -Destination $launcherPython -Force
$launcherCommandText = "@`"$runtimePython`" `"$launcherPython`" %*`r`n"
Write-Utf8NoBom -Path $launcherCommand -Content $launcherCommandText
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$userEntries = @($userPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($userEntries -notcontains $launcherRoot) {
    [Environment]::SetEnvironmentVariable("Path", (($userEntries + $launcherRoot) -join ";"), "User")
}
if (@($env:Path -split ";") -notcontains $launcherRoot) {
    $env:Path = "$launcherRoot;$env:Path"
}
Write-Output "后续维护：memory-device status、memory-device doctor、memory-device repair、memory-device uninstall"
