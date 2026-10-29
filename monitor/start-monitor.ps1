param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogRoot = Join-Path $env:LOCALAPPDATA 'YashiPDFMonitor\logs'
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
$LogPath = Join-Path $LogRoot 'monitor-host.log'
Set-Location -LiteralPath $ProjectRoot
$python = Get-Command python -ErrorAction Stop
Add-Content -LiteralPath $LogPath -Value "$(Get-Date -Format o) Starting monitor with $($python.Source) in $ProjectRoot"
& $python.Source (Join-Path $PSScriptRoot 'monitor.py') *>> $LogPath
$exitCode = $LASTEXITCODE
Add-Content -LiteralPath $LogPath -Value "$(Get-Date -Format o) Monitor exited with code $exitCode"
exit $exitCode
