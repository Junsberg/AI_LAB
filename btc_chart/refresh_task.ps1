# Registers (or removes) a per-user Windows scheduled task that rebuilds the dashboard.
#   powershell -ExecutionPolicy Bypass -File btc_chart\refresh_task.ps1            # install
#   powershell -ExecutionPolicy Bypass -File btc_chart\refresh_task.ps1 -Uninstall # remove
# Runs at logon and then hourly while you are logged in. If the PC was off at a scheduled
# time, the task runs once as soon as it can (StartWhenAvailable). No admin rights needed.
param(
    [switch]$Uninstall,
    [int]$EveryMinutes = 60,
    [string]$ExtraArgs = ""   # e.g. "--skip coinbase"
)

$ErrorActionPreference = "Stop"
$name = "BTC_Chart_Refresh"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "removed $name"
    return
}

$repo = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python).Source
$log = Join-Path $repo "btc_chart\out\refresh.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

# The log is trimmed to its last 300 lines on every run so it cannot grow without bound.
$cmd = "Set-Location '$repo'; " +
    "`$t = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'; " +
    "`$o = & '$python' -m btc_chart $ExtraArgs 2>&1 | Out-String; " +
    "Add-Content -Path '$log' -Value ('[' + `$t + '] ' + `$o.Trim()); " +
    "`$k = @(Get-Content '$log' -Tail 300); Set-Content -Path '$log' -Value `$k"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -Command `"$cmd`"" -WorkingDirectory $repo

$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger @($logon, $hourly) `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "installed $name (logon + every $EveryMinutes min); log: $log"
