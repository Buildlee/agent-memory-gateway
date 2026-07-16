[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SshHost,

    [ValidateRange(1, 65535)]
    [int]$SshPort = 22,

    [Parameter(Mandatory)]
    [string]$ClientContainerName,

    [Parameter(Mandatory)]
    [string]$StateDirectory,

    [Parameter(Mandatory)]
    [string]$TenantId,

    [Parameter(Mandatory)]
    [string]$UserId,

    [Parameter(Mandatory)]
    [string]$DeviceId,

    [string]$DeviceName = $DeviceId,

    [ValidateSet("nas", "other")]
    [string]$DeviceType = "nas",

    [Parameter(Mandatory)]
    [string]$AgentInstallationId,

    [ValidateSet("codex", "hermes", "other")]
    [string]$AgentType,

    [string]$AgentDisplayName = $AgentInstallationId,

    [Parameter(Mandatory)]
    [string]$DefaultWorkspace,

    [string]$Capabilities = "memory.feedback,memory.forget,memory.read_context,memory.search,memory.sync,memory.write_event",

    [string]$GatewayInternalUrl = "http://gateway:8787",

    [ValidatePattern("^[0-9]+:[0-9]+$")]
    [string]$ContainerUser = "1000:1001",

    [switch]$Apply,

    [switch]$Resume
)

$ErrorActionPreference = "Stop"

function Require-RemotePath([string]$Name, [string]$Value) {
    if ($Value -notmatch "^/[A-Za-z0-9._/-]+$") {
        throw "$Name 必须是没有空格的 Linux 绝对路径。"
    }
}

function Require-Identifier([string]$Name, [string]$Value) {
    if ($Value -notmatch "^[A-Za-z0-9_.@:-]+$") {
        throw "$Name 只能使用字母、数字、点、下划线、@、冒号或连字符。"
    }
}

function ConvertTo-PosixLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "'`"'`"'") + "'"
}

function Invoke-RemoteScript([string]$Script) {
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = "ssh"
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in @("-p", [string]$SshPort, $SshHost, "sh", "-s")) {
        [void]$startInfo.ArgumentList.Add($argument)
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $process.StandardInput.NewLine = "`n"
    $process.StandardInput.Write(($Script -replace "`r`n", "`n"))
    $process.StandardInput.Close()
    $standardOutput = $process.StandardOutput.ReadToEndAsync()
    $standardError = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $output = $standardOutput.GetAwaiter().GetResult().TrimEnd()
    $errorOutput = $standardError.GetAwaiter().GetResult().Trim()
    if ($output) {
        Write-Output $output
    }
    if ($process.ExitCode -ne 0) {
        if ($errorOutput) {
            throw "容器接入命令失败：$errorOutput"
        }
        throw "容器接入命令失败，退出码：$($process.ExitCode)"
    }
}

Require-RemotePath -Name "StateDirectory" -Value $StateDirectory
foreach ($entry in @{
        "ClientContainerName" = $ClientContainerName
        "TenantId" = $TenantId
        "UserId" = $UserId
        "DeviceId" = $DeviceId
        "AgentInstallationId" = $AgentInstallationId
        "DefaultWorkspace" = $DefaultWorkspace
    }.GetEnumerator()) {
    Require-Identifier -Name $entry.Key -Value $entry.Value
}
if ([string]::IsNullOrWhiteSpace($DeviceName) -or $DeviceName.Length -gt 256) {
    throw "DeviceName 无效。"
}
if ([string]::IsNullOrWhiteSpace($AgentDisplayName) -or $AgentDisplayName.Length -gt 256) {
    throw "AgentDisplayName 无效。"
}
if ($GatewayInternalUrl -notmatch "^https?://[A-Za-z0-9._:-]+$") {
    throw "GatewayInternalUrl 必须是容器网络中可访问的 HTTP 或 HTTPS 地址。"
}
$capabilityValues = @($Capabilities.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $capabilityValues -or $capabilityValues | Where-Object { $_ -notmatch "^[A-Za-z0-9_.]+$" }) {
    throw "Capabilities 必须是逗号分隔的能力名。"
}
$Capabilities = ($capabilityValues | Sort-Object -Unique) -join ","

$quoted = @{
    ClientContainerName = ConvertTo-PosixLiteral $ClientContainerName
    StateDirectory = ConvertTo-PosixLiteral $StateDirectory
    TenantId = ConvertTo-PosixLiteral $TenantId
    UserId = ConvertTo-PosixLiteral $UserId
    DeviceId = ConvertTo-PosixLiteral $DeviceId
    DeviceName = ConvertTo-PosixLiteral $DeviceName
    DeviceType = ConvertTo-PosixLiteral $DeviceType
    AgentInstallationId = ConvertTo-PosixLiteral $AgentInstallationId
    AgentType = ConvertTo-PosixLiteral $AgentType
    AgentDisplayName = ConvertTo-PosixLiteral $AgentDisplayName
    DefaultWorkspace = ConvertTo-PosixLiteral $DefaultWorkspace
    Capabilities = ConvertTo-PosixLiteral $Capabilities
    GatewayInternalUrl = ConvertTo-PosixLiteral $GatewayInternalUrl
    ContainerUser = ConvertTo-PosixLiteral $ContainerUser
    Apply = if ($Apply) { "1" } else { "0" }
    Resume = if ($Resume) { "1" } else { "0" }
}

$remoteScript = @'
set -e
set -u

client_container=__ClientContainerName__
state_dir=__StateDirectory__
tenant_id=__TenantId__
user_id=__UserId__
device_id=__DeviceId__
device_name=__DeviceName__
device_type=__DeviceType__
agent_id=__AgentInstallationId__
agent_type=__AgentType__
agent_name=__AgentDisplayName__
workspace_id=__DefaultWorkspace__
capabilities=__Capabilities__
gateway_url=__GatewayInternalUrl__
container_user=__ContainerUser__
apply=__Apply__
resume=__Resume__

client_project="$(docker inspect "$client_container" --format '{{ index .Config.Labels "com.docker.compose.project" }}')"
client_service="$(docker inspect "$client_container" --format '{{ index .Config.Labels "com.docker.compose.service" }}')"
client_compose="$(docker inspect "$client_container" --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
test -n "$client_project" && test "$client_project" != '<no value>'
test -n "$client_service" && test "$client_service" != '<no value>'
test -n "$client_compose" && test "$client_compose" != '<no value>'
test -f "$client_compose"

set -- $(docker ps -q --filter 'label=com.docker.compose.project=memory-gateway' --filter 'label=com.docker.compose.service=gateway')
test "$#" -eq 1
gateway_container="$1"
gateway_compose="$(docker inspect "$gateway_container" --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
test -f "$gateway_compose"
gateway_release="$(dirname "$(dirname "$(dirname "$gateway_compose")")")"
bridge_compose="$gateway_release/deploy/fn/memory-mcp-bridge.compose.yaml"
test -f "$bridge_compose"
test -f "$gateway_release/.env"
image="$(docker inspect "$gateway_container" --format '{{.Config.Image}}')"
test -n "$image"
gateway_entrypoint="$(docker inspect "$gateway_container" --format '{{index .Config.Entrypoint 0}}')"
test -n "$gateway_entrypoint" && test "$gateway_entrypoint" != '<no value>'
docker exec "$gateway_container" "$gateway_entrypoint" memory-gateway --help >/dev/null

sidecar_env="$state_dir/sidecar.env"
device_key="$state_dir/device-identity.pem"
refresh_file="$state_dir/refresh-credential.json"
bridge_env="$state_dir/bridge.env"

export MEMORY_CLIENT_SERVICE="$client_service"
export MEMORY_SIDECAR_STATE_DIR="$state_dir"
export MEMORY_GATEWAY_URL="$gateway_url"
export MEMORY_AGENT_INSTALLATION_ID="$agent_id"
export MEMORY_DEFAULT_WORKSPACE="$workspace_id"
export MEMORY_DEVICE_ID="$device_id"
export MEMORY_SIDECAR_UID="${container_user%%:*}"
export MEMORY_SIDECAR_GID="${container_user##*:}"
docker compose --project-name "$client_project" -f "$client_compose" -f "$bridge_compose" config -q

printf '%s\n' "client_container=$client_container" "client_service=$client_service" "gateway_container=$gateway_container" "state_directory=$state_dir" "mcp_endpoint=http://127.0.0.1:8767/mcp"
if [ "$apply" != 1 ]; then
  printf '%s\n' 'status=waiting_for_apply'
  exit 0
fi

uid="${container_user%%:*}"
gid="${container_user##*:}"
bootstrap_suffix="$(printf '%s' "$device_id" | sha256sum | cut -c1-12)"
pair_container="memory-sidecar-pair-$bootstrap_suffix"
key_container="memory-sidecar-key-$bootstrap_suffix"
if [ -f "$device_key" ] || [ -f "$refresh_file" ]; then
  if [ "$resume" != 1 ] || [ ! -f "$device_key" ] || [ ! -f "$refresh_file" ]; then
    echo '已有或不完整的设备凭据；拒绝覆盖。请核对后使用 -Resume。' >&2
    exit 65
  fi
else
  if [ -e "$state_dir" ] && [ ! -d "$state_dir" ]; then
    echo 'Sidecar 状态路径不是目录。' >&2
    exit 65
  fi
  install -d -m 0700 "$state_dir"
  test "$(stat -c %u:%g "$state_dir")" = "$uid:$gid"

  pairing_code="$(docker exec "$gateway_container" "$gateway_entrypoint" memory-gateway pairing-code --tenant-id "$tenant_id" --user-id "$user_id" --device-type "$device_type" --agent-types "$agent_type" | docker exec -i "$gateway_container" python -c 'import json, sys; print(json.load(sys.stdin)["pairing_code"])')"
  test -n "$pairing_code"
  printf '%s\n' "$pairing_code" | docker run --name "$pair_container" -i --network "container:$client_container" --user "$container_user" --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m --security-opt no-new-privileges:true --cap-drop ALL --pids-limit 64 -v "$state_dir:/state" --entrypoint python "$image" -m agent_memory_gateway.device_pair --gateway-url "$gateway_url" --pairing-code-stdin --device-id "$device_id" --device-name "$device_name" --device-type "$device_type" --device-key-file /state/device-identity.pem --credential-file /state/refresh-credential.json --credential-username "$user_id" --agent "$agent_id|$agent_type|$agent_name"
  pairing_code=''
fi

if [ ! -f "$sidecar_env" ]; then
  docker run --name "$key_container" --user "$container_user" --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m --security-opt no-new-privileges:true --cap-drop ALL --pids-limit 64 -v "$state_dir:/state" --entrypoint python "$image" -m agent_memory_gateway.sidecar_key --output /state/sidecar.env
fi
test "$(stat -c %a "$device_key")" = 600
test "$(stat -c %a "$refresh_file")" = 600
test "$(stat -c %a "$sidecar_env")" = 600

docker exec "$gateway_container" "$gateway_entrypoint" memory-gateway bind-workspace --agent-installation-id "$agent_id" --workspace-id "$workspace_id" --capabilities "$capabilities"

if [ -e "$bridge_env" ] && [ "$resume" != 1 ]; then
  echo 'Bridge 配置已存在；拒绝覆盖。请核对后使用 -Resume。' >&2
  exit 65
fi
if [ ! -e "$bridge_env" ]; then
  umask 077
  {
    printf '%s\n' "MEMORY_CLIENT_SERVICE=$client_service"
    printf '%s\n' "MEMORY_SIDECAR_STATE_DIR=$state_dir"
    printf '%s\n' "MEMORY_GATEWAY_URL=$gateway_url"
    printf '%s\n' "MEMORY_AGENT_INSTALLATION_ID=$agent_id"
    printf '%s\n' "MEMORY_DEFAULT_WORKSPACE=$workspace_id"
    printf '%s\n' "MEMORY_DEVICE_ID=$device_id"
    printf '%s\n' "MEMORY_SIDECAR_UID=$uid"
    printf '%s\n' "MEMORY_SIDECAR_GID=$gid"
  } > "$bridge_env"
  chmod 0600 "$bridge_env"
fi
test "$(stat -c %a "$bridge_env")" = 600

bridge_id="$(docker compose --project-name "$client_project" --env-file "$bridge_env" -f "$client_compose" -f "$bridge_compose" ps -aq memory-mcp-bridge)"
if [ -n "$bridge_id" ]; then
  if [ "$resume" != 1 ]; then
    echo '已存在统一 MCP Bridge 容器；拒绝替换。' >&2
    exit 65
  fi
  docker compose --project-name "$client_project" --env-file "$bridge_env" -f "$client_compose" -f "$bridge_compose" up -d --no-deps --force-recreate memory-mcp-bridge
  bridge_id="$(docker compose --project-name "$client_project" --env-file "$bridge_env" -f "$client_compose" -f "$bridge_compose" ps -q memory-mcp-bridge)"
else
  docker compose --project-name "$client_project" --env-file "$bridge_env" -f "$client_compose" -f "$bridge_compose" up -d --no-deps memory-mcp-bridge
  bridge_id="$(docker compose --project-name "$client_project" --env-file "$bridge_env" -f "$client_compose" -f "$bridge_compose" ps -q memory-mcp-bridge)"
fi
test -n "$bridge_id"

ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  status="$(docker inspect "$bridge_id" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')"
  if [ "$status" = healthy ]; then
    ready=1
    break
  fi
  sleep 2
done
test "$ready" = 1

docker exec -e MEMORY_VERIFY_WORKSPACE="$workspace_id" -e MEMORY_VERIFY_AGENT="$agent_id" "$bridge_id" python -c 'import os; from pathlib import Path; from agent_memory_gateway.sidecar_daemon import LocalSidecarProxy, daemon_auth_token; values = dict(line.split("=", 1) for line in Path("/state/sidecar.env").read_text(encoding="utf-8").splitlines() if "=" in line); proxy = LocalSidecarProxy("http://127.0.0.1:8766", daemon_auth_token(values["MEMORY_OUTBOX_KEY"]), os.environ["MEMORY_VERIFY_AGENT"]); assert proxy.health(); assert isinstance(proxy.sync(os.environ["MEMORY_VERIFY_WORKSPACE"]), dict); print("sidecar_gateway_sync=ready")'
docker exec "$bridge_id" python -c 'import json; from urllib.request import Request, urlopen; body = json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"bridge-check","version":"1"}}}).encode(); request = Request("http://127.0.0.1:8767/mcp", data=body, headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}, method="POST"); response = urlopen(request, timeout=10); assert response.status == 200; print("mcp_endpoint=ready")'
printf '%s\n' 'status=ready'
'@

foreach ($name in $quoted.Keys) {
    $remoteScript = $remoteScript.Replace(("__" + $name + "__"), $quoted[$name])
}
Invoke-RemoteScript -Script $remoteScript
