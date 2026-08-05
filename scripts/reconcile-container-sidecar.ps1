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

    [switch]$Apply
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
            throw "Bridge 对账命令失败：$errorOutput"
        }
        throw "Bridge 对账命令失败，退出码：$($process.ExitCode)"
    }
    if ($errorOutput) {
        Write-Verbose $errorOutput
    }
}

Require-Identifier -Name "ClientContainerName" -Value $ClientContainerName
Require-RemotePath -Name "StateDirectory" -Value $StateDirectory

$quoted = @{
    ClientContainerName = ConvertTo-PosixLiteral $ClientContainerName
    StateDirectory = ConvertTo-PosixLiteral $StateDirectory
    Apply = if ($Apply) { "1" } else { "0" }
}

$remoteScript = @'
set -eu

client_container=__ClientContainerName__
state_dir=__StateDirectory__
apply=__Apply__

client_id="$(docker inspect "$client_container" --format '{{.Id}}')"
client_project="$(docker inspect "$client_container" --format '{{ index .Config.Labels "com.docker.compose.project" }}')"
client_service="$(docker inspect "$client_container" --format '{{ index .Config.Labels "com.docker.compose.service" }}')"
client_compose="$(docker inspect "$client_container" --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
client_status="$(docker inspect "$client_container" --format '{{.State.Status}}')"
test -n "$client_id"
test -n "$client_project" && test "$client_project" != '<no value>'
test -n "$client_service" && test "$client_service" != '<no value>'
test -n "$client_compose" && test "$client_compose" != '<no value>'
case "$client_compose" in *,*) echo '目标容器使用多个 Compose 文件；请使用完整安装器执行对账。' >&2; exit 65;; esac
test -f "$client_compose"

gateway_container="$(docker ps -q --filter 'label=com.docker.compose.project=memory-gateway' --filter 'label=com.docker.compose.service=app')"
if [ -z "$gateway_container" ]; then
  gateway_container="$(docker ps -q --filter 'label=com.docker.compose.project=memory-gateway' --filter 'label=com.docker.compose.service=gateway')"
fi
set -- $gateway_container
test "$#" -eq 1
gateway_container="$1"
gateway_service="$(docker inspect "$gateway_container" --format '{{ index .Config.Labels "com.docker.compose.service" }}')"
gateway_image_id="$(docker inspect "$gateway_container" --format '{{.Image}}')"
gateway_compose="$(docker inspect "$gateway_container" --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
test -n "$gateway_service" && test "$gateway_service" != '<no value>'
test -n "$gateway_image_id"
test -f "$gateway_compose"
gateway_release="$(dirname "$(dirname "$(dirname "$gateway_compose")")")"
bridge_compose="$gateway_release/deploy/fn/memory-mcp-bridge.compose.yaml"
bridge_env="$state_dir/bridge.env"
test -f "$bridge_compose"
test -r "$bridge_env"
test "$(stat -c %a "$bridge_env")" = 600
test -r "$state_dir/device-identity.pem"
test -r "$state_dir/refresh-credential.json"
test -r "$state_dir/sidecar.env"

client_networks="$(docker inspect "$client_container" --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}')"
gateway_networks="$(docker inspect "$gateway_container" --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}')"
shared_network=0
for client_network in $client_networks; do
  if printf '%s\n' "$gateway_networks" | grep -Fx "$client_network" >/dev/null; then
    shared_network=1
    break
  fi
done
if [ "$shared_network" != 1 ]; then
  echo 'Gateway 与目标 Agent 容器没有共同的 Docker 网络，无法恢复 Bridge。' >&2
  exit 69
fi

bridge_ids="$(docker ps -aq --filter "label=com.docker.compose.project=$client_project" --filter 'label=com.docker.compose.service=memory-mcp-bridge')"
bridge_status=absent
bridge_network_mode=absent
bridge_image_id=absent
needs_recreate=1
if [ -n "$bridge_ids" ]; then
  set -- $bridge_ids
  test "$#" -eq 1
  bridge_id="$1"
  bridge_status="$(docker inspect "$bridge_id" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')"
  bridge_network_mode="$(docker inspect "$bridge_id" --format '{{.HostConfig.NetworkMode}}')"
  bridge_image_id="$(docker inspect "$bridge_id" --format '{{.Image}}')"
  if [ "$bridge_status" = healthy ] && [ "$bridge_network_mode" = "container:$client_id" ] && [ "$bridge_image_id" = "$gateway_image_id" ]; then
    needs_recreate=0
  fi
fi

gateway_url="http://$gateway_service:8787"
printf '%s\n' \
  "client_container=$client_container" \
  "client_service=$client_service" \
  "client_status=$client_status" \
  "bridge_status=$bridge_status" \
  "bridge_network_mode=$bridge_network_mode" \
  "gateway_service=$gateway_service" \
  "gateway_url=$gateway_url" \
  "needs_recreate=$needs_recreate"
if [ "$apply" != 1 ]; then
  printf '%s\n' 'status=waiting_for_apply'
  exit 0
fi
if [ "$client_status" != running ]; then
  echo '目标 Agent 容器当前未运行；请先恢复它，再重建 Bridge。' >&2
  exit 69
fi

uid="$(stat -c %u "$state_dir")"
gid="$(stat -c %g "$state_dir")"
bridge_env_candidate="$bridge_env.next"
if [ -e "$bridge_env_candidate" ]; then
  echo '发现未完成的 Bridge 配置候选文件；请先核对后再继续。' >&2
  exit 65
fi
umask 077
{
  printf '%s\n' "MEMORY_CLIENT_SERVICE=$client_service"
  printf '%s\n' "MEMORY_SIDECAR_STATE_DIR=$state_dir"
  printf '%s\n' "MEMORY_GATEWAY_URL=$gateway_url"
  printf '%s\n' "MEMORY_SIDECAR_UID=$uid"
  printf '%s\n' "MEMORY_SIDECAR_GID=$gid"
  grep -E '^(MEMORY_AGENT_INSTALLATION_ID|MEMORY_DEFAULT_WORKSPACE|MEMORY_DEVICE_ID)=' "$bridge_env"
} > "$bridge_env_candidate"
chmod 0600 "$bridge_env_candidate"

docker compose --project-name "$client_project" --env-file "$bridge_env_candidate" -f "$client_compose" -f "$bridge_compose" config -q
mv "$bridge_env_candidate" "$bridge_env"
docker compose --project-name "$client_project" --env-file "$bridge_env" -f "$client_compose" -f "$bridge_compose" up -d --no-deps --force-recreate memory-mcp-bridge
bridge_id="$(docker compose --project-name "$client_project" --env-file "$bridge_env" -f "$client_compose" -f "$bridge_compose" ps -q memory-mcp-bridge)"
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

docker exec -e MEMORY_VERIFY_AGENT="$(grep '^MEMORY_AGENT_INSTALLATION_ID=' "$bridge_env" | cut -d= -f2-)" -e MEMORY_VERIFY_WORKSPACE="$(grep '^MEMORY_DEFAULT_WORKSPACE=' "$bridge_env" | cut -d= -f2-)" "$bridge_id" python -c 'import os; from pathlib import Path; from agent_memory_gateway.sidecar_daemon import LocalSidecarProxy, daemon_auth_token; values = dict(line.split("=", 1) for line in Path("/state/sidecar.env").read_text(encoding="utf-8").splitlines() if "=" in line); proxy = LocalSidecarProxy("http://127.0.0.1:8766", daemon_auth_token(values["MEMORY_OUTBOX_KEY"]), os.environ["MEMORY_VERIFY_AGENT"]); assert proxy.health(); result = proxy.sync(os.environ["MEMORY_VERIFY_WORKSPACE"]); assert result.get("offline") is False and not result.get("errors"); print("sidecar_gateway_sync=ready")'
docker exec "$bridge_id" python -c 'import json; from urllib.request import Request, urlopen; body = json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"bridge-check","version":"1"}}}).encode(); request = Request("http://127.0.0.1:8767/mcp", data=body, headers={"Content-Type":"application/json","Accept":"application/json, text/event-stream"}, method="POST"); response = urlopen(request, timeout=10); assert response.status == 200; print("mcp_endpoint=ready")'
printf '%s\n' 'status=ready'
'@

foreach ($name in $quoted.Keys) {
    $remoteScript = $remoteScript.Replace(("__" + $name + "__"), $quoted[$name])
}
Invoke-RemoteScript -Script $remoteScript
