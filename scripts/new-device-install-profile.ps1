[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$GatewayUrl,

    [Parameter(Mandatory)]
    [string]$DefaultWorkspace,

    [string[]]$AgentType = @("codex", "hermes"),

    [string]$DeviceIdPrefix = "windows",

    [string]$ReleaseArchiveUrl = "",

    [string]$ReleaseSha256 = "",

    [string]$ReleaseArchivePath = "",

    [string]$ReleaseId = "agent-memory-gateway",

    [Parameter(Mandatory)]
    [string]$OutputPath
)

$ErrorActionPreference = "Stop"

try {
    $gatewayUri = [Uri]$GatewayUrl
}
catch {
    throw "GatewayUrl 必须是 HTTPS 地址。"
}
if (
    -not $gatewayUri.IsAbsoluteUri -or
    $gatewayUri.Scheme -ne "https" -or
    [string]::IsNullOrWhiteSpace($gatewayUri.Host) -or
    -not [string]::IsNullOrWhiteSpace($gatewayUri.UserInfo) -or
    -not [string]::IsNullOrWhiteSpace($gatewayUri.Query) -or
    -not [string]::IsNullOrWhiteSpace($gatewayUri.Fragment)
) {
    throw "GatewayUrl 必须是不带账号、查询参数或片段的 HTTPS 地址。"
}
$GatewayUrl = $gatewayUri.AbsoluteUri.TrimEnd("/")
if ($DefaultWorkspace -notmatch '^[A-Za-z0-9_.@:-]+$') {
    throw "DefaultWorkspace 无效。"
}
if ($DeviceIdPrefix -notmatch '^[A-Za-z0-9_.@:-]+$') {
    throw "DeviceIdPrefix 无效。"
}
if (-not $AgentType -or @($AgentType | Select-Object -Unique).Count -ne $AgentType.Count) {
    throw "AgentType 至少包含一个不重复的 Agent 类型。"
}
if (-not [string]::IsNullOrWhiteSpace($ReleaseArchivePath)) {
    if (-not (Test-Path -LiteralPath $ReleaseArchivePath -PathType Leaf)) {
        throw "找不到 ReleaseArchivePath：$ReleaseArchivePath"
    }
    $computedReleaseHash = (Get-FileHash -LiteralPath $ReleaseArchivePath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if (-not [string]::IsNullOrWhiteSpace($ReleaseSha256) -and $ReleaseSha256.ToLowerInvariant() -ne $computedReleaseHash) {
        throw "ReleaseSha256 与 ReleaseArchivePath 的实际摘要不一致。"
    }
    $ReleaseSha256 = $computedReleaseHash
}
if ([string]::IsNullOrWhiteSpace($ReleaseArchiveUrl)) {
    if (-not [string]::IsNullOrWhiteSpace($ReleaseSha256)) {
        throw "提供 ReleaseSha256 时必须同时提供 ReleaseArchiveUrl。"
    }
}
elseif ([string]::IsNullOrWhiteSpace($ReleaseSha256)) {
    throw "ReleaseArchiveUrl 需要 ReleaseSha256，或提供 ReleaseArchivePath 自动计算摘要。"
}
if (-not [string]::IsNullOrWhiteSpace($ReleaseArchiveUrl)) {
    try {
        $releaseUri = [Uri]$ReleaseArchiveUrl
    }
    catch {
        throw "ReleaseArchiveUrl 必须是 HTTPS 地址。"
    }
    if (
        -not $releaseUri.IsAbsoluteUri -or
        $releaseUri.Scheme -ne "https" -or
        [string]::IsNullOrWhiteSpace($releaseUri.Host) -or
        -not [string]::IsNullOrWhiteSpace($releaseUri.UserInfo) -or
        -not [string]::IsNullOrWhiteSpace($releaseUri.Query) -or
        -not [string]::IsNullOrWhiteSpace($releaseUri.Fragment)
    ) {
        throw "ReleaseArchiveUrl 必须是不带账号、查询参数或片段的 HTTPS 地址。"
    }
    $ReleaseArchiveUrl = $releaseUri.AbsoluteUri.TrimEnd("/")
    $ReleaseSha256 = $ReleaseSha256.ToLowerInvariant()
    if ($ReleaseSha256 -notmatch '^[a-f0-9]{64}$') {
        throw "ReleaseSha256 必须是 64 位十六进制摘要。"
    }
    if ($ReleaseId -notmatch '^[A-Za-z0-9._-]{1,96}$') {
        throw "ReleaseId 无效。"
    }
}

$displayNames = @{
    codex = "Codex"
    hermes = "Hermes"
    other = "Other Agent"
}
$agents = @()
foreach ($type in $AgentType) {
    if ($type -notin @("codex", "hermes", "other")) {
        throw "AgentType 只能是 codex、hermes 或 other。"
    }
    $agents += [ordered]@{
        type = $type
        display_name = $displayNames[$type]
        installation_id_template = "$type-{device_id}"
    }
}

$target = [System.IO.Path]::GetFullPath($OutputPath)
if (Test-Path -LiteralPath $target) {
    throw "拒绝覆盖已有安装配置：$target"
}
$parent = Split-Path -Parent $target
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

$profile = [ordered]@{
    version = 1
    gateway_url = $GatewayUrl
    default_workspace = $DefaultWorkspace
    device_id_prefix = $DeviceIdPrefix
    agents = $agents
}
if (-not [string]::IsNullOrWhiteSpace($ReleaseArchiveUrl)) {
    $profile.release = [ordered]@{
        release_id = $ReleaseId
        archive_url = $ReleaseArchiveUrl
        sha256 = $ReleaseSha256
    }
}
$profile | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $target -Encoding utf8NoBOM
[pscustomobject]@{
    status = "ready"
    output_path = $target
    next_step = "将此非敏感配置交付到客户端默认路径，或让客户端运行 memory-device-install.ps1 -ProfilePath <此文件>。一次性配对码仍由客户端隐藏输入。"
}
