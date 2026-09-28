$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidPath = Join-Path $root 'data/server.pid'

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host 'No Agent Board process started by this folder was found.'
    exit 0
}

$serverProcessId = 0
if (-not [int]::TryParse((Get-Content -LiteralPath $pidPath -Raw), [ref]$serverProcessId)) {
    Remove-Item -LiteralPath $pidPath -ErrorAction SilentlyContinue
    Write-Host 'Removed an invalid process marker.'
    exit 0
}

$process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $serverProcessId" -ErrorAction SilentlyContinue
$expectedServer = [IO.Path]::GetFullPath((Join-Path $root 'server.py'))
if ($process -and $process.CommandLine -and $process.CommandLine.Contains($expectedServer) -and $process.CommandLine.Contains('--web')) {
    Stop-Process -Id $serverProcessId -Force
    Write-Host 'Agent Board stopped.'
} else {
    Write-Host 'The saved process ID no longer points to the Agent Board server.'
}
Remove-Item -LiteralPath $pidPath -ErrorAction SilentlyContinue
