#!/bin/bash
# 装采集环境的主体部分（miniconda / ffmpeg / pip 依赖 / 电源设置）。
#
# 用法（Git Bash，仓库根目录）：
#   ./setup_windows.sh
#
# 新机器上没有 Git Bash，先跑 setup_windows.bat——它负责提权、装 Git、拉仓库，
# 然后回头调这个脚本。已经有 Git 的机器直接跑这个就行。
#
# 两条原则：
#   1) 可以反复跑。每一步先检测再装，装过的跳过。
#   2) 某一步失败不中断。后面照常走，最后统一列出哪几步没成——不然网络抖一下
#      就得从头再来，而且看不出到底卡在哪。
#
# 在 Git Bash（MSYS）里跑 Windows 程序有两个必须注意的地方，下面到处都在处理：
#   - MSYS 会把以 / 开头的参数当路径翻译：/S 会变成 C:/Program Files/Git/S，
#     安装器直接静默失败。凡是给 Windows 程序传 /开头的参数，都要
#     MSYS_NO_PATHCONV=1。
#   - Windows 程序不认 /c/Users/... 这种路径，要 cygpath -w 转成 C:\Users\...

set -uo pipefail   # 故意不开 -e：某一步失败要能继续走完后面的
cd "$(dirname "${BASH_SOURCE[0]}")"

INSTALL_DIR="$(pwd)"
# ffmpeg 装在仓库外面：装仓库里的话 git clone 会因为目标目录非空失败，
# 而且重新 clone 一次就得重下一遍
FFDIR="${LOCALAPPDATA:-$HOME/AppData/Local}/ffmpeg"
FFDIR_U="$(cygpath -u "$FFDIR" 2>/dev/null || echo "$FFDIR")"
CONDA_ROOT="$HOME/miniconda3"

# 国内直连 PyPI / Anaconda 慢到经常超时，默认走清华镜像
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_HOST="pypi.tuna.tsinghua.edu.cn"
CONDA_MIRROR="https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda"

TMPDIR_W="${TEMP:-$HOME}/wit_setup"
TMP="$(cygpath -u "$TMPDIR_W" 2>/dev/null || echo "$TMPDIR_W")"
mkdir -p "$TMP"

FAILED=()
PY=""

fail() { FAILED+=("$1"); echo "      [失败] $2"; }

# 解压统一走 PowerShell 的 Expand-Archive：Git for Windows 不一定带 unzip，
# 而 MSYS 的 GNU tar 不认 zip
unzip_to() {  # unzip_to <zip> <目标目录>
    powershell -NoProfile -ExecutionPolicy Bypass -Command \
      "Expand-Archive -Path '$(cygpath -w "$1")' -DestinationPath '$(cygpath -w "$2")' -Force" 2>&1
}

echo
echo "============================================================"
echo "  witmotion 采集环境安装"
echo "  仓库: $INSTALL_DIR"
echo "  ffmpeg: $FFDIR"
echo "============================================================"
echo

if ! net session >/dev/null 2>&1; then
    echo "[提醒] 当前不是管理员。电源设置那一步会失败。"
    echo "       要么用 setup_windows.bat 启动，要么以管理员身份打开 Git Bash 再跑。"
    echo
fi

# ── 1. Miniconda ─────────────────────────────────────────────────────────
echo "[1/4] Miniconda"
if [ -x "$CONDA_ROOT/python.exe" ]; then
    echo "      已安装: $CONDA_ROOT"
elif [ -x "${PROGRAMDATA:-/c/ProgramData}/miniconda3/python.exe" ]; then
    CONDA_ROOT="${PROGRAMDATA:-/c/ProgramData}/miniconda3"
    echo "      已安装: $CONDA_ROOT"
else
    echo "      从清华镜像下载安装包..."
    if curl -fL --retry 3 -o "$TMP/miniconda.exe" \
         "$CONDA_MIRROR/Miniconda3-latest-Windows-x86_64.exe"; then
        echo "      静默安装到 $(cygpath -w "$CONDA_ROOT") ..."
        # 直接调 exe，不要套 cmd //c start //wait：
        # MSYS_NO_PATHCONV=1 会连 // 开头的参数一起放过，于是 //wait 原样传给 cmd，
        # cmd 不认，后面的参数跟着串位——现场报的是 'egisterPython' is not
        # recognized，正是 /RegisterPython 被啃掉了开头的 /R。
        # 而且从 bash 直接执行本来就会等进程退出，不需要 start //wait。
        #
        # /D 必须放最后、不能加引号（NSIS 的硬性要求），路径还得是 Windows 风格；
        # MSYS_NO_PATHCONV=1 是为了 /InstallationType 这些不被当成路径翻译。
        MSYS_NO_PATHCONV=1 "$TMP/miniconda.exe" \
            /InstallationType=JustMe /AddToPath=1 /RegisterPython=0 /S \
            /D="$(cygpath -w "$CONDA_ROOT")"
    else
        fail Miniconda "下载不下来"
    fi
fi
if [ -x "$CONDA_ROOT/python.exe" ]; then
    PY="$CONDA_ROOT/python.exe"
    export PATH="$CONDA_ROOT:$CONDA_ROOT/Scripts:$PATH"
    echo "      完成"
elif command -v python >/dev/null 2>&1; then
    PY="python"
    echo "      [退让] 用系统里已有的 python"
else
    fail Miniconda "装完找不到 python.exe"
fi

# ── 2. ffmpeg ────────────────────────────────────────────────────────────
# 录像是把每一帧喂给 ffmpeg 管道写 VFR mp4 的，没有 ffmpeg 完全录不了视频。
# 下压缩包解压 + 写 PATH，不走安装器：出问题删掉目录重来即可，不会在系统里
# 留一堆卸载不干净的东西。
echo "[2/4] ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
    echo "      已安装: $(command -v ffmpeg)"
elif [ -x "$FFDIR_U/bin/ffmpeg.exe" ]; then
    echo "      已安装: $FFDIR"
    export PATH="$FFDIR_U/bin:$PATH"
else
    echo "      下载压缩包..."
    got=""
    for url in \
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" \
        "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
    do
        echo "      试 $url"
        # --max-time 放到 40 分钟：现场实测 106MB 只跑到 100KB/s，预计要 16 分钟，
        # 原来卡 10 分钟必然半途而废，还白下一半。真卡死的情况用 --speed-time/-limit
        # 兜：连续 60 秒低于 10KB/s 才判失败，比一刀切的总时长合理。
        if curl -fL --retry 2 --max-time 2400 --speed-time 60 --speed-limit 10240 \
                -o "$TMP/ffmpeg.zip" "$url"; then got=1; break; fi
        echo "      这个源不行，换下一个"
    done
    if [ -n "$got" ]; then
        rm -rf "$TMP/ffx"
        unzip_to "$TMP/ffmpeg.zip" "$TMP/ffx" >/dev/null
        # 压缩包里是一层带版本号的目录，把它整个挪成 $FFDIR
        inner="$(find "$TMP/ffx" -mindepth 1 -maxdepth 1 -type d | head -1)"
        if [ -n "$inner" ]; then
            rm -rf "$FFDIR_U"
            mkdir -p "$(dirname "$FFDIR_U")"
            mv "$inner" "$FFDIR_U"
        fi
    fi
    if [ -x "$FFDIR_U/bin/ffmpeg.exe" ]; then
        export PATH="$FFDIR_U/bin:$PATH"
        echo "      完成: $FFDIR"
    else
        fail ffmpeg "没装上。没有 ffmpeg 录不了视频，必须补上"
        echo "             手动办法：下 ffmpeg-release-essentials.zip 解压到 $FFDIR"
        echo "             解压后应该能看到 $FFDIR\\bin\\ffmpeg.exe"
    fi
fi
# 写进用户 PATH（不动系统 PATH，影响面小），新开的命令行才生效
if [ -x "$FFDIR_U/bin/ffmpeg.exe" ]; then
    powershell -NoProfile -ExecutionPolicy Bypass -Command \
      "\$p=[Environment]::GetEnvironmentVariable('Path','User'); if (\$null -eq \$p) { \$p='' };
       \$add='$(cygpath -w "$FFDIR_U/bin")';
       if (\$p -notlike ('*'+\$add+'*')) {
         \$new = if (\$p -eq '') { \$add } else { \$p.TrimEnd(';') + ';' + \$add };
         [Environment]::SetEnvironmentVariable('Path', \$new, 'User');
         Write-Host '      已写入用户 PATH'
       } else { Write-Host '      用户 PATH 里已经有了' }"
fi

# ── 3. Python 依赖 ───────────────────────────────────────────────────────
echo "[3/4] Python 依赖（清华镜像）"
if [ -z "$PY" ]; then
    fail pip "没有可用的 python"
elif [ ! -f "$INSTALL_DIR/requirements.txt" ]; then
    fail pip "找不到 requirements.txt"
else
    # 把镜像写进 pip 配置，以后手动 pip install 也走镜像
    "$PY" -m pip config set global.index-url "$PIP_MIRROR" >/dev/null 2>&1
    "$PY" -m pip config set install.trusted-host "$PIP_HOST" >/dev/null 2>&1
    "$PY" -m pip install --upgrade pip -i "$PIP_MIRROR"
    if "$PY" -m pip install -r "$INSTALL_DIR/requirements.txt" -i "$PIP_MIRROR"; then
        echo "      完成"
    else
        fail pip "装依赖出错"
    fi
fi

# ── 4. 电源设置 ──────────────────────────────────────────────────────────
# 锁屏不影响录制，但睡眠会：一睡摄像头和蓝牙全断，那一晚就没了。
# 显示器该关还是关，只是别让系统睡。
echo "[4/4] 电源设置"
if net session >/dev/null 2>&1; then
    # MSYS_NO_PATHCONV：不然 /change 会被翻译成路径
    MSYS_NO_PATHCONV=1 powercfg /change standby-timeout-ac 0
    MSYS_NO_PATHCONV=1 powercfg /change hibernate-timeout-ac 0
    MSYS_NO_PATHCONV=1 powercfg /change disk-timeout-ac 0
    MSYS_NO_PATHCONV=1 powercfg /change monitor-timeout-ac 10
    # USB 选择性暂停：开着的话摄像头可能被系统挂起
    MSYS_NO_PATHCONV=1 powercfg /setacvalueindex SCHEME_CURRENT \
        2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
    MSYS_NO_PATHCONV=1 powercfg /setactive SCHEME_CURRENT
    echo "      睡眠/休眠已关，USB 选择性暂停已关"
    # 设备管理器里那个「允许计算机关闭此设备以节约电源」只能走 WMI 改。
    # 蓝牙适配器被这么关掉过的表现是：扫不到任何设备，重启才好。
    powershell -NoProfile -ExecutionPolicy Bypass -Command \
      "\$n=0;
       Get-CimInstance -Namespace root/wmi -ClassName MSPower_DeviceEnable -ErrorAction SilentlyContinue |
         ForEach-Object {
           if (\$_.InstanceName -match 'USB' -or \$_.InstanceName -match 'BTH') {
             try { Set-CimInstance -InputObject \$_ -Property @{Enable=\$false} -ErrorAction Stop; \$n++ } catch {}
           } };
       Write-Host ('      已关闭 ' + \$n + ' 个 USB/蓝牙设备的省电开关')"
    echo "      完成"
else
    fail 电源设置 "不是管理员，跳过。用 setup_windows.bat 启动，或以管理员身份开 Git Bash"
fi

# ── 收尾 ─────────────────────────────────────────────────────────────────
echo
echo "============================================================"
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "  装完了，但这几步没成功："
    for f in "${FAILED[@]}"; do echo "    - $f"; done
    echo
    echo "  把上面的报错发给开发，或者手动装这几项再跑一次。"
else
    echo "  全部完成。"
fi
echo "============================================================"
cat <<EOF

还差最后一步，只能人来做：填场地配置

  1. 拿到每个 IMU 设备的 MAC:
       cd "$INSTALL_DIR"
       python wit_ble_live.py --scan

  2. 把 MAC、狗名、摄像头路数填进 sites/狗场.env
     （照着 sites/影棚.env 的写法，设备一律用 MAC 不用名字）

  3. 试录:
       SITE=狗场 ./record_multicam.sh

  4. 确认无误后注册每日归档任务（每天 00:05 自动传 NAS）:
       schtasks //Create //TN "IMU每日归档" //TR "$(cygpath -w "$INSTALL_DIR")\\daily_archive.bat" //SC DAILY //ST 00:05 //RL HIGHEST //F

注意：PATH 是这次装的，新开的命令行才生效，当前这个窗口不用管。
EOF
