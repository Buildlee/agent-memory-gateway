[CmdletBinding()]
param(
    [ValidateSet("demo", "server", "device", "container", "verify")]
    [string]$Mode = "device",

    [string]$PythonExecutable = "python",

    [bool]$BootstrapRuntime = $true,

    [string]$DemoHome = "$env:LOCALAPPDATA\agent-memory-gateway-demo",

    [ValidateRange(1024, 65535)]
    [int]$DemoPort = 8787,

    [Parameter()]
    [string]$SshHost,

    [ValidateRange(1, 65535)]
    [int]$SshPort = 22,

    [string]$RemoteRoot,

    [string]$SecretsFile,

    [string]$BackendNetwork = "memory-backend",

    [string]$GatewayPublicName,

    [string]$GatewayBindAddress,

    [ValidateRange(1024, 65535)]
    [int]$HttpsPort = 8443,

    [ValidateSet("slim", "split")]
    [string]$DeploymentProfile = "slim",

    [string]$AdminEnvironmentFile,

    [switch]$Apply,

    [string]$GatewayUrl,

    [string]$DeviceId,

    [string]$DeviceName = $env:COMPUTERNAME,

    [ValidateSet("windows", "nas", "other")]
    [string]$DeviceType = "windows",

    [string[]]$Agent = @(),

    [string]$DefaultWorkspace,

    [string]$CredentialTarget = "AgentMemoryGateway/local-device",

    [string]$CredentialUsername = $env:USERNAME,

    [switch]$UseExistingCredential,

    [string]$DeviceKeyFile = "$env:LOCALAPPDATA\memory-gateway\secrets\device-identity.pem",

    [string]$SidecarKeyFile = "$env:LOCALAPPDATA\memory-gateway\secrets\pc-sidecar.env",

    [AllowEmptyString()]
    [string]$GatewayCaCertificate = "",

    [ValidateRange(1024, 65535)]
    [int]$SidecarPort = 8766,

    [switch]$InstallAutostart,

    [string]$TaskName = "MemoryGatewaySidecar",

    [string]$McpOutputDirectory = "$env:LOCALAPPDATA\memory-gateway\mcp"

    ,

    [string]$ClientContainerName,

    [string]$ContainerStateDirectory,

    [string]$TenantId,

    [string]$UserId,

    [string]$ContainerGatewayUrl = "http://gateway:8787",

    [string]$ContainerCapabilities = "memory.feedback,memory.forget,memory.read_context,memory.search,memory.sync,memory.write_event",

    [string]$ContainerUser = "1000:1001",

    [switch]$Resume
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..") -ErrorAction Stop).ProviderPath

function Require-Value([string]$Name, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "缺少 $Name。"
    }
}

function Get-SourcePython([string]$RequestedExecutable) {
    $python = Get-Command -Name $RequestedExecutable -ErrorAction Stop
    if ($python.CommandType -notin @("Application", "ExternalScript")) {
        throw "PythonExecutable 必须是可执行文件：$RequestedExecutable"
    }
    return $python.Source
}

function Get-DevicePython([string]$RequestedExecutable, [bool]$CreateRuntime) {
    $bootstrapPython = Get-SourcePython -RequestedExecutable $RequestedExecutable
    if (-not $CreateRuntime) {
        return $bootstrapPython
    }
    $runtimeRoot = Join-Path $projectRoot ".shared-memory-venv"
    $runtimePython = Join-Path $runtimeRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
        if (Test-Path -LiteralPath $runtimeRoot) {
            throw "共享记忆运行环境不完整：$runtimeRoot。为避免覆盖已有文件，脚本不会自动清理。"
        }
        Write-Output "正在创建独立的共享记忆运行环境…"
        & $bootstrapPython -m venv $runtimeRoot
        if ($LASTEXITCODE -ne 0) {
            throw "无法创建共享记忆运行环境：$runtimeRoot"
        }
        Write-Output "正在安装 Sidecar 和 MCP 所需依赖…"
        & $runtimePython -m pip install --disable-pip-version-check -e "$projectRoot[mcp]"
        if ($LASTEXITCODE -ne 0) {
            throw "无法安装共享记忆运行环境依赖。请检查网络和 pip 配置后重试。"
        }
    }
    & $runtimePython -c "import cryptography; import mcp; import agent_memory_gateway"
    if ($LASTEXITCODE -ne 0) {
        throw "共享记忆运行环境缺少依赖：$runtimeRoot。脚本不会自动覆盖它，请先检查后再重新安装。"
    }
    return $runtimePython
}

function ConvertTo-AgentSpec([string]$RawAgent) {
    $parts = $RawAgent.Split("|", 3)
    if ($parts.Count -ne 3) {
        throw "Agent 必须使用三段格式：安装实例 ID|类型|显示名。"
    }
    $agentId = $parts[0].Trim()
    $agentType = $parts[1].Trim()
    $agentName = $parts[2].Trim()
    if ($agentId -notmatch "^[A-Za-z0-9_.@:-]+$") {
        throw "Agent 安装实例 ID 只能使用字母、数字、点、下划线、@、冒号或连字符。"
    }
    if ($agentType -notin @("codex", "hermes", "other")) {
        throw "Agent 类型只能是 codex、hermes 或 other。"
    }
    if ([string]::IsNullOrWhiteSpace($agentName) -or $agentName.Length -gt 256) {
        throw "Agent 显示名无效。"
    }
    return [pscustomobject]@{
        Id = $agentId
        Type = $agentType
        Name = $agentName
    }
}

function Get-SidecarAuthToken([string]$KeyFile) {
    $encodedKey = ""
    Get-Content -LiteralPath $KeyFile | ForEach-Object {
        if ($_ -match "^MEMORY_OUTBOX_KEY=(.+)$") {
            $encodedKey = $Matches[1]
        }
    }
    if ([string]::IsNullOrWhiteSpace($encodedKey)) {
        throw "Sidecar key 文件缺少 MEMORY_OUTBOX_KEY。"
    }
    $padding = "=" * ((4 - ($encodedKey.Length % 4)) % 4)
    $base64 = $encodedKey.Replace("-", "+").Replace("_", "/") + $padding
    try {
        $key = [Convert]::FromBase64String($base64)
    }
    catch {
        throw "Sidecar key 文件中的 MEMORY_OUTBOX_KEY 格式无效。"
    }
    if ($key.Length -ne 32) {
        throw "Sidecar key 文件中的 MEMORY_OUTBOX_KEY 长度无效。"
    }
    $hmac = [Security.Cryptography.HMACSHA256]::new($key)
    try {
        $message = [Text.Encoding]::UTF8.GetBytes("memory-sidecar-local-rpc-v1")
        return [Convert]::ToHexString($hmac.ComputeHash($message)).ToLowerInvariant()
    }
    finally {
        $hmac.Dispose()
    }
}

function Test-SidecarHealth([string]$KeyFile, [int]$Port) {
    $token = Get-SidecarAuthToken -KeyFile $KeyFile
    try {
        $response = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$Port/health" `
            -Headers @{ Authorization = "Sidecar $token" } `
            -TimeoutSec 2
    }
    catch {
        return $false
    }
    return $response.ok -eq $true -and $response.service -eq "memory-sidecar"
}

function New-McpConfigFile(
    [pscustomobject]$AgentSpec,
    [string]$OutputDirectory,
    [string]$StartScript,
    [string]$Workspace,
    [string]$KeyFile,
    [int]$Port,
    [string]$PythonPath,
    [bool]$AllowExisting
) {
    $outputPath = Join-Path $OutputDirectory "$($AgentSpec.Id)-mcp.json"
    if (Test-Path -LiteralPath $outputPath) {
        if ($AllowExisting) {
            return $outputPath
        }
        throw "MCP 配置已存在，拒绝覆盖：$outputPath"
    }
    $config = [ordered]@{
        mcp_servers = [ordered]@{
            "shared-memory" = [ordered]@{
                command = "pwsh"
                args = @(
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-File", $StartScript,
                    "-AgentInstallationId", $AgentSpec.Id,
                    "-DefaultWorkspace", $Workspace,
                    "-SidecarKeyFile", $KeyFile,
                    "-Port", [string]$Port,
                    "-PythonExecutable", $PythonPath
                )
            }
        }
    }
    $config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $outputPath -Encoding utf8NoBOM
    return $outputPath
}

if ($Mode -eq "demo") {
    & (Join-Path $PSScriptRoot "setup-local-demo.ps1") `
        -DemoHome $DemoHome `
        -Port $DemoPort `
        -PythonExecutable $PythonExecutable
    exit $LASTEXITCODE
}

if ($Mode -eq "server") {
    foreach ($required in @{
            "SshHost" = $SshHost
            "RemoteRoot" = $RemoteRoot
            "SecretsFile" = $SecretsFile
            "GatewayPublicName" = $GatewayPublicName
    }.GetEnumerator()) {
        Require-Value -Name $required.Key -Value $required.Value
    }
    if ($DeploymentProfile -eq "slim") {
        Require-Value -Name "AdminEnvironmentFile" -Value $AdminEnvironmentFile
    }
    $bindAddress = if ($GatewayBindAddress) { $GatewayBindAddress } else { $GatewayPublicName }
    if (-not $Apply) {
        [pscustomobject]@{
            mode = "server"
            status = "waiting_for_apply"
            message = "已核对发布参数。确认维护窗口后，加上 -Apply 执行构建和启动。"
            ssh_host = $SshHost
            ssh_port = $SshPort
            gateway_public_name = $GatewayPublicName
            gateway_bind_address = $bindAddress
            deployment_profile = $DeploymentProfile
        }
        exit 0
    }
    & (Join-Path $PSScriptRoot "deploy-fn-release.ps1") `
        -SshHost $SshHost `
        -SshPort $SshPort `
        -RemoteRoot $RemoteRoot `
        -SecretsFile $SecretsFile `
        -BackendNetwork $BackendNetwork `
        -GatewayPublicName $GatewayPublicName `
        -GatewayBindAddress $bindAddress `
        -HttpsPort $HttpsPort `
        -DeploymentProfile $DeploymentProfile `
        -AdminEnvironmentFile $AdminEnvironmentFile `
        -ProjectRoot $projectRoot `
        -Build `
        -Start
    exit $LASTEXITCODE
}

if ($Mode -eq "container") {
    foreach ($required in @{
            "SshHost" = $SshHost
            "ClientContainerName" = $ClientContainerName
            "ContainerStateDirectory" = $ContainerStateDirectory
            "TenantId" = $TenantId
            "UserId" = $UserId
            "DeviceId" = $DeviceId
            "DefaultWorkspace" = $DefaultWorkspace
        }.GetEnumerator()) {
        Require-Value -Name $required.Key -Value $required.Value
    }
    if ($Agent.Count -ne 1) {
        throw "容器接入一次只登记一个 Agent；请提供一条 -Agent。"
    }
    $containerAgent = ConvertTo-AgentSpec -RawAgent $Agent[0]
    & (Join-Path $PSScriptRoot "setup-container-sidecar.ps1") `
        -SshHost $SshHost `
        -SshPort $SshPort `
        -ClientContainerName $ClientContainerName `
        -StateDirectory $ContainerStateDirectory `
        -TenantId $TenantId `
        -UserId $UserId `
        -DeviceId $DeviceId `
        -DeviceName $DeviceName `
        -DeviceType $DeviceType `
        -AgentInstallationId $containerAgent.Id `
        -AgentType $containerAgent.Type `
        -AgentDisplayName $containerAgent.Name `
        -DefaultWorkspace $DefaultWorkspace `
        -Capabilities $ContainerCapabilities `
        -GatewayInternalUrl $ContainerGatewayUrl `
        -ContainerUser $ContainerUser `
        -Apply:$Apply `
        -Resume:$Resume
    exit $LASTEXITCODE
}

if ($Mode -eq "verify") {
    if (-not (Test-Path -LiteralPath $SidecarKeyFile -PathType Leaf)) {
        throw "找不到本机 Sidecar key 文件：$SidecarKeyFile"
    }
    if (-not (Test-SidecarHealth -KeyFile $SidecarKeyFile -Port $SidecarPort)) {
        throw "本机 Sidecar 没有通过只读健康检查。请先确认计划任务或启动脚本是否正在运行。"
    }
    [pscustomobject]@{
        mode = "verify"
        status = "ready"
        listener = "127.0.0.1:$SidecarPort"
        check = "只读 Sidecar 健康检查"
    }
    exit 0
}

foreach ($required in @{
        "GatewayUrl" = $GatewayUrl
        "DeviceId" = $DeviceId
        "DeviceName" = $DeviceName
        "DefaultWorkspace" = $DefaultWorkspace
        "CredentialTarget" = $CredentialTarget
        "CredentialUsername" = $CredentialUsername
    }.GetEnumerator()) {
    Require-Value -Name $required.Key -Value $required.Value
}
if ($GatewayUrl -notmatch "^https?://[^\s/]+") {
    throw "GatewayUrl 必须是 HTTP 或 HTTPS 地址。"
}
if ($DeviceId -notmatch "^[A-Za-z0-9_.@:-]+$") {
    throw "DeviceId 只能使用字母、数字、点、下划线、@、冒号或连字符。"
}
if ($DefaultWorkspace -notmatch "^[A-Za-z0-9_.@:-]+$") {
    throw "DefaultWorkspace 必须是已经登记的工作区 ID。"
}
if (-not $Agent) {
    throw "至少提供一个 -Agent。"
}

$agentSpecs = @($Agent | ForEach-Object { ConvertTo-AgentSpec -RawAgent $_ })
if (($agentSpecs.Id | Select-Object -Unique).Count -ne $agentSpecs.Count) {
    throw "Agent 安装实例 ID 不能重复。"
}
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($InstallAutostart -and $existingTask -and -not $UseExistingCredential) {
    throw "计划任务已存在，拒绝覆盖：$TaskName"
}

$pythonPath = Get-DevicePython -RequestedExecutable $PythonExecutable -CreateRuntime $BootstrapRuntime
$sourceDirectory = Join-Path $projectRoot "src"
if (-not (Test-Path -LiteralPath (Join-Path $sourceDirectory "agent_memory_gateway") -PathType Container)) {
    throw "找不到项目源码目录：$sourceDirectory"
}
if (-not [string]::IsNullOrWhiteSpace($GatewayCaCertificate)) {
    if (-not (Test-Path -LiteralPath $GatewayCaCertificate -PathType Leaf)) {
        throw "找不到 Gateway CA 证书文件：$GatewayCaCertificate"
    }
    if ($GatewayUrl -notmatch "^https://") {
        throw "GatewayCaCertificate 只能和 HTTPS Gateway 一起使用。"
    }
    $GatewayCaCertificate = (Resolve-Path -LiteralPath $GatewayCaCertificate).ProviderPath
}

$mcpStartScript = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "start-sidecar-mcp.ps1")).ProviderPath
foreach ($agentSpec in $agentSpecs) {
    $outputPath = Join-Path $McpOutputDirectory "$($agentSpec.Id)-mcp.json"
    if ((Test-Path -LiteralPath $outputPath) -and -not $UseExistingCredential) {
        throw "MCP 配置已存在，拒绝覆盖：$outputPath"
    }
}

$hadPythonPath = Test-Path -LiteralPath "Env:PYTHONPATH"
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = if ($previousPythonPath) { "$sourceDirectory;$previousPythonPath" } else { $sourceDirectory }

    if (-not (Test-Path -LiteralPath $SidecarKeyFile -PathType Leaf)) {
        & $pythonPath -m agent_memory_gateway.sidecar_key --output $SidecarKeyFile
        if ($LASTEXITCODE -ne 0) {
            throw "无法生成本机 Sidecar key 文件。"
        }
    }

    if ($UseExistingCredential) {
        if (-not (Test-Path -LiteralPath $DeviceKeyFile -PathType Leaf)) {
            throw "指定 -UseExistingCredential 时必须保留原设备私钥：$DeviceKeyFile"
        }
        $pairResult = [pscustomobject]@{
            device_id = $DeviceId
            agent_installation_ids = @($agentSpecs.Id)
            credential_target = $CredentialTarget
        }
    }
    else {
        $pairingSecret = Read-Host "请输入管理员生成的一次性配对码（不会显示或写入配置）" -AsSecureString
        $pairingBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pairingSecret)
        $pairingCode = $null
        try {
            $pairingCode = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pairingBstr)
            $pairingArguments = @(
                "-m", "agent_memory_gateway.device_pair",
                "--gateway-url", $GatewayUrl,
                "--pairing-code-stdin",
                "--device-id", $DeviceId,
                "--device-name", $DeviceName,
                "--device-type", $DeviceType,
                "--device-key-file", $DeviceKeyFile,
                "--credential-target", $CredentialTarget,
                "--credential-username", $CredentialUsername
            )
            foreach ($agentSpec in $agentSpecs) {
                $pairingArguments += @("--agent", "$($agentSpec.Id)|$($agentSpec.Type)|$($agentSpec.Name)")
            }
            if (-not [string]::IsNullOrWhiteSpace($GatewayCaCertificate)) {
                $pairingArguments += @("--gateway-ca-certificate", $GatewayCaCertificate)
            }
            $pairResultLines = $pairingCode | & $pythonPath @pairingArguments
            if ($LASTEXITCODE -ne 0) {
                throw "设备配对失败。上面的错误码可用于判断是配对码、网络、证书还是登记信息需要修正。"
            }
            $pairResult = $pairResultLines | ConvertFrom-Json
        }
        finally {
            if ($pairingBstr -ne [IntPtr]::Zero) {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pairingBstr)
            }
            Remove-Variable -Name pairingCode -ErrorAction SilentlyContinue
        }
    }
}
finally {
    if ($hadPythonPath) {
        $env:PYTHONPATH = $previousPythonPath
    }
    else {
        Remove-Item -LiteralPath "Env:PYTHONPATH" -ErrorAction SilentlyContinue
    }
}

New-Item -ItemType Directory -Path $McpOutputDirectory -Force | Out-Null
$mcpFiles = @()
foreach ($agentSpec in $agentSpecs) {
    $mcpFiles += New-McpConfigFile `
        -AgentSpec $agentSpec `
        -OutputDirectory $McpOutputDirectory `
        -StartScript $mcpStartScript `
        -Workspace $DefaultWorkspace `
        -KeyFile $SidecarKeyFile `
        -Port $SidecarPort `
        -PythonPath $pythonPath `
        -AllowExisting $UseExistingCredential
}

$autostartStatus = "not_installed"
if ($InstallAutostart) {
    $allowedAgents = $agentSpecs.Id -join ","
    $installArguments = @{
        TaskName = $TaskName
        GatewayUrl = $GatewayUrl
        AllowedAgents = $allowedAgents
        DeviceId = $DeviceId
        DefaultWorkspace = $DefaultWorkspace
        CredentialTarget = $CredentialTarget
        SidecarKeyFile = $SidecarKeyFile
        Port = $SidecarPort
        PythonExecutable = $pythonPath
    }
    if (-not [string]::IsNullOrWhiteSpace($GatewayCaCertificate)) {
        $installArguments.GatewayCaCertificate = $GatewayCaCertificate
    }
    if (-not $existingTask) {
        & (Join-Path $PSScriptRoot "install-sidecar-autostart.ps1") @installArguments
    }
    Start-ScheduledTask -TaskName $TaskName
    $healthy = $false
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-SidecarHealth -KeyFile $SidecarKeyFile -Port $SidecarPort) {
            $healthy = $true
            break
        }
    }
    if (-not $healthy) {
        throw "已创建计划任务，但 Sidecar 未在 5 秒内通过健康检查。请检查计划任务历史和启动脚本输出。"
    }
    $autostartStatus = if ($existingTask) { "existing_and_running" } else { "installed_and_running" }
}

[pscustomobject]@{
    mode = "device"
    status = "ready"
    device_id = $pairResult.device_id
    agent_installation_ids = @($pairResult.agent_installation_ids)
    credential_target = $pairResult.credential_target
    reused_existing_credential = [bool]$UseExistingCredential
    sidecar_autostart = $autostartStatus
    runtime_python = $pythonPath
    mcp_config_files = $mcpFiles
    next_step = "把对应 MCP 配置文件导入 Codex、Hermes 或其他 MCP 客户端后，调用 memory_sync_status。"
}
