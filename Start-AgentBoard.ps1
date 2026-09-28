param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$url = 'http://127.0.0.1:8765'
$healthUrl = "$url/api/board"
$serverPath = Join-Path $root 'server.py'
$dataDir = Join-Path $root 'data'
$pidPath = Join-Path $dataDir 'server.pid'

try {
    Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 1 | Out-Null
    if (-not $NoBrowser) { Start-Process $url }
    exit 0
} catch { }

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdoutPath = Join-Path $dataDir "server-$stamp.log"
$stderrPath = Join-Path $dataDir "server-$stamp-error.log"
$arguments = '"' + $serverPath + '" --web'
$process = Start-Process -FilePath 'python' -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
Set-Content -LiteralPath $pidPath -Value $process.Id -NoNewline

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 300
    try {
        Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 1 | Out-Null
        $ready = $true
        break
    } catch { }
}

if (-not $ready) {
    Write-Error "Agent Board did not start. See $stderrPath"
}

if (-not $NoBrowser) { Start-Process $url }
