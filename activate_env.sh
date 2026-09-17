#!/bin/bash
# 把仓库自带的 python / ffmpeg 挂到 PATH 上。**用 source 调，不要直接执行**：
#
#     source ./activate_env.sh
#     python check_device_worn.py --imu wit=... --duration 8
#
# 为什么需要这个：setup_windows.sh 把 miniconda 装在仓库的 .tools/miniconda3
# 下，而且用的是 /AddToPath=0（官方默认，也是对的——那份 conda 跟着仓库走，
# 写进用户 PATH 的话仓库一挪一删 PATH 就指向空目录，还会悄悄接管这台机器上
# 所有命令行的 python）。
#
# 代价是：**新开一个 Git Bash 敲 python / conda / pip 都是 command not found**，
# 而这看起来非常像"conda 没装好"。装好了，只是故意不在 PATH 上。
#
# record_multicam.sh 自己会做这件事，所以录制不受影响；需要手敲 python 的那些
# 小工具（check_device_worn.py、wit_ble_live.py --scan 等）就 source 一下这个。
#
# 已经有 python / ffmpeg 的机器（比如自己装了 Anaconda 并加进了 PATH）不会被
# 覆盖——只在找不到的时候才补。

_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -x "$_repo/.tools/ffmpeg/bin/ffmpeg.exe" ] && ! command -v ffmpeg >/dev/null 2>&1; then
    export PATH="$_repo/.tools/ffmpeg/bin:$PATH"
fi

if [ -x "$_repo/.tools/miniconda3/python.exe" ] && ! command -v python >/dev/null 2>&1; then
    export PATH="$_repo/.tools/miniconda3:$_repo/.tools/miniconda3/Scripts:$PATH"
fi

unset _repo
