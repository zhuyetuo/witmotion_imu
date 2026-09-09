@echo off
setlocal enabledelayedexpansion

REM ============================================================================
REM  Register the two scheduled tasks that make this rig run unattended.
REM  Run ONCE per machine, as Administrator (it elevates itself).
REM
REM      install_autostart.bat gouchang
REM      install_autostart.bat yingpeng
REM
REM  To remove everything again:  install_autostart.bat /uninstall
REM
REM  THIS FILE IS PURE ASCII ON PURPOSE (see setup_windows.bat), and so is the
REM  site name it writes. The real site configs are named in Chinese
REM  (the folder holds sites\<Chinese name>.env), but each declares SITE_ALIAS="gouchang"; the shell
REM  scripts resolve the alias themselves. So no non-ASCII text ever has to
REM  survive cmd.exe, a .bat file, or a Task Scheduler command line.
REM
REM  What gets registered:
REM
REM    "IMU record"  -> record_autostart.bat, at logon
REM    "IMU archive" -> daily_archive.bat,    daily at 00:05
REM
REM  Why AT LOGON and not AT STARTUP: USB cameras and the Bluetooth stack are
REM  reached through the interactive desktop session. A task running before
REM  anyone logs in (session 0) can open neither reliably. So the machine has to
REM  log itself in -- set that up once:
REM
REM      Win+R -> netplwiz -> untick "Users must enter a user name and password"
REM
REM  After a power cut the machine then boots, logs in, and starts recording on
REM  its own with nobody present. That is the whole point.
REM
REM  Why 00:05 for the archive: just past midnight, recording has already rolled
REM  over into the new day's folder and yesterday's last hourly segment has been
REM  written. daily_archive.sh still checks for itself that the day is quiet
REM  before touching anything, and it never deletes original data.
REM ============================================================================

set "SITE_ARG=%~1"

net session >nul 2>&1
if errorlevel 1 (
    echo Administrator rights are required. Relaunching...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%SITE_ARG%' -Verb RunAs"
    exit /b 0
)

cd /d "%~dp0"

if /i "%SITE_ARG%"=="/uninstall" goto :uninstall

set "BASH=C:\Program Files\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=C:\Program Files (x86)\Git\bin\bash.exe"
if not exist "%BASH%" (
    echo Cannot find Git Bash. Run setup_windows.bat first.
    pause
    exit /b 1
)

if "%SITE_ARG%"=="" (
    echo Usage: install_autostart.bat gouchang ^| yingpeng
    echo        install_autostart.bat /uninstall
    pause
    exit /b 1
)

REM -- 1. Remember which site this machine is -------------------------------
REM Both record_multicam.sh and daily_archive.sh read sites\.current, so the
REM scheduled tasks do not need the site on their command line. The alias is
REM written as-is; the shell scripts map it to sites\<Chinese name>.env.
echo [1/3] site: %SITE_ARG%
<nul set /p ="%SITE_ARG%" > sites\.current

REM Verify through bash that the alias actually resolves to a config file --
REM a typo here would otherwise only surface as "no data recorded" tomorrow.
"%BASH%" -lc "for f in sites/*.env; do grep -qE '^[[:space:]]*SITE_ALIAS=\"?%SITE_ARG%\"?[[:space:]]*$' \"$f\" && exit 0; done; exit 1"
if errorlevel 1 (
    echo       [FAILED] no site config declares SITE_ALIAS="%SITE_ARG%".
    echo                Known aliases:
    "%BASH%" -lc "grep -h '^SITE_ALIAS=' sites/*.env | sed 's/SITE_ALIAS=/                  /;s/\"//g'"
    del sites\.current >nul 2>&1
    pause
    exit /b 1
)

REM -- 2. Recording, at logon ------------------------------------------------
echo [2/3] task "IMU record" (at logon)
schtasks /Create /TN "IMU record" /TR "\"%~dp0record_autostart.bat\"" /SC ONLOGON /RL HIGHEST /F >nul
if errorlevel 1 (
    echo       [FAILED] could not register the recording task
    pause
    exit /b 1
)
REM Defaults that would quietly stop an unattended rig:
REM   /DY  ExecutionTimeLimit -- Task Scheduler kills a task after 72 hours by
REM        default. This one is meant to run for months.
REM   Stop-if-on-batteries and stop-when-idle-ends are off for the same reason.
powershell -NoProfile -Command ^
  "$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1);" ^
  "Set-ScheduledTask -TaskName 'IMU record' -Settings $s | Out-Null" 2>nul
if errorlevel 1 echo       [note] could not relax the task limits; check "72 hour" limit by hand in Task Scheduler

REM -- 3. Archive, daily at 00:05 -------------------------------------------
echo [3/3] task "IMU archive" (daily 00:05)
schtasks /Create /TN "IMU archive" /TR "\"%~dp0daily_archive.bat\"" /SC DAILY /ST 00:05 /RL HIGHEST /F >nul
if errorlevel 1 (
    echo       [FAILED] could not register the archive task
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done. This machine is set up as: %SITE_ARG%
echo.
echo   Recording starts by itself at every logon.
echo   Archiving runs every night at 00:05.
echo.
echo   Still to do by hand, once:
echo     - auto login, so a power cut recovers on its own:
echo         Win+R -^> netplwiz -^> untick "Users must enter..."
echo.
echo   Start recording now without rebooting:
echo         schtasks /Run /TN "IMU record"
echo   Stop it for a while (no need to touch Task Scheduler):
echo         create a file named  .recording_disabled  in this folder
echo         and kill the running python; delete the file to resume
echo   Logs:  logs\
echo ============================================================
echo.
pause
exit /b 0

:uninstall
schtasks /Delete /TN "IMU record" /F 2>nul
schtasks /Delete /TN "IMU archive" /F 2>nul
echo Both tasks removed. sites\.current was left alone.
pause
exit /b 0
