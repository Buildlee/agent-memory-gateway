[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SshHost,

    [ValidateRange(1, 65535)]
    [int]$SshPort = 22,

    [Parameter(Mandatory)]
    [string]$RemoteRoot,

    [Parameter(Mandatory)]
    [string]$StateDirectory,

    [Parameter(Mandatory)]
    [string]$TenantId,

    [Parameter(Mandatory)]
    [string]$UserId,

    [Parameter(Mandatory)]
    [string]$DeviceId,

    [string]$DeviceName = $DeviceId,

    [Parameter(Mandatory)]
    [string]$AgentInstallationId,

    [string]$AgentDisplayName = "Central Memory Admin",

    [Parameter(Mandatory)]
    [string]$DefaultWorkspace,

    [Parameter(Mandatory)]
    [string]$PublicBaseUrl,

    [string]$BackendNetwork = "memory-backend",

    [string]$Capabilities = "memory.feedback,memory.forget,memory.manage,memory.read_context,memory.search,memory.sync,memory.write_event",

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
            throw "中枢管理页配置失败：$errorOutput"
        }
        throw "中枢管理页配置失败，退出码：$($process.ExitCode)"
    }
}

Require-RemotePath -Name "RemoteRoot" -Value $RemoteRoot
Require-RemotePath -Name "StateDirectory" -Value $StateDirectory
if (-not $StateDirectory.StartsWith("$($RemoteRoot.TrimEnd('/'))/", [StringComparison]::Ordinal)) {
    throw "StateDirectory 必须位于 RemoteRoot 内，避免误写到其他 NAS 目录。"
}
foreach ($entry in @{
        "TenantId" = $TenantId
        "UserId" = $UserId
        "DeviceId" = $DeviceId
        "AgentInstallationId" = $AgentInstallationId
        "DefaultWorkspace" = $DefaultWorkspace
        "BackendNetwork" = $BackendNetwork
    }.GetEnumerator()) {
    Require-Identifier -Name $entry.Key -Value $entry.Value
}
if ([string]::IsNullOrWhiteSpace($DeviceName) -or $DeviceName.Length -gt 256) {
    throw "DeviceName 无效。"
}
if ([string]::IsNullOrWhiteSpace($AgentDisplayName) -or $AgentDisplayName.Length -gt 256) {
    throw "AgentDisplayName 无效。"
}
if ($PublicBaseUrl -notmatch "^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/admin$") {
    throw "PublicBaseUrl 必须是 HTTPS 管理入口，且固定以 /admin 结尾。"
}
$capabilityValues = @($Capabilities.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $capabilityValues -or $capabilityValues | Where-Object { $_ -notmatch "^[A-Za-z0-9_.]+$" }) {
    throw "Capabilities 必须是逗号分隔的能力名。"
}
if ($capabilityValues -notcontains "memory.manage") {
    throw "中枢管理页必须包含 memory.manage。"
}
$Capabilities = ($capabilityValues | Sort-Object -Unique) -join ","

$quoted = @{
    RemoteRoot = ConvertTo-PosixLiteral $RemoteRoot
    StateDirectory = ConvertTo-PosixLiteral $StateDirectory
    TenantId = ConvertTo-PosixLiteral $TenantId
    UserId = ConvertTo-PosixLiteral $UserId
    DeviceId = ConvertTo-PosixLiteral $DeviceId
    DeviceName = ConvertTo-PosixLiteral $DeviceName
    AgentInstallationId = ConvertTo-PosixLiteral $AgentInstallationId
    AgentDisplayName = ConvertTo-PosixLiteral $AgentDisplayName
    DefaultWorkspace = ConvertTo-PosixLiteral $DefaultWorkspace
    PublicBaseUrl = ConvertTo-PosixLiteral $PublicBaseUrl
    BackendNetwork = ConvertTo-PosixLiteral $BackendNetwork
    Capabilities = ConvertTo-PosixLiteral $Capabilities
    ContainerUser = ConvertTo-PosixLiteral $ContainerUser
    Apply = if ($Apply) { "1" } else { "0" }
    Resume = if ($Resume) { "1" } else { "0" }
}

$remoteScript = @'
set -eu

remote_root=__RemoteRoot__
state_dir=__StateDirectory__
tenant_id=__TenantId__
user_id=__UserId__
device_id=__DeviceId__
device_name=__DeviceName__
agent_id=__AgentInstallationId__
agent_name=__AgentDisplayName__
workspace_id=__DefaultWorkspace__
public_base_url=__PublicBaseUrl__
backend_network=__BackendNetwork__
capabilities=__Capabilities__
container_user=__ContainerUser__
apply=__Apply__
resume=__Resume__

docker network inspect "$backend_network" >/dev/null
test -d "$remote_root"

set -- $(docker ps -q --filter 'label=com.docker.compose.project=memory-gateway' --filter 'label=com.docker.compose.service=gateway')
test "$#" -eq 1
gateway_container="$1"
gateway_compose="$(docker inspect "$gateway_container" --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
test -f "$gateway_compose"
gateway_release="$(dirname "$(dirname "$(dirname "$gateway_compose")")")"
base_env="$gateway_release/.env"
admin_compose="$gateway_release/deploy/fn/admin-console.compose.yaml"
image="$(docker inspect "$gateway_container" --format '{{.Config.Image}}')"
gateway_entrypoint="$(docker inspect "$gateway_container" --format '{{index .Config.Entrypoint 0}}')"
test -f "$base_env"
test -f "$admin_compose"
test -n "$image"
test -n "$gateway_entrypoint" && test "$gateway_entrypoint" != '<no value>'
docker exec "$gateway_container" "$gateway_entrypoint" memory-gateway --help >/dev/null

gateway_ip="$(docker inspect "$gateway_container" --format '{{range $name, $network := .NetworkSettings.Networks}}{{if eq $name "__BACKEND_NETWORK__"}}{{$network.IPAddress}}{{end}}{{end}}')"
test -n "$gateway_ip"
gateway_url="http://$gateway_ip:8787"

sidecar_state="$state_dir/sidecar"
console_state="$state_dir/console"
sidecar_env="$sidecar_state/sidecar.env"
device_key="$sidecar_state/device-identity.pem"
refresh_file="$sidecar_state/refresh-credential.json"
admin_env="$state_dir/admin.env"
uid="${container_user%%:*}"
gid="${container_user##*:}"
admin_sidecar_id="$(docker compose --project-name memory-gateway --env-file "$base_env" --env-file "$admin_env" -f "$gateway_compose" -f "$admin_compose" ps -aq admin-sidecar 2>/dev/null || true)"
admin_console_id="$(docker compose --project-name memory-gateway --env-file "$base_env" --env-file "$admin_env" -f "$gateway_compose" -f "$admin_compose" ps -aq admin-console 2>/dev/null || true)"

printf '%s\n' \
  "gateway_container=$gateway_container" \
  "gateway_release=$gateway_release" \
  "gateway_internal_url=$gateway_url" \
  "state_directory=$state_dir" \
  "admin_sidecar=${admin_sidecar_id:-absent}" \
  "admin_console=${admin_console_id:-absent}" \
  "public_base_url=$public_base_url"

if [ "$apply" != 1 ]; then
  printf '%s\n' 'status=waiting_for_apply'
  exit 0
fi

if { [ -e "$device_key" ] || [ -e "$refresh_file" ] || [ -e "$sidecar_env" ]; } && [ "$resume" != 1 ]; then
  echo '中枢管理 Sidecar 状态已存在；请先核对后使用 -Resume。' >&2
  exit 65
fi
if { [ -n "$admin_sidecar_id" ] || [ -n "$admin_console_id" ]; } && [ "$resume" != 1 ]; then
  echo '中枢管理容器已存在；拒绝替换。请先核对后使用 -Resume。' >&2
  exit 65
fi

install -d -m 0700 "$state_dir" "$sidecar_state" "$console_state"
test "$(stat -c %u:%g "$sidecar_state")" = "$uid:$gid"
test "$(stat -c %u:%g "$console_state")" = "$uid:$gid"

if [ ! -f "$device_key" ] || [ ! -f "$refresh_file" ]; then
  pairing_code="$(docker exec "$gateway_container" "$gateway_entrypoint" memory-gateway pairing-code --tenant-id "$tenant_id" --user-id "$user_id" --device-type nas --agent-types other | docker exec -i "$gateway_container" python -c 'import json, sys; print(json.load(sys.stdin)["pairing_code"])')"
  test -n "$pairing_code"
  printf '%s\n' "$pairing_code" | docker run --rm --network "$backend_network" --user "$container_user" --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m --security-opt no-new-privileges:true --cap-drop ALL --pids-limit 64 -v "$sidecar_state:/state" --entrypoint python "$image" -m agent_memory_gateway.device_pair --gateway-url "$gateway_url" --pairing-code-stdin --device-id "$device_id" --device-name "$device_name" --device-type nas --device-key-file /state/device-identity.pem --credential-file /state/refresh-credential.json --credential-username "$user_id" --agent "$agent_id|other|$agent_name"
fi
if [ ! -f "$sidecar_env" ]; then
  docker run --rm --network none --user "$container_user" --read-only --tmpfs /tmp:rw,noexec,nosuid,size=32m --security-opt no-new-privileges:true --cap-drop ALL --pids-limit 64 -v "$sidecar_state:/state" --entrypoint python "$image" -m agent_memory_gateway.sidecar_key --output /state/sidecar.env
fi
test "$(stat -c %a "$device_key")" = 600
test "$(stat -c %a "$refresh_file")" = 600
test "$(stat -c %a "$sidecar_env")" = 600

docker exec "$gateway_container" "$gateway_entrypoint" memory-gateway bind-workspace --agent-installation-id "$agent_id" --workspace-id "$workspace_id" --capabilities "$capabilities"

if [ -e "$admin_env" ] && [ "$resume" != 1 ]; then
  echo '中枢管理环境文件已存在；拒绝覆盖。' >&2
  exit 65
fi
umask 077
{
  printf '%s\n' "MEMORY_ADMIN_SIDECAR_STATE_DIR=$sidecar_state"
  printf '%s\n' "MEMORY_ADMIN_CONSOLE_STATE_DIR=$console_state"
  printf '%s\n' "MEMORY_ADMIN_SIDECAR_KEY_FILE=$sidecar_env"
  printf '%s\n' "MEMORY_GATEWAY_URL=$gateway_url"
  printf '%s\n' "MEMORY_ADMIN_AGENT_INSTALLATION_ID=$agent_id"
  printf '%s\n' "MEMORY_ADMIN_DEVICE_ID=$device_id"
  printf '%s\n' "MEMORY_DEFAULT_WORKSPACE=$workspace_id"
  printf '%s\n' "MEMORY_ADMIN_PUBLIC_BASE_URL=$public_base_url"
  printf '%s\n' "MEMORY_ADMIN_SIDECAR_UID=$uid"
  printf '%s\n' "MEMORY_ADMIN_SIDECAR_GID=$gid"
} > "$admin_env"
chmod 0600 "$admin_env"
test "$(stat -c %a "$admin_env")" = 600

docker compose --project-name memory-gateway --env-file "$base_env" --env-file "$admin_env" -f "$gateway_compose" -f "$admin_compose" config -q
if [ "$resume" = 1 ]; then
  docker compose --project-name memory-gateway --env-file "$base_env" --env-file "$admin_env" -f "$gateway_compose" -f "$admin_compose" up -d --no-build --no-deps --force-recreate admin-sidecar admin-console
else
  docker compose --project-name memory-gateway --env-file "$base_env" --env-file "$admin_env" -f "$gateway_compose" -f "$admin_compose" up -d --no-build --no-deps admin-sidecar admin-console
fi
printf '%s\n' 'status=central_admin_ready' "launch_file=$console_state/launch-url"
'@

$remoteScript = $remoteScript.Replace("__BACKEND_NETWORK__", $BackendNetwork)
foreach ($entry in $quoted.GetEnumerator()) {
    $remoteScript = $remoteScript.Replace("__" + $entry.Key + "__", $entry.Value)
}

Invoke-RemoteScript -Script $remoteScript
