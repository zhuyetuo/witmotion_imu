@echo off
setlocal enabledelayedexpansion

REM ============================================================================
REM  Bootstrap installer for the capture rig (Windows).
REM
REM    Run as Administrator (right-click, or just double-click -- it elevates
REM    itself). Install somewhere else:  setup_windows.bat D:\witmotion_imu
REM
REM  THIS FILE IS DELIBERATELY PURE ASCII, AND HAS NO "chcp".
REM
REM  It used to carry Chinese comments plus "chcp 65001". That combination
REM  breaks cmd: cmd reads a .bat byte by byte, and switching the code page
REM  mid-file makes it lose its place inside multi-byte characters. The tail
REM  of a comment line then gets executed as a command, e.g.
REM
REM      '...' is not recognized as an internal or external command
REM
REM  It stumbles through the comment block, re-syncs, and the rest runs fine --
REM  which is exactly why it looked harmless and unrelated to encoding.
REM  Keeping this file ASCII-only sidesteps the whole class of problem.
REM  (Chinese is fine in setup_windows.sh: bash/UTF-8 handles it properly.)
REM
REM  This .bat only does the three things that ONLY a .bat can do; everything
REM  else lives in setup_windows.sh, which it hands off to at the end:
REM
REM    1) Elevate. Git Bash cannot raise its own UAC prompt, and changing the
REM       power settings requires Administrator.
REM    2) Install Git. If the installer script were a .sh, you would need Git
REM       Bash to run it -- which a fresh machine does not have. Chicken and egg.
REM    3) Clone the repo. setup_windows.sh lives inside it.
REM
REM  A machine that already has Git can skip this file entirely and just run
REM  ./setup_windows.sh from Git Bash.
REM ============================================================================

set "REPO_URL=https://github.com/zhuyetuo/witmotion_imu"
set "INSTALL_DIR=%~1"
if "%INSTALL_DIR%"=="" set "INSTALL_DIR=%USERPROFILE%\witmotion_imu"

REM The git-for-windows installer is hosted on GitHub, which is consistently
REM slow from mainland China. Use the npmmirror mirror instead.
set "GIT_MIRROR=https://registry.npmmirror.com/-/binary/git-for-windows"
set "TMPDIR=%TEMP%\wit_setup"
if not exist "%TMPDIR%" mkdir "%TMPDIR%" >nul 2>&1

REM %ProgramFiles(x86)% has a closing paren in the variable NAME, which closes
REM an if(...) block early and produces a baffling syntax error. Read it out
REM here, outside any block.
set "PF86=%ProgramFiles(x86)%"

REM -- Elevate --------------------------------------------------------------
net session >nul 2>&1
if errorlevel 1 (
    echo Administrator rights are required ^(power settings^). Relaunching...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%INSTALL_DIR%' -Verb RunAs"
    exit /b 0
)

echo.
echo ============================================================
echo   witmotion capture rig setup
echo   Install dir: %INSTALL_DIR%
echo ============================================================
echo.

REM -- 1. Git ---------------------------------------------------------------
echo [bootstrap 1/3] Git
where git >nul 2>&1
if not errorlevel 1 goto :git_ok

where winget >nul 2>&1
if not errorlevel 1 (
    echo       installing via winget...
    winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --silent
) else (
    echo       downloading installer from mirror...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ErrorActionPreference='Stop';" ^
      "$base='%GIT_MIRROR%';" ^
      "$list=Invoke-RestMethod $base;" ^
      "$ver=($list ^| Where-Object { $_.name -match '^\d' } ^| Sort-Object { [version]($_.name.TrimEnd('/')) } ^| Select-Object -Last 1).name;" ^
      "$files=Invoke-RestMethod ($base+'/'+$ver);" ^
      "$exe=$files ^| Where-Object { $_.name -like '*64-bit.exe' } ^| Select-Object -First 1;" ^
      "Invoke-WebRequest $exe.url -OutFile '%TMPDIR%\git.exe'"
    if exist "%TMPDIR%\git.exe" "%TMPDIR%\git.exe" /VERYSILENT /NORESTART /NOCANCEL /SP-
)

REM Freshly installed: this cmd session still has the old PATH, so look in the
REM default locations.
where git >nul 2>&1
if errorlevel 1 (
    if exist "%ProgramFiles%\Git\cmd\git.exe" set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
    if exist "%PF86%\Git\cmd\git.exe" set "PATH=%PF86%\Git\cmd;%PATH%"
)
where git >nul 2>&1
if errorlevel 1 (
    echo       [FAILED] git still not found after install; cannot continue.
    echo                Install Git for Windows by hand, then re-run this script.
    pause
    exit /b 1
)
:git_ok
for /f "tokens=*" %%v in ('git --version 2^>nul') do echo       %%v

REM -- 2. Clone -------------------------------------------------------------
echo [bootstrap 2/3] repository
if exist "%INSTALL_DIR%\.git" (
    echo       already present, pulling...
    pushd "%INSTALL_DIR%"
    git pull --ff-only
    if errorlevel 1 echo       [note] git pull failed; you may have local changes. Check by hand.
    popd
) else (
    echo       cloning into %INSTALL_DIR% ...
    git clone "%REPO_URL%" "%INSTALL_DIR%"
)
if not exist "%INSTALL_DIR%\setup_windows.sh" (
    echo       [FAILED] setup_windows.sh missing; the clone probably did not work.
    echo                For a private repo, set up your GitHub account first.
    pause
    exit /b 1
)
echo       done

REM -- 3. Hand off to setup_windows.sh --------------------------------------
echo [bootstrap 3/3] handing off to setup_windows.sh
echo.
set "BASH="
if exist "%ProgramFiles%\Git\bin\bash.exe" set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined BASH if exist "%PF86%\Git\bin\bash.exe" set "BASH=%PF86%\Git\bin\bash.exe"
if not defined BASH (
    echo [FAILED] cannot find Git Bash ^(bash.exe^)
    pause
    exit /b 1
)
"%BASH%" -lc "cd '%INSTALL_DIR:\=/%' && ./setup_windows.sh"

echo.
pause
exit /b 0
