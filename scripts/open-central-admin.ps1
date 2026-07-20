[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SshHost,

    [ValidateRange(1, 65535)]
    [int]$SshPort = 22,

    [Parameter(Mandatory)]
    [string]$RemoteRoot,

    [Parameter(Mandatory)]
    [string]$StateDirectory
)

$ErrorActionPreference = "Stop"

function Require-RemotePath([string]$Name, [string]$Value) {
    if ($Value -notmatch "^/[A-Za-z0-9._/-]+$") {
        throw "$Name 必须是没有空格的 Linux 绝对路径。"
    }
}

function Invoke-RemoteLaunch([string]$Script) {
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
    $launchUrl = $standardOutput.GetAwaiter().GetResult().Trim()
    $errorOutput = $standardError.GetAwaiter().GetResult().Trim()
    if ($process.ExitCode -ne 0) {
        if ($errorOutput) {
            throw "打开中枢管理页失败：$errorOutput"
        }
        throw "打开中枢管理页失败，退出码：$($process.ExitCode)"
    }
    if ($launchUrl -notmatch "^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/admin/\?session=[A-Za-z0-9_-]{32,}$") {
        throw "中枢返回的启动链接格式无效。"
    }
    return $launchUrl
}

Require-RemotePath -Name "RemoteRoot" -Value $RemoteRoot
Require-RemotePath -Name "StateDirectory" -Value $StateDirectory
if (-not $StateDirectory.StartsWith("$($RemoteRoot.TrimEnd('/'))/", [StringComparison]::Ordinal)) {
    throw "StateDirectory 必须位于 RemoteRoot 内，避免误写到其他 NAS 目录。"
}

$quotedRoot = "'" + $RemoteRoot.Replace("'", "'`"'`"'") + "'"
$quotedState = "'" + $StateDirectory.Replace("'", "'`"'`"'") + "'"
$remoteScript = @'
set -eu

remote_root=__REMOTE_ROOT__
state_dir=__STATE_DIRECTORY__
test -d "$remote_root"
test -d "$state_dir"

set -- $(docker ps -q --filter 'label=com.docker.compose.project=memory-gateway' --filter 'label=com.docker.compose.service=gateway')
test "$#" -eq 1
gateway_container="$1"
gateway_compose="$(docker inspect "$gateway_container" --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
test -f "$gateway_compose"
gateway_release="$(dirname "$(dirname "$(dirname "$gateway_compose")")")"
base_env="$gateway_release/.env"
admin_compose="$gateway_release/deploy/fn/admin-console.compose.yaml"
admin_env="$state_dir/admin.env"
launch_file="$state_dir/console/launch-url"
test -f "$base_env"
test -f "$admin_compose"
test -r "$admin_env"
test "$(stat -c %a "$admin_env")" = 600

previous_fingerprint="$(sha256sum "$launch_file" 2>/dev/null | awk '{print $1}' || true)"
docker compose --project-name memory-gateway --env-file "$base_env" --env-file "$admin_env" -f "$gateway_compose" -f "$admin_compose" up -d --no-build --no-deps --force-recreate admin-console >/dev/null

attempt=0
while [ "$attempt" -lt 30 ]; do
  current_fingerprint="$(sha256sum "$launch_file" 2>/dev/null | awk '{print $1}' || true)"
  if [ -n "$current_fingerprint" ] && [ "$current_fingerprint" != "$previous_fingerprint" ]; then
    test "$(stat -c %a "$launch_file")" = 600
    cat "$launch_file"
    exit 0
  fi
  attempt=$((attempt + 1))
  sleep 1
done

echo '一次性启动链接未在 30 秒内生成。请检查 admin-console 容器日志。' >&2
exit 69
'@
$remoteScript = $remoteScript.Replace("__REMOTE_ROOT__", $quotedRoot).Replace("__STATE_DIRECTORY__", $quotedState)

$launchUrl = Invoke-RemoteLaunch -Script $remoteScript
Start-Process -FilePath $launchUrl
Write-Output "已在默认浏览器打开中枢管理页。该链接只可使用一次，未写入控制台或操作记录。"
