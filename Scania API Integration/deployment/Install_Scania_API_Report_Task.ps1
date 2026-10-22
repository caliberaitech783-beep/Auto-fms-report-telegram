$TaskName = "Scania API PDF Reports"
$Bat = Join-Path $PSScriptRoot "run_scania_reports.bat"
$Times = @("10:30 AM", "11:45 AM", "3:00 PM", "5:00 PM", "6:45 PM")
$Triggers = foreach ($Time in $Times) {
    New-ScheduledTaskTrigger -Daily -At ([datetime]$Time)
}
$Triggers += New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Action = New-ScheduledTaskAction -Execute "C:\Windows\System32\cmd.exe" `
    -Argument "/c `"$Bat`"" -WorkingDirectory (Split-Path $PSScriptRoot)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 6 `
    -RestartInterval (New-TimeSpan -Minutes 5)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers `
    -Principal $Principal -Settings $Settings `
    -Description "Browser-free Scania API PDF reports with current-date catch-up" | Out-Null
Write-Host "Created scheduled task: $TaskName"
Start-ScheduledTask -TaskName $TaskName
Write-Host "Started current-date catch-up check."
