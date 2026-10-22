$TaskName = "Volvo APIv2 PDF Reports"
$Bat = Join-Path $PSScriptRoot "run_api_reports.bat"
$times = @("10:30 AM", "11:45 AM", "3:00 PM", "5:00 PM", "6:45 PM")
$dailyTriggers = foreach ($t in $times) { New-ScheduledTaskTrigger -Daily -At ([datetime]$t) }
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$triggers = @($dailyTriggers) + @($logonTrigger)
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -Command `"& '$Bat'`"" `
    -WorkingDirectory $PSScriptRoot
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings `
    -Description "Browser-free Volvo API PDFs at 10:30, 11:45, 15:00, 17:00, 18:45; same-day recovery at Windows login" | Out-Null
Write-Host "Created $TaskName. PDFs: $PSScriptRoot\output\pdf"
