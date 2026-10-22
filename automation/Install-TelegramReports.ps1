param(
    [Parameter(Mandatory=$true)][string]$Python,
    [string]$Config = (Join-Path (Split-Path $PSScriptRoot) 'telegram_config.json'),
    [switch]$DeliveryOnly
)
$ErrorActionPreference = 'Stop'
$Python = (Resolve-Path -LiteralPath $Python).Path
$Config = (Resolve-Path -LiteralPath $Config).Path
$Runner = Join-Path $PSScriptRoot 'telegram_reports.py'
& $Python $Runner --config $Config --check
if ($LASTEXITCODE -ne 0) { throw 'Telegram configuration verification failed.' }
if ((Get-TimeZone).BaseUtcOffset -ne [TimeSpan]::FromMinutes(330)) {
    throw 'Set this report computer to India Standard Time before installing the schedule.'
}
$runnerArguments = "`"$Runner`" --config `"$Config`""
if ($DeliveryOnly) { $runnerArguments += ' --delivery-only' }
$action = New-ScheduledTaskAction -Execute $Python -Argument $runnerArguments -WorkingDirectory (Split-Path $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$login = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName 'Fleet Reports to Telegram' -Action $action -Trigger @($trigger,$login) -Principal $principal -Settings $settings -Description 'Generate due Scania and Volvo PDFs and retry pending Telegram deliveries every five minutes.' -Force | Out-Null
Write-Host 'Installed Fleet Reports to Telegram. Runs while this user is logged in and the computer is awake.'
