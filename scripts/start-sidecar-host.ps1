[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$SidecarArguments
)

$ErrorActionPreference = "Stop"

# Windows Store 更新会替换带版本号的 pwsh.exe 路径。计划任务由系统自带的
# Windows PowerShell 启动，再在当前用户会话中解析并调用最新的 PowerShell 7。
$pwsh = Get-Command -Name "pwsh" -CommandType Application -ErrorAction Stop
if (-not (Test-Path -LiteralPath $pwsh.Source -PathType Leaf)) {
    throw "找不到可用的 PowerShell 7 启动程序"
}

$startScript = Join-Path $PSScriptRoot "start-sidecar.ps1"
if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
    throw "找不到 Sidecar 启动脚本：$startScript"
}

& $pwsh.Source -NoProfile -ExecutionPolicy Bypass -File $startScript @SidecarArguments
exit $LASTEXITCODE
