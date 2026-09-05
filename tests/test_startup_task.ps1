$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskName = 'AIManhuaWorkflow-Test-' + [Guid]::NewGuid().ToString('N')
$runner = Join-Path $project 'scripts\install_startup.ps1'
try {
  & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $runner -ProjectDir $project -TaskName $taskName
  if ($LASTEXITCODE -ne 0) { throw "installer failed: $LASTEXITCODE" }
  $task = Get-ScheduledTask -TaskName $taskName
  if ($task.Triggers.Count -ne 2) { throw "expected logon and watchdog triggers, got $($task.Triggers.Count)" }
  $logon = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
  $calendar = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskDailyTrigger' })
  if ($logon.Count -ne 1 -or $calendar.Count -ne 1) { throw 'missing required trigger types' }
  if ($logon[0].Delay -ne 'PT1M') { throw "unexpected logon delay: $($logon[0].Delay)" }
  if ($calendar[0].Repetition.Interval -ne 'PT10M') { throw "unexpected watchdog interval: $($calendar[0].Repetition.Interval)" }
  if ($calendar[0].Repetition.Duration -ne 'PT9H') { throw "unexpected watchdog duration: $($calendar[0].Repetition.Duration)" }
  [xml]$xml = Export-ScheduledTask -TaskName $taskName
  $daily = @($xml.Task.Triggers.CalendarTrigger | Where-Object { $_.ScheduleByDay.DaysInterval -eq '1' })
  if ($daily.Count -ne 1) { throw 'watchdog is not daily' }
  $startHour = ([datetime]$calendar[0].StartBoundary).Hour
  if ($startHour -ne 15) { throw "watchdog does not start at 15:00: $startHour" }
  if ($task.Principal.LogonType -ne 'Interactive') { throw "unexpected logon type: $($task.Principal.LogonType)" }
  if ($task.Settings.MultipleInstances -ne 'IgnoreNew') { throw "unexpected multiple instance policy: $($task.Settings.MultipleInstances)" }
  Write-Output 'STARTUP_TASK_TEST_PASS'
}
finally {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}
