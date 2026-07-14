[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SshHost,

    [Parameter(Mandatory)]
    [string]$RemoteRoot,

    [Parameter(Mandatory)]
    [string]$SecretsFile,

    [string]$BackendNetwork = "memory-backend",

    [Parameter(Mandatory)]
    [string]$GatewayAddress,

    [int]$HttpsPort = 8443,

    [int]$WorkerHeartbeatMaxSeconds = 30,

    [switch]$Build,

    [switch]$Start
)

$ErrorActionPreference = "Stop"

if ($SshHost -notmatch "^[A-Za-z0-9_.@:-]+$") {
    throw "SSH 主机参数包含不允许的字符"
}
if ($RemoteRoot -notmatch "^/[A-Za-z0-9._/-]+$") {
    throw "远端发布目录必须是 Linux 绝对路径，且不能包含空格或引号"
}
if ($SecretsFile -notmatch "^/[A-Za-z0-9._/-]+$") {
    throw "secret 文件必须是 Linux 绝对路径，且不能包含空格或引号"
}
if ($BackendNetwork -notmatch "^[A-Za-z0-9_.-]+$") {
    throw "Docker 后端网络名包含不允许的字符"
}
if ($GatewayAddress -notmatch "^[A-Za-z0-9.-]+$") {
    throw "GatewayAddress 必须是内部 DNS 名称或 IP 地址"
}
if ($HttpsPort -lt 1024 -or $HttpsPort -gt 65535) {
    throw "HttpsPort 必须在 1024 到 65535 之间"
}
if ($WorkerHeartbeatMaxSeconds -lt 1 -or $WorkerHeartbeatMaxSeconds -gt 3600) {
    throw "WorkerHeartbeatMaxSeconds 必须在 1 到 3600 之间"
}

if ($Start) {
    $Build = $true
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseId = "release-" + (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
$remoteRelease = "$RemoteRoot/releases/$releaseId"

$prepareCommand = "set -eu; docker network inspect '$BackendNetwork' >/dev/null; test -r '$SecretsFile'; mkdir -p '$RemoteRoot/releases'; test ! -e '$remoteRelease'; mkdir -m 0750 '$remoteRelease'"
ssh $SshHost $prepareCommand
if ($LASTEXITCODE -ne 0) {
    throw "远端发布前置检查失败，退出码：$LASTEXITCODE"
}

Push-Location -LiteralPath $projectRoot
try {
    & scp -r "pyproject.toml" "README.md" "src" "schema" "${SshHost}:$remoteRelease/"
    if ($LASTEXITCODE -ne 0) {
        throw "上传 Gateway 源码失败，退出码：$LASTEXITCODE"
    }
    & scp -r "deploy" "${SshHost}:$remoteRelease/"
    if ($LASTEXITCODE -ne 0) {
        throw "上传 Gateway 编排文件失败，退出码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$publicConfigCommand = "chmod 0644 '$remoteRelease/deploy/fn/Caddyfile'"
ssh $SshHost $publicConfigCommand
if ($LASTEXITCODE -ne 0) {
    throw "设置 Caddy 公开配置文件权限失败，退出码：$LASTEXITCODE"
}

$environmentCommand = "umask 077; printf '%s\n' 'MEMORY_GATEWAY_SECRETS_FILE=$SecretsFile' 'MEMORY_GATEWAY_BACKEND_NETWORK=$BackendNetwork' 'MEMORY_GATEWAY_PUBLIC_NAME=$GatewayAddress' 'MEMORY_GATEWAY_BIND_ADDRESS=$GatewayAddress' 'MEMORY_GATEWAY_HTTPS_PORT=$HttpsPort' 'MEMORY_WORKER_HEARTBEAT_MAX_SECONDS=$WorkerHeartbeatMaxSeconds' > '$remoteRelease/.env'"
ssh $SshHost $environmentCommand
if ($LASTEXITCODE -ne 0) {
    throw "写入不含密钥的发布环境文件失败，退出码：$LASTEXITCODE"
}

$validateCommand = "cd '$remoteRelease' && docker compose --project-name memory-gateway --env-file .env -f deploy/fn/compose.yaml config -q"
ssh $SshHost $validateCommand
if ($LASTEXITCODE -ne 0) {
    throw "远端 Compose 校验失败，退出码：$LASTEXITCODE"
}

if ($Build) {
    $buildCommand = "cd '$remoteRelease' && docker compose --project-name memory-gateway --env-file .env -f deploy/fn/compose.yaml build --pull"
    ssh $SshHost $buildCommand
    if ($LASTEXITCODE -ne 0) {
        throw "远端 Gateway 镜像构建失败，退出码：$LASTEXITCODE"
    }
}

if ($Start) {
    $startCommand = "cd '$remoteRelease' && docker compose --project-name memory-gateway --env-file .env -f deploy/fn/compose.yaml up -d --no-build"
    ssh $SshHost $startCommand
    if ($LASTEXITCODE -ne 0) {
        throw "远端 Gateway 服务启动失败，退出码：$LASTEXITCODE"
    }
}

$imageId = ssh $SshHost "docker image inspect agent-memory-gateway:0.1.0 --format '{{.Id}}' 2>/dev/null || true"
if ($LASTEXITCODE -ne 0) {
    throw "读取 Gateway 镜像 ID 失败，退出码：$LASTEXITCODE"
}

[pscustomobject]@{
    release = $remoteRelease
    image_id = $imageId.Trim()
    built = [bool]$Build
    started = [bool]$Start
    endpoint = "https://$GatewayAddress`:$HttpsPort"
}
