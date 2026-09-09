@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================================
REM  新机器一键装采集环境（Windows）
REM
REM  用法：右键「以管理员身份运行」，或者双击（会自己要一次管理员权限）。
REM        装到别的盘：  setup_windows.bat D:\witmotion_imu
REM
REM  为什么不用 Docker：这个程序要直接开 USB 摄像头和蓝牙。Windows 上的 Docker
REM  容器跑在 WSL2 的 Linux 虚拟机里，既看不到摄像头（没有 /dev/video*，也没有
REM  DirectShow），也拿不到蓝牙适配器。而 bleak 在 Windows 用的是 WinRT 后端，
REM  进了 Linux 容器要换成 BlueZ，等于另一套栈重趟一遍。容器的作用是把硬件隔开，
REM  而这个程序的全部工作就是贴着硬件跑。
REM
REM  设计上的两条原则：
REM   1) 可以反复跑。每一步先检测再装，装过的直接跳过。
REM   2) 某一步失败不中断。后面照常走，最后统一列出哪几步没成——不然网络抖一下
REM      就得从头再来，而且看不出到底卡在哪。
REM
REM  装完不会自动开录：还要填 sites\<场地>.env（设备 MAC、狗名、摄像头路数），
REM  那个只能人来填。脚本最后会告诉你还差什么。
REM ============================================================================

set "REPO_URL=https://github.com/zhuyetuo/witmotion_imu"
set "INSTALL_DIR=%~1"
if "%INSTALL_DIR%"=="" set "INSTALL_DIR=%USERPROFILE%\witmotion_imu"

REM ffmpeg 装在仓库外面：装在仓库里的话，git clone 会因为目标目录非空而失败，
REM 而且重新 clone 一次就得重下一遍。放这儿跟仓库互不影响。
set "FFDIR=%LOCALAPPDATA%\ffmpeg"

REM 国内直连 PyPI / Anaconda 慢到经常超时，默认走清华镜像
set "PIP_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"
set "PIP_HOST=pypi.tuna.tsinghua.edu.cn"
set "CONDA_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda"
REM git-for-windows 的安装包在 GitHub 上，国内常年慢，用 npmmirror 的镜像
set "GIT_MIRROR=https://registry.npmmirror.com/-/binary/git-for-windows"

REM %ProgramFiles(x86)% 的变量名里带右括号，写在 if(...) 块里会把块提前闭合，
REM 报的是莫名其妙的语法错。先在块外面取出来存成普通变量。
set "PF86=%ProgramFiles(x86)%"

set "FAILED="
set "TMPDIR=%TEMP%\wit_setup"
if not exist "%TMPDIR%" mkdir "%TMPDIR%" >nul 2>&1

REM ── 管理员权限：powercfg 和设备电源设置都需要 ────────────────────────────
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
echo   ffmpeg 装到: %FFDIR%
echo ============================================================
echo.

where winget >nul 2>&1 && (set "HAS_WINGET=1") || (set "HAS_WINGET=")
if defined HAS_WINGET (echo [信息] 检测到 winget) else (echo [信息] 没有 winget，全部改用直接下载)
echo.

call :step_git
call :step_conda
call :step_clone
call :step_ffmpeg
call :step_pip
call :step_power

echo.
echo ============================================================
if defined FAILED (
    echo   装完了，但这几步没成功：
    for %%F in (%FAILED%) do echo     - %%F
    echo.
    echo   把上面的报错发给开发，或者手动装这几项再跑一次本脚本。
) else (
    echo   全部完成。
)
echo ============================================================
echo.
echo 还差最后一步，只能人来做：填场地配置
echo.
echo   1. 拿到每个 IMU 设备的 MAC:
echo        cd /d "%INSTALL_DIR%"
echo        python wit_ble_live.py --scan
echo.
echo   2. 把 MAC、狗名、摄像头路数填进 sites\狗场.env
echo      ^(照着 sites\影棚.env 的写法，设备一律用 MAC 不用名字^)
echo.
echo   3. 试录:
echo        set SITE=狗场
echo        record_multicam.sh
echo.
echo   4. 确认无误后注册每日归档任务^(每天 00:05 自动传 NAS^):
echo        schtasks /Create /TN "IMU每日归档" /TR "%INSTALL_DIR%\daily_archive.bat" /SC DAILY /ST 00:05 /RL HIGHEST /F
echo.
echo 注意：PATH 是这次装的，新开的命令行才生效。当前这个窗口不用管。
echo.
pause
exit /b 0


REM ── 1. Git ───────────────────────────────────────────────────────────────
:step_git
echo [1/6] Git
where git >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%v in ('git --version 2^>nul') do echo       已安装: %%v
    goto :eof
)
if defined HAS_WINGET (
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
REM 装完当前这个 cmd 的 PATH 还是旧的，去默认位置找
where git >nul 2>&1
if errorlevel 1 (
    if exist "%ProgramFiles%\Git\cmd\git.exe" set "PATH=%ProgramFiles%\Git\cmd;%PATH%"
    if exist "%PF86%\Git\cmd\git.exe" set "PATH=%PF86%\Git\cmd;%PATH%"
)
where git >nul 2>&1
if errorlevel 1 (set "FAILED=%FAILED% Git" & echo       [失败] 装完还是找不到 git) else (echo       完成)
goto :eof


REM ── 2. Miniconda ─────────────────────────────────────────────────────────
:step_conda
echo [2/6] Miniconda
set "CONDA_ROOT=%USERPROFILE%\miniconda3"
if exist "%CONDA_ROOT%\python.exe" (
    echo       已安装: %CONDA_ROOT%
    goto :conda_done
)
if exist "%ProgramData%\miniconda3\python.exe" (
    set "CONDA_ROOT=%ProgramData%\miniconda3"
    echo       已安装: !CONDA_ROOT!
    goto :conda_done
)
echo       从清华镜像下载安装包...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "Invoke-WebRequest '%CONDA_MIRROR%/Miniconda3-latest-Windows-x86_64.exe' -OutFile '%TMPDIR%\miniconda.exe'"
if not exist "%TMPDIR%\miniconda.exe" (
    set "FAILED=%FAILED% Miniconda下载"
    echo       [失败] 下载不下来
    goto :eof
)
echo       静默安装到 %CONDA_ROOT% ...
REM /D 必须放最后，而且路径不能加引号——NSIS 安装器的硬性要求，加了引号会静默失败
start /wait "" "%TMPDIR%\miniconda.exe" /InstallationType=JustMe /AddToPath=1 /RegisterPython=0 /S /D=%CONDA_ROOT%
:conda_done
if exist "%CONDA_ROOT%\python.exe" (
    set "PY=%CONDA_ROOT%\python.exe"
    set "PATH=%CONDA_ROOT%;%CONDA_ROOT%\Scripts;%PATH%"
    echo       完成
) else (
    set "FAILED=%FAILED% Miniconda"
    echo       [失败] 装完找不到 python.exe
    REM 退而求其次：系统里本来就有 python 也能用
    where python >nul 2>&1 && set "PY=python"
)
goto :eof


REM ── 3. 拉仓库 ────────────────────────────────────────────────────────────
REM 放在 ffmpeg 前面：git clone 要求目标目录不存在或是空的
:step_clone
echo [3/6] 代码仓库
where git >nul 2>&1
if errorlevel 1 (
    set "FAILED=%FAILED% clone"
    echo       [跳过] 没有 git
    goto :eof
)
if exist "%INSTALL_DIR%\.git" (
    echo       已存在，拉取更新...
    pushd "%INSTALL_DIR%"
    git pull --ff-only
    if errorlevel 1 echo       [提醒] git pull 没成功，本地可能有改动，先自己看一眼
    popd
    goto :eof
)
echo       clone 到 %INSTALL_DIR% ...
git clone "%REPO_URL%" "%INSTALL_DIR%"
if exist "%INSTALL_DIR%\.git" (
    echo       完成
) else (
    set "FAILED=%FAILED% clone"
    echo       [失败] clone 不下来。私有仓库的话先配好 GitHub 账号再重跑
)
goto :eof


REM ── 4. ffmpeg ────────────────────────────────────────────────────────────
REM 录像是把每一帧喂给 ffmpeg 管道写 VFR mp4 的，没有 ffmpeg 完全录不了视频。
REM 下压缩包解压 + 写 PATH，不走安装器：免安装包解到哪都行，出问题删掉目录重来
REM 即可，不会在系统里留一堆卸载不干净的东西。
:step_ffmpeg
echo [4/6] ffmpeg
where ffmpeg >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%v in ('where ffmpeg 2^>nul') do echo       已安装: %%v & goto :ff_ok
)
if exist "%FFDIR%\bin\ffmpeg.exe" (
    echo       已安装: %FFDIR%
    goto :ff_path
)
echo       下载压缩包...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$urls=@('https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'," ^
  "        'https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip');" ^
  "$ok=$false;" ^
  "foreach ($u in $urls) {" ^
  "  try { Write-Host ('      试 ' + $u); Invoke-WebRequest $u -OutFile '%TMPDIR%\ffmpeg.zip' -TimeoutSec 300; $ok=$true; break }" ^
  "  catch { Write-Host ('      这个源不行: ' + $_.Exception.Message) } };" ^
  "if (-not $ok) { exit 1 };" ^
  "Write-Host '      解压...';" ^
  "if (Test-Path '%TMPDIR%\ffx') { Remove-Item '%TMPDIR%\ffx' -Recurse -Force };" ^
  "Expand-Archive -Path '%TMPDIR%\ffmpeg.zip' -DestinationPath '%TMPDIR%\ffx' -Force;" ^
  "$d=Get-ChildItem '%TMPDIR%\ffx' -Directory ^| Select-Object -First 1;" ^
  "if (Test-Path '%FFDIR%') { Remove-Item '%FFDIR%' -Recurse -Force };" ^
  "Move-Item $d.FullName '%FFDIR%'"
if not exist "%FFDIR%\bin\ffmpeg.exe" (
    set "FAILED=%FAILED% ffmpeg"
    echo       [失败] 没装上。没有 ffmpeg 录不了视频，必须补上
    echo              手动办法：下 ffmpeg-release-essentials.zip 解压到 %FFDIR%
    echo              解压后应该能看到 %FFDIR%\bin\ffmpeg.exe
    goto :eof
)
:ff_path
REM 写进用户 PATH（不动系统 PATH，影响面小），并让当前会话也能用
set "PATH=%FFDIR%\bin;%PATH%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p=[Environment]::GetEnvironmentVariable('Path','User');" ^
  "if ($null -eq $p) { $p='' };" ^
  "if ($p -notlike '*%FFDIR%\bin*') {" ^
  "  $new=if ($p -eq '') { '%FFDIR%\bin' } else { $p.TrimEnd(';') + ';%FFDIR%\bin' };" ^
  "  [Environment]::SetEnvironmentVariable('Path', $new, 'User');" ^
  "  Write-Host '      已写入用户 PATH'" ^
  "} else { Write-Host '      用户 PATH 里已经有了' }"
"%FFDIR%\bin\ffmpeg.exe" -version >nul 2>&1
if errorlevel 1 (
    set "FAILED=%FAILED% ffmpeg"
    echo       [失败] 解压出来了但跑不起来
) else (
    echo       完成: %FFDIR%
)
:ff_ok
goto :eof


REM ── 5. Python 依赖 ───────────────────────────────────────────────────────
:step_pip
echo [5/6] Python 依赖^(清华镜像^)
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    set "FAILED=%FAILED% pip"
    echo       [跳过] 没有可用的 python
    goto :eof
)
if not exist "%INSTALL_DIR%\requirements.txt" (
    set "FAILED=%FAILED% pip"
    echo       [跳过] 找不到 requirements.txt，仓库没拉下来
    goto :eof
)
REM 把镜像写进 pip 配置，以后手动 pip install 也走镜像
"%PY%" -m pip config set global.index-url "%PIP_MIRROR%" >nul 2>&1
"%PY%" -m pip config set install.trusted-host "%PIP_HOST%" >nul 2>&1
"%PY%" -m pip install --upgrade pip -i "%PIP_MIRROR%"
"%PY%" -m pip install -r "%INSTALL_DIR%\requirements.txt" -i "%PIP_MIRROR%"
if errorlevel 1 (
    set "FAILED=%FAILED% pip"
    echo       [失败] 装依赖出错
) else (
    echo       完成
)
goto :eof


REM ── 6. 电源设置 ──────────────────────────────────────────────────────────
REM 锁屏不影响录制，但睡眠会：一睡摄像头和蓝牙全断，那一晚就没了。
REM 显示器该关还是关，只是别让系统睡。
:step_power
echo [6/6] 电源设置
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0
powercfg /change monitor-timeout-ac 10
REM USB 选择性暂停：开着的话摄像头可能被系统挂起
powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
powercfg /setactive SCHEME_CURRENT
echo       睡眠/休眠已关，USB 选择性暂停已关

REM 设备管理器里那个「允许计算机关闭此设备以节约电源」，只能走 WMI 改。
REM 蓝牙适配器被这么关掉过的表现是：扫不到任何设备，重启才好。
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$n=0;" ^
  "Get-CimInstance -Namespace root/wmi -ClassName MSPower_DeviceEnable -ErrorAction SilentlyContinue ^| ForEach-Object {" ^
  "  if ($_.InstanceName -match 'USB' -or $_.InstanceName -match 'BTH') {" ^
  "    try { Set-CimInstance -InputObject $_ -Property @{Enable=$false} -ErrorAction Stop; $n++ } catch {}" ^
  "  } };" ^
  "Write-Host ('      已关闭 ' + $n + ' 个 USB/蓝牙设备的省电开关')"
echo       完成
goto :eof
