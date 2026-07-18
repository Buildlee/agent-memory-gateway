[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$hookDirectory = Join-Path $projectRoot '.githooks'
$prePushHook = Join-Path $hookDirectory 'pre-push'

if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.git') -PathType Container)) {
    throw '当前目录不是 Git 仓库，无法安装推送前检查。'
}
if (-not (Test-Path -LiteralPath $prePushHook -PathType Leaf)) {
    throw "找不到推送前检查：$prePushHook"
}

Push-Location $projectRoot
try {
    git config core.hooksPath .githooks
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}

Write-Output 'git_hooks=ready'
