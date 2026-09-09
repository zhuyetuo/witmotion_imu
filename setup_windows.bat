@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================================
REM  新机器装机引导（Windows）
REM
REM  用法：右键「以管理员身份运行」，或者双击（会自己要一次管理员权限）。
REM        装到别的盘：  setup_windows.bat D:\witmotion_imu
REM
REM  这个 .bat 只做三件"非它不可"的事，剩下全部交给 setup_windows.sh：
REM
REM    1) 提权。Git Bash 没法自己弹 UAC，而改电源设置必须管理员。
REM    2) 装 Git。装机脚本写成 .sh 的话，得先有 Git Bash 才能跑——新机器上
REM       正好没有，先有鸡还是先有蛋。所以这一步只能由不依赖任何东西的 .bat 做。
REM    3) 拉仓库。setup_windows.sh 本身就在仓库里，没拉下来就没得跑。
REM
REM  之后的 miniconda / ffmpeg / pip / 电源设置都在 setup_windows.sh 里。
REM  逻辑只有一份，跟平时用的 record_multicam.sh、daily_archive.sh 同一套工具链；
REM  已经装了 Git 的机器可以跳过这个 .bat，直接在 Git Bash 里跑那个 .sh。
REM ============================================================================

set "REPO_URL=https://github.com/zhuyetuo/witmotion_imu"
set "INSTALL_DIR=%~1"
if "%INSTALL_DIR%"=="" set "INSTALL_DIR=%USERPROFILE%\witmotion_imu"

REM git-for-windows 的安装包在 GitHub 上，国内常年慢，用 npmmirror 的镜像
set "GIT_MIRROR=https://registry.npmmirror.com/-/binary/git-for-windows"
set "TMPDIR=%TEMP%\wit_setup"
if not exist "%TMPDIR%" mkdir "%TMPDIR%" >nul 2>&1

REM %ProgramFiles(x86)% 的变量名自带右括号，写在 if(...) 块里会把块提前闭合，
REM 报的是看不懂的语法错。先在块外取出来存成普通变量。
set "PF86=%ProgramFiles(x86)%"

REM ── 提权 ─────────────────────────────────────────────────────────────────
net session >nul 2>&1
if errorlevel 1 (
    echo 需要管理员权限^(改电源设置^)，正在重新以管理员身份启动...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%INSTALL_DIR%' -Verb RunAs"
    exit /b 0
)

echo.
echo ============================================================
echo   witmotion 采集环境安装
echo   代码装到: %INSTALL_DIR%
echo ============================================================
echo.

REM ── 1. Git ───────────────────────────────────────────────────────────────
echo [引导 1/3] Git
where git >nul 2>&1
if not errorlevel 1 goto :git_ok

where winget >nul 2>&1
if not errorlevel 1 (
    echo       用 winget 安装...
    winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements --silent
) else (
    echo       从镜像下载安装包...
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

REM 刚装完，当前这个 cmd 的 PATH 还是旧的，去默认位置找
where git >nul 2>&1
if errorlevel 1 (
    if exist "%ProgramFiles%\Git\cmd\git.exe" set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
    if exist "%PF86%\Git\cmd\git.exe" set "PATH=%PF86%\Git\cmd;%PATH%"
)
where git >nul 2>&1
if errorlevel 1 (
    echo       [失败] 装完还是找不到 git，后面没法继续
    echo              手动装一下 Git for Windows 再跑一次本脚本
    pause
    exit /b 1
)
:git_ok
for /f "tokens=*" %%v in ('git --version 2^>nul') do echo       %%v

REM ── 2. 拉仓库 ────────────────────────────────────────────────────────────
echo [引导 2/3] 代码仓库
if exist "%INSTALL_DIR%\.git" (
    echo       已存在，拉取更新...
    pushd "%INSTALL_DIR%"
    git pull --ff-only
    if errorlevel 1 echo       [提醒] git pull 没成功，本地可能有改动，先自己看一眼
    popd
) else (
    echo       clone 到 %INSTALL_DIR% ...
    git clone "%REPO_URL%" "%INSTALL_DIR%"
)
if not exist "%INSTALL_DIR%\setup_windows.sh" (
    echo       [失败] 仓库里没有 setup_windows.sh，clone 可能没成功
    echo              私有仓库的话先配好 GitHub 账号再重跑
    pause
    exit /b 1
)
echo       完成

REM ── 3. 交给 setup_windows.sh ─────────────────────────────────────────────
echo [引导 3/3] 交给 setup_windows.sh 装其余部分
echo.
set "BASH="
if exist "%ProgramFiles%\Git\bin\bash.exe" set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined BASH if exist "%PF86%\Git\bin\bash.exe" set "BASH=%PF86%\Git\bin\bash.exe"
if not defined BASH (
    echo [失败] 找不到 Git Bash 的 bash.exe
    pause
    exit /b 1
)
"%BASH%" -lc "cd '%INSTALL_DIR:\=/%' && ./setup_windows.sh"

echo.
pause
exit /b 0
