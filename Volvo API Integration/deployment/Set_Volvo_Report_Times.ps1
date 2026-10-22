# ============================================================
#  Creates "Volvo Auto Report" - runs run_reports.bat at the
#  fixed daily times below, in your logged-on session (so it can
#  read the live token from the open browser). Like Scania Auto Report.
#  RIGHT-CLICK -> Run with PowerShell (Administrator)
# ============================================================
$TaskName = "Volvo Auto Report"
$Bat = Join-Path $PSScriptRoot "run_reports.bat"

if (-not (Test-Path $Bat)) {
    Write-Host "ERROR: run_reports.bat not found next to this script." -ForegroundColor Red
    Read-Host "Press Enter to exit"; exit
}

# --- the 5 report times ---
$times = @("10:30 AM", "11:45 AM", "3:00 PM", "5:00 PM", "6:45 PM")
$triggers = @()
foreach ($t in $times) { $triggers += New-ScheduledTaskTrigger -Daily -At ([datetime]$t) }

$Action    = New-ScheduledTaskAction -Execute "C:\Windows\System32\cmd.exe" `
                -Argument "/c `"$Bat`"" -WorkingDirectory $PSScriptRoot
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$Settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -MultipleInstances IgnoreNew `
                -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 2)

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $triggers `
    -Principal $Principal -Settings $Settings -Description "Volvo reports at 10:30, 11:45, 15:00, 17:00, 18:45" | Out-Null

Write-Host "Created task '$TaskName'." -ForegroundColor Green
Write-Host "Runs run_reports.bat daily at: $($times -join ', ')"
Write-Host "PDF destination: $PSScriptRoot\reports\WhatsApp_Pending"
Write-Host "Test now:  Start-ScheduledTask -TaskName 'Volvo Auto Report'"
Read-Host "Press Enter to exit"
