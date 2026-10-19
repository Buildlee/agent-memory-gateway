[CmdletBinding()]
param(
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

    [string]$PythonExecutable = "python",

    # 单次健康探测超时；对端假死（TCP 通但不回 HTTP）就是靠它判定的
    [int]$HealthTimeoutMs = 3000,

    # 拉起后等待 Sidecar 就绪的时间
    [int]$StartupTimeoutSec = 30,

    [string]$LogFile = "$env:LOCALAPPDATA\memory-gateway\logs\sidecar-ensure.log",

    # 端口在听但不应答时，连续这么多分钟都不健康才判定假死并接管（避免误杀繁忙进程）
    [int]$TakeoverAfterMin = 10,

    # 端口被占但健康检查失败时，默认只报告不处理
    [switch]$NoTakeover
)

# 幂等启动器：计划任务每 5 分钟调用一次。
#   健康 → 直接 exit 0（任务实例立即结束，不再被 IgnoreNew 判为"拒绝启动"）
#   端口空闲 / 假死 → 以脱离任务进程树的方式拉起 start-sidecar.ps1，再等它就绪
# 为什么不直接让任务常驻跑 start-sidecar.ps1：那样任务实例永远是 Running，
# 之后每次触发都被 MultipleInstances=IgnoreNew 拒绝，记成 0x800710E0，
# 而且进程假死时没有任何重启机会。

$ErrorActionPreference = "Stop"

function Write-EnsureLog([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss.fff"), $Message
    try {
        $dir = Split-Path -Parent $LogFile
        if ($dir -and -not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        Add-Content -LiteralPath $LogFile -Value $line -Encoding utf8
        $item = Get-Item -LiteralPath $LogFile
        if ($item.Length -gt 512 * 1024) {
            Set-Content -LiteralPath $LogFile -Value (Get-Content -LiteralPath $LogFile -Tail 400) -Encoding utf8
        }
    } catch {
        # 日志失败不能影响守护判定
    }
}

function Test-PortOpen([int]$Port, [int]$TimeoutMs) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            return $false
        }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# 返回 Sidecar 的 /health 响应体；连不上或超时返回 $null。
# 注意：/health 由全局鉴权中间件拦住时返回 {"error":"LOCAL_AUTH_REQUIRED"}，
# 这仍然是"进程活着并在正常应答"的证据，所以按响应体指纹判定，而不是看状态码。
function Get-HealthBody([int]$Port, [int]$TimeoutMs) {
    $request = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/health")
        $request.Method = "GET"
        $request.Timeout = $TimeoutMs
        $request.ReadWriteTimeout = $TimeoutMs
        $request.Proxy = $null
        $request.KeepAlive = $false
        $response = $null
        try {
            $response = $request.GetResponse()
        } catch [System.Net.WebException] {
            $response = $_.Exception.Response
        }
        if (-not $response) {
            return $null
        }
        try {
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
            return $reader.ReadToEnd()
        } finally {
            $response.Close()
        }
    } catch {
        return $null
    }
}

function Test-SidecarHealthy([int]$Port, [int]$TimeoutMs) {
    $body = Get-HealthBody -Port $Port -TimeoutMs $TimeoutMs
    if (-not $body) {
        return @{ ok = $false; detail = "无 HTTP 应答" }
    }
    # LOCAL_AUTH_REQUIRED 是本机 Sidecar 的固定指纹：端口被别的程序占用时不会出现
    if ($body -match "LOCAL_AUTH_REQUIRED") {
        return @{ ok = $true; detail = "health 已应答（需要本地鉴权）" }
    }
    if ($body -match '"(?:status|state)"\s*:\s*"?ok' -or $body -match '"ok"\s*:\s*true') {
        return @{ ok = $true; detail = "health=ok" }
    }
    return @{ ok = $false; detail = "应答不认识：$($body.Trim() -replace '\s+', ' ') ".Trim() }
}

function Wait-SidecarHealthy([int]$Port, [int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $last = $null
    while ((Get-Date) -lt $deadline) {
        $last = Test-SidecarHealthy -Port $Port -TimeoutMs 2000
        if ($last.ok) {
            return $last
        }
        Start-Sleep -Milliseconds 1000
    }
    return $last
}

function Get-ListenerPid([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($conn) {
            return [int]$conn.OwningProcess
        }
    } catch {
        # Get-NetTCPConnection 在部分精简环境不可用，退回 netstat 解析
        $line = netstat -ano -p tcp 2>$null | Select-String -Pattern (":$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") | Select-Object -First 1
        if ($line -and $line.Matches.Count -gt 0) {
            return [int]$line.Matches[0].Groups[1].Value
        }
    }
    return 0
}

function Test-SidecarProcess([int]$ProcessId) {
    if ($ProcessId -le 0) {
        return $false
    }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $proc) {
        return $false
    }
    # 只认 Sidecar 守护进程本身，绝不误杀占用同端口的其它程序
    return [bool]($proc.CommandLine -match "agent_memory_gateway[./]sidecar_daemon")
}

function Quote-Argument([string]$Value) {
    '"' + $Value.Replace('"', '""') + '"'
}

function Start-SidecarDetached {
    $startScript = Join-Path $PSScriptRoot "start-sidecar.ps1"
    if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
        throw "找不到 Sidecar 启动脚本：$startScript"
    }
    $pwshCommand = Get-Command -Name "pwsh" -ErrorAction SilentlyContinue
    if (-not $pwshCommand -or -not (Test-Path -LiteralPath $pwshCommand.Source -PathType Leaf)) {
        # 只用 PowerShell 7：本仓库脚本含中文注解且未加 BOM，5.1 会按 ANSI 解析出错
        throw "找不到 PowerShell 7（pwsh），无法拉起 Sidecar"
    }
    $hostExe = $pwshCommand.Source

    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", (Quote-Argument $startScript),
        "-GatewayUrl", (Quote-Argument $GatewayUrl),
        "-AllowedAgents", (Quote-Argument $AllowedAgents),
        "-DeviceId", (Quote-Argument $DeviceId),
        "-DefaultWorkspace", (Quote-Argument $DefaultWorkspace),
        "-HeartbeatAgent", (Quote-Argument $HeartbeatAgent),
        "-CredentialTarget", (Quote-Argument $CredentialTarget),
        "-SidecarKeyFile", (Quote-Argument $SidecarKeyFile),
        "-MemoryHome", (Quote-Argument $MemoryHome),
        "-Port", $Port,
        "-GatewayCaCertificate", (Quote-Argument $GatewayCaCertificate),
        "-PythonExecutable", (Quote-Argument $PythonExecutable)
    ) -join " "

    # 子进程的 stdout/stderr 必须落盘，否则 start-sidecar.ps1 抛出的参数或密钥错误会彻底静默
    $logDir = Split-Path -Parent $LogFile
    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    $outLog = Join-Path $logDir "sidecar-start.out.log"
    $errLog = Join-Path $logDir "sidecar-start.err.log"

    # Start-Process 不等待：任务实例立刻结束，守护进程脱离任务进程树继续存活
    # （已实测：动作退出后子进程仍在运行，LastTaskResult=0）
    Start-Process -FilePath $hostExe -ArgumentList $arguments -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog | Out-Null
}

# ---------- 主流程 ----------
$stateFile = Join-Path (Split-Path -Parent $LogFile) "sidecar-unhealthy-since.txt"

function Clear-UnhealthyMarker {
    if (Test-Path -LiteralPath $stateFile) {
        Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
    }
}

function Set-UnhealthyMarker {
    try {
        Set-Content -LiteralPath $stateFile -Value (Get-Date).ToString("o") -Encoding utf8
    } catch { }
}

function Get-UnhealthyAgeMinutes {
    if (-not (Test-Path -LiteralPath $stateFile)) {
        return -1
    }
    try {
        $since = [datetime]::Parse((Get-Content -LiteralPath $stateFile -Raw).Trim())
        return ((Get-Date) - $since).TotalMinutes
    } catch {
        return -1
    }
}

# 端口空闲就不必走 HTTP 超时（连不上要等满 3 秒，白等）
if (-not (Test-PortOpen -Port $Port -TimeoutMs 1000)) {
    Write-EnsureLog "未在运行（端口空闲）→ 拉起"
    Clear-UnhealthyMarker
} else {
    $health = Test-SidecarHealthy -Port $Port -TimeoutMs $HealthTimeoutMs
    if ($health.ok) {
        Write-EnsureLog "已在运行：$($health.detail)（pid=$(Get-ListenerPid $Port)）→ 跳过"
        Clear-UnhealthyMarker
        exit 0
    }

    # 端口短暂抖动（例如刚重启）时不要立刻再拉一个实例
    Start-Sleep -Seconds 2
    $health = Test-SidecarHealthy -Port $Port -TimeoutMs $HealthTimeoutMs
    if ($health.ok) {
        Write-EnsureLog "二次确认已恢复：$($health.detail) → 跳过"
        Clear-UnhealthyMarker
        exit 0
    }

    $listenerPid = Get-ListenerPid $Port
    if ($listenerPid -le 0) {
        Write-EnsureLog "端口已关闭（$($health.detail)）→ 拉起"
    } elseif (-not (Test-SidecarProcess $listenerPid)) {
        # 只认 Sidecar 守护进程的命令行，绝不接管/误杀其它程序
        Write-EnsureLog "端口 $Port 被非 Sidecar 进程 $listenerPid 占用且不应答（$($health.detail)）；不接管，退出码 1"
        exit 1
    } else {
        # 进程还在、端口在听、但不应答：可能只是忙（同步大工作区），也可能真挂了。
        # 单次探测不足以判死，先记录起始时间，连续 ${TakeoverAfterMin} 分钟都不健康才接管。
        $ageMin = Get-UnhealthyAgeMinutes
        if ($NoTakeover) {
            Write-EnsureLog "Sidecar 进程 $listenerPid 占端口但不应答（$($health.detail)）；-NoTakeover 已启用，不处理，退出码 1"
            exit 1
        }
        if ($ageMin -lt 0) {
            Set-UnhealthyMarker
            Write-EnsureLog "Sidecar 进程 $listenerPid 占端口但不健康（$($health.detail)）→ 记为首次观察，不重启（避免误杀繁忙进程）"
            exit 0
        }
        if ($ageMin -lt $TakeoverAfterMin) {
            Write-EnsureLog "仍不健康已 $([math]::Round($ageMin, 1)) 分钟（阈值 ${TakeoverAfterMin} 分钟）→ 继续观察"
            exit 0
        }
        Write-EnsureLog "Sidecar 进程 $listenerPid 持续 $([math]::Round($ageMin, 1)) 分钟不健康（$($health.detail)）→ 结束并重启"
        Stop-Process -Id $listenerPid -Force -ErrorAction SilentlyContinue
        $freed = $false
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 500
            if (-not (Test-PortOpen -Port $Port -TimeoutMs 500)) {
                $freed = $true
                break
            }
        }
        Clear-UnhealthyMarker
        if (-not $freed) {
            Write-EnsureLog "端口 $Port 在 10 秒内未释放，放弃本次拉起，退出码 1"
            exit 1
        }
    }
}

$ready = $null
try {
    Start-SidecarDetached
} catch {
    Write-EnsureLog "拉起失败：$($_.Exception.Message)，退出码 1"
    exit 1
}

$ready = Wait-SidecarHealthy -Port $Port -TimeoutSec $StartupTimeoutSec
if ($ready.ok) {
    Clear-UnhealthyMarker
    Write-EnsureLog "拉起成功：$($ready.detail)（pid=$(Get-ListenerPid $Port)）"
    exit 0
}

# 拉起失败时把子进程的 stderr 尾部带进日志，否则只能靠人工去翻文件
$tail = ""
try {
    $errLog = Join-Path (Split-Path -Parent $LogFile) "sidecar-start.err.log"
    if (Test-Path -LiteralPath $errLog) {
        $tail = (Get-Content -LiteralPath $errLog -Tail 5) -join " / "
    }
} catch { }
$tailNote = ""
if (-not [string]::IsNullOrWhiteSpace($tail)) {
    $tailNote = "；stderr: $tail"
}
Write-EnsureLog "拉起后 ${StartupTimeoutSec}s 内未就绪（$($ready.detail)）$tailNote，退出码 1"
exit 1
