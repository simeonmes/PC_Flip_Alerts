# Run this in an elevated PowerShell (right-click PowerShell -> "Run as administrator").
# Registers two scheduled tasks: the scanner and its watchdog, both starting
# at logon, both auto-restarting on failure, both running in a visible
# window under your own session.

$action1 = New-ScheduledTaskAction -Execute "C:\PCFlipScanner\run.bat" -WorkingDirectory "C:\PCFlipScanner"
$trigger1 = New-ScheduledTaskTrigger -AtLogOn
$settings1 = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0) -StartWhenAvailable -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "PCFlipScanner-Main" -TaskPath "\PCFlipScanner\" -Action $action1 -Trigger $trigger1 -Settings $settings1 -Description "Runs the PC Flip Scanner (main.py) as a single process. Starts at logon, auto-restarts on failure." -Force

$action2 = New-ScheduledTaskAction -Execute "C:\PCFlipScanner\run_watchdog.bat" -WorkingDirectory "C:\PCFlipScanner"
$trigger2 = New-ScheduledTaskTrigger -AtLogOn
$settings2 = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0) -StartWhenAvailable -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "PCFlipScanner-Watchdog" -TaskPath "\PCFlipScanner\" -Action $action2 -Trigger $trigger2 -Settings $settings2 -Description "Runs the downtime watchdog (watchdog.py) as a single process, separate from the scanner. Starts at logon, auto-restarts on failure." -Force

Write-Host ""
Write-Host "Done. Verify with:"
Write-Host "  Get-ScheduledTask -TaskPath '\PCFlipScanner\'"
Write-Host ""
Write-Host "To start both right now instead of waiting for next logon:"
Write-Host "  Start-ScheduledTask -TaskName 'PCFlipScanner-Main' -TaskPath '\PCFlipScanner\'"
Write-Host "  Start-ScheduledTask -TaskName 'PCFlipScanner-Watchdog' -TaskPath '\PCFlipScanner\'"
