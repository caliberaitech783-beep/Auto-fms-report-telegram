param(
    [Parameter(Mandatory = $true)]
    [string]$OldTaskName
)
$Task = Get-ScheduledTask -TaskName $OldTaskName -ErrorAction Stop
if ($Task.TaskName -eq "Scania API PDF Reports") {
    throw "Refusing to disable the new API task."
}
Disable-ScheduledTask -TaskName $Task.TaskName -ErrorAction Stop | Out-Null
Write-Host "Disabled old browser-dependent task: $($Task.TaskName)"
