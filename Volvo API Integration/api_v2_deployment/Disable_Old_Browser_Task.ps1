# Run once with PowerShell as Administrator after confirming APIv2 output.
Disable-ScheduledTask -TaskName "Volvo Auto Report" -ErrorAction Stop | Out-Null
Write-Host "Disabled old browser-dependent task: Volvo Auto Report"
