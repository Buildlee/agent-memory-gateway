[CmdletBinding()]
param(
    [string]$DemoHome = "$env:LOCALAPPDATA\agent-memory-gateway-demo",

    [ValidateRange(1024, 65535)]
    [int]$Port = 8787,

    [string]$PythonExecutable = "python",

    [bool]$RunVerification = $true
)

$ErrorActionPreference = "Stop"

$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$bootstrapPython = Get-Command $PythonExecutable -ErrorAction Stop
if ($bootstrapPython.CommandType -notin @("Application", "ExternalScript")) {
    throw "PythonExecutable 必须是可执行文件：$PythonExecutable"
}

$versionText = & $bootstrapPython.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) {
    throw "无法读取 Python 版本：$($bootstrapPython.Source)"
}
if ([Version]$versionText -lt [Version]"3.10") {
    throw "需要 Python 3.10 或更高版本，当前版本是 $versionText。"
}

$environmentRoot = Join-Path $projectRoot ".local-demo-venv"
$environmentPython = Join-Path $environmentRoot "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $environmentPython -PathType Leaf)) {
    if (Test-Path -LiteralPath $environmentRoot) {
        throw "本地演示虚拟环境不完整：$environmentRoot。为避免覆盖已有文件，脚本不会自动清理。"
    }
    Write-Output "正在创建本地演示虚拟环境…"
    & $bootstrapPython.Source -m venv $environmentRoot
    if ($LASTEXITCODE -ne 0) {
        throw "创建本地演示虚拟环境失败：$environmentRoot"
    }
}

Write-Output "正在安装本地演示所需依赖…"
& $environmentPython -m pip install --disable-pip-version-check -e $projectRoot
if ($LASTEXITCODE -ne 0) {
    throw "安装本地演示依赖失败。请检查网络、Python 的 pip 配置和错误输出后重试。"
}

& (Join-Path $PSScriptRoot "start-local-demo.ps1") `
    -DemoHome $DemoHome `
    -Port $Port `
    -PythonExecutable $environmentPython `
    -RunVerification $RunVerification
