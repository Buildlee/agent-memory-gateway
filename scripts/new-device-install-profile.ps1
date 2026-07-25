[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$GatewayUrl,

    [Parameter(Mandatory)]
    [string]$DefaultWorkspace,

    [string[]]$AgentType = @("codex", "hermes"),

    [string]$DeviceIdPrefix = "windows",

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
$profile | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $target -Encoding utf8NoBOM
[pscustomobject]@{
    status = "ready"
    output_path = $target
    next_step = "将此非敏感配置交付到客户端默认路径，或让客户端运行 memory-device-install.ps1 -ProfilePath <此文件>。一次性配对码仍由客户端隐藏输入。"
}
