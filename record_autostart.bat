@echo off
REM ============================================================================
REM  Unattended recording, started by Task Scheduler at logon.
REM  Registered once by install_autostart.bat -- do not run this by hand.
REM
REM  THIS FILE IS PURE ASCII ON PURPOSE (see setup_windows.bat for the reason:
REM  cmd reads a .bat byte by byte and non-ASCII comments can derail it).
REM
REM  What it adds on top of record_multicam.sh:
REM    1) No preview windows. Nobody is sitting there to press "p", and six
REM       720p imshow windows cost about 40% of the frame rate.
REM    2) Restart on crash. USB cameras drop off, the Bluetooth stack wedges.
REM       A rig that stops recording at 03:00 and nobody notices until morning
REM       is worse than one that loses 30 seconds and keeps going.
REM    3) An off switch that does not need Task Scheduler: create the file
REM       .recording_disabled in this folder and the loop exits at the next
REM       restart. Delete it to resume. (Same idea as .archive_disabled.)
REM
REM  Recording itself never needs a daily restart: record_multicam.sh runs with
REM  --align-hourly --loop, so it writes one file per hour and rolls into the
REM  new day's folder by itself.
REM ============================================================================

setlocal
cd /d "%~dp0"

set "BASH=C:\Program Files\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=C:\Program Files (x86)\Git\bin\bash.exe"
if not exist "%BASH%" (
    echo Cannot find Git Bash. Edit BASH in this file.
    exit /b 1
)

if not exist logs mkdir logs

:loop
if exist .recording_disabled (
    echo .recording_disabled found, stopping.
    exit /b 0
)

REM SITE comes from sites\.current, written by install_autostart.bat.
REM EXTRA_ARGS_APPEND adds to the site config's EXTRA_ARGS instead of replacing
REM it -- using EXTRA_ARGS here would silently drop any flag added to the site
REM file later.
"%BASH%" -lc "EXTRA_ARGS_APPEND=--no-preview ./record_multicam.sh"

if exist .recording_disabled (
    echo .recording_disabled found, stopping.
    exit /b 0
)

REM 30s before retrying: long enough for a wedged USB/Bluetooth stack to reset,
REM short enough that a real crash costs well under a minute of data. Without
REM the wait, a config error would spin here thousands of times a minute.
echo [%date% %time%] recorder exited, restarting in 30s >> logs\record_autostart.log
timeout /t 30 /nobreak >nul
goto loop
