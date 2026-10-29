param()
$ErrorActionPreference = 'Stop'

$taskName = 'YashiPDF.Online Autopilot Monitor'
$projectRoot = Split-Path -Parent $PSScriptRoot
$monitorScript = Join-Path $PSScriptRoot 'monitor.py'
$pythonCommand = Get-Command python -ErrorAction Stop
$pythonConsole = $pythonCommand.Source
$pythonWindowless = Join-Path (Split-Path -Parent $pythonConsole) 'pythonw.exe'
$pythonExecutable = if (Test-Path -LiteralPath $pythonWindowless) { $pythonWindowless } else { $pythonConsole }
$arguments = '"' + $monitorScript + '"'
$action = New-ScheduledTaskAction -Execute $pythonExecutable -Argument $arguments -WorkingDirectory $projectRoot
$userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$matchingProcesses = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine.IndexOf($monitorScript, [StringComparison]::OrdinalIgnoreCase) -ge 0
})
$actionMatches = $false
$settingsMatch = $false
$logonTriggerExists = $false
if ($existing) {
    $actionMatches = $existing.Actions.Count -eq 1 -and
        $existing.Actions[0].Execute -ieq $pythonExecutable -and
        $existing.Actions[0].Arguments -ceq $arguments -and
        $existing.Actions[0].WorkingDirectory -ieq $projectRoot
    $settingsMatch = $existing.Settings.MultipleInstances -eq 'IgnoreNew' -and
        $existing.Settings.StartWhenAvailable -and
        $existing.Settings.RestartCount -eq 5 -and
        $existing.Settings.RestartInterval.ToString() -eq 'PT5M' -and
        $existing.Settings.ExecutionTimeLimit.ToString() -eq 'PT0S'
    $logonTriggerExists = @($existing.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' }).Count -gt 0
}

# Task Scheduler must own the monitor process. A PowerShell wrapper can be
# stopped while its Python child survives and creates a duplicate on restart.
$needsRestart = -not $existing -or -not $actionMatches -or -not $settingsMatch -or -not $logonTriggerExists -or $matchingProcesses.Count -ne 1 -or $existing.State -ne 'Running'
if ($needsRestart) {
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'data\monitor.sqlite3')) {
        $dbPath = Join-Path $PSScriptRoot 'data\monitor.sqlite3'
        $auditState = & $pythonConsole -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=5); r=c.execute('SELECT status FROM runs ORDER BY id DESC LIMIT 1').fetchone(); print(r[0] if r else 'empty'); c.close()" $dbPath
        if ($LASTEXITCODE -ne 0) { throw 'Could not safely inspect monitor history; refusing to replace a running monitor.' }
        if ([string]$auditState -eq 'running') { throw 'An audit is currently running. Wait for it to finish, then run this installer again.' }
    }

    if ($existing -and $existing.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $taskName
        $deadline = (Get-Date).AddSeconds(30)
        do {
            Start-Sleep -Seconds 1
            $existing = Get-ScheduledTask -TaskName $taskName
        } while ($existing.State -eq 'Running' -and (Get-Date) -lt $deadline)
        if ($existing.State -eq 'Running') { throw 'The existing YashiPDF monitor task did not stop; no processes were terminated.' }
    }

    # Clean up only processes whose command line names this exact monitor.py.
    foreach ($process in $matchingProcesses) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($process in $matchingProcesses) {
        try { Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction Stop } catch { }
        if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) {
            throw "YashiPDF monitor process $($process.ProcessId) did not exit; refusing to start a duplicate."
        }
    }

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Runs the local YashiPDF monitor at user sign-in. Its SQLite-backed scheduler runs daily audits and retries failed audits.' -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
}

$port = if ($env:YASHIPDF_PORT) { $env:YASHIPDF_PORT } else { '8765' }
$healthUri = 'http://127.0.0.1:' + $port + '/health'
$healthy = $false
for ($i = 0; $i -lt 120; $i++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUri -TimeoutSec 2
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { Start-Sleep -Seconds 1 }
}
$registered = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
if (-not $healthy) { throw "Task '$taskName' is registered but $healthUri did not return HTTP 200. Check Task Scheduler history, monitor-host.log, and monitor/data/monitor.sqlite3." }
$finalProcesses = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine.IndexOf($monitorScript, [StringComparison]::OrdinalIgnoreCase) -ge 0
})
[pscustomobject]@{
    TaskName = $registered.TaskName
    State = $registered.State
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    Health = $healthUri
    RegisteredUser = $userId
    MonitorExecutable = $pythonExecutable
    MatchingMonitorProcesses = $finalProcesses.Count
} | Format-List
