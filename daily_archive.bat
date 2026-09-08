@echo off
REM Windows 任务计划的入口：调 Git Bash 跑 daily_archive.sh。
REM
REM 注册成每天 00:05 自动跑（管理员 CMD 里执行一次，路径按实际改）：
REM   schtasks /Create /TN "IMU每日归档" /TR "C:\Users\user\Downloads\witmotion_imu\daily_archive.bat" /SC DAILY /ST 00:05 /RL HIGHEST /F
REM
REM 想立刻试一次：  schtasks /Run /TN "IMU每日归档"
REM 看有没有在跑：  schtasks /Query /TN "IMU每日归档"
REM 删掉：          schtasks /Delete /TN "IMU每日归档" /F
REM
REM 00:05 是刚过零点、录制已经换到新一天的目录、昨天最后一个整点片段也落盘了的
REM 时间点。脚本自己还会再确认一遍"这一天确实不写了"才动手。

setlocal
cd /d "%~dp0"

REM 找 Git Bash：标准安装位置，装在别处就改这里
set "BASH=C:\Program Files\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=C:\Program Files (x86)\Git\bin\bash.exe"
if not exist "%BASH%" (
    echo 找不到 Git Bash，请把 BASH 改成实际路径
    exit /b 1
)

"%BASH%" -lc "./daily_archive.sh"
REM 永远返回 0：归档失败不该让任务计划报红，状态看 logs\daily_archive_*.log
exit /b 0
