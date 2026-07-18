[CmdletBinding()]
param(
    [string[]]$Path
)

$ErrorActionPreference = "Stop"

$patterns = @(
    '-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'gh[pousr]_[A-Za-z0-9]{20,}',
    'github_pat_[A-Za-z0-9_]{20,}',
    'sk-[A-Za-z0-9]{20,}',
    'AKIA[0-9A-Z]{16}',
    '(?i)postgres(?:ql)?://[^\s/:]+:[^\s@]+@'
)

if ($Path) {
    $files = $Path | Where-Object { $_ -ne 'tests/fixtures/security_cases.json' }
} else {
    $files = git ls-files | Where-Object {
        $_ -ne 'tests/fixtures/security_cases.json'
    }
}

$findings = foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        continue
    }
    Select-String -LiteralPath $file -Pattern $patterns -AllMatches | ForEach-Object {
        [pscustomobject]@{
            File = $file
            Line = $_.LineNumber
        }
    }
}

if ($findings) {
    $findings | ForEach-Object {
        Write-Error "公开文件疑似包含敏感信息：$($_.File) 行 $($_.Line)"
    }
    exit 1
}

Write-Output "public_sensitive_scan=clean ($($files.Count) files)"
