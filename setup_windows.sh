#!/bin/bash
# 装采集环境的主体部分（miniconda / ffmpeg / pip 依赖 / 电源设置）。
#
# 用法（Git Bash，仓库根目录）：
#   ./setup_windows.sh                  装缺的，装过的跳过
#   ./setup_windows.sh --reinstall      推倒重装 miniconda 和 ffmpeg
#   ./setup_windows.sh --reinstall-conda
#   ./setup_windows.sh --reinstall-ffmpeg
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

# 装出来的东西全放在仓库下的 .tools/：
#   .tools/cache/     下载的安装包和压缩包（下次重跑不用再下一遍）
#   .tools/miniconda3/
#   .tools/ffmpeg/
#
# 为什么收进仓库：一台机器上就这一份，删掉仓库等于卸载干净，不会在
# %USERPROFILE% 和 %LOCALAPPDATA% 各留一坨没人记得的东西。更实际的是缓存——
# ffmpeg 那个包 106MB，网慢的时候要十几分钟，下到一半失败重跑又从头来，
# 有了 cache 就能接着用。
#
# （早先 ffmpeg 放在仓库外，是因为那时 .bat 先装 ffmpeg 再 clone，仓库目录
# 非空会让 git clone 直接失败。现在 clone 挪到了 .bat 里、在这个脚本之前，
# 这个顾虑没有了。）
TOOLS="$INSTALL_DIR/.tools"
CACHE="$TOOLS/cache"
FFDIR="$TOOLS/ffmpeg"
FFDIR_U="$FFDIR"
CONDA_ROOT="$TOOLS/miniconda3"

# Miniconda 走官方源：就一个安装包，下一次的事，实测速度够用。
CONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe"
# pip 必须走镜像：直连 PyPI 装 opencv/numpy/scipy 这几个大包经常超时，
# 而且以后每次装包都要走，不是一次性的。
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_HOST="pypi.tuna.tsinghua.edu.cn"

# 重装：把装出来的目录删掉，让下面的检测走到"没装"那条路。
# 只删装出来的东西，不碰 cache/ 和手动拷进来的安装包——重装的人要的是重新装，
# 不是重新下 106MB。
#
# miniconda 尤其必须先删干净：它的安装器装进已存在的非空目录会出问题，
# 而脚本又是"看到 python.exe 就跳过"，不删的话重装根本走不到安装那一步。
RE_CONDA=""; RE_FF=""
for a in "$@"; do
    case "$a" in
        --reinstall)         RE_CONDA=1; RE_FF=1 ;;
        --reinstall-conda)   RE_CONDA=1 ;;
        --reinstall-ffmpeg)  RE_FF=1 ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}"
            exit 0 ;;
        *)
            echo "不认识的参数：$a"
            echo "可用：--reinstall / --reinstall-conda / --reinstall-ffmpeg"
            exit 1 ;;
    esac
done

mkdir -p "$CACHE"

if [ -n "$RE_CONDA" ] && [ -d "$CONDA_ROOT" ]; then
    echo "[重装] 删掉 $CONDA_ROOT"
    rm -rf "$CONDA_ROOT"
fi
if [ -n "$RE_FF" ] && [ -d "$FFDIR" ]; then
    echo "[重装] 删掉 $FFDIR"
    rm -rf "$FFDIR"
fi

FAILED=()
PY=""

fail() { FAILED+=("$1"); echo "      [失败] $2"; }

# 找一个已经放在本地的安装包，找到就打印路径、返回 0。
# 先翻 .tools/ 再翻 .tools/cache/：人手动拷进来时自然会丢在 .tools/ 下，
# 没道理逼着按脚本的目录规矩摆。文件名用通配匹配——官方包名带版本号
# （ffmpeg-8.1.1-essentials_build.zip），不可能猜得准。
find_local() {  # find_local <glob> ...
    local g
    for g in "$@"; do
        local hit
        hit="$(ls -1 $g 2>/dev/null | head -1)"
        if [ -n "$hit" ] && [ -s "$hit" ]; then
            echo "$hit"
            return 0
        fi
    done
    return 1
}

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
elif [ -z "$RE_CONDA" ] && [ -x "$HOME/miniconda3/python.exe" ]; then
    # 这台机器以前按老办法装过（装在用户目录下），继续用它，不重复装一份。
    # --reinstall 时跳过这两条：说了要重装，就该真的装一份新的到仓库里，
    # 而不是又回去用外面那份旧的
    CONDA_ROOT="$HOME/miniconda3"
    echo "      已安装: $CONDA_ROOT"
elif [ -z "$RE_CONDA" ] && [ -x "${PROGRAMDATA:-/c/ProgramData}/miniconda3/python.exe" ]; then
    CONDA_ROOT="${PROGRAMDATA:-/c/ProgramData}/miniconda3"
    echo "      已安装: $CONDA_ROOT"
else
    # 本地已经有安装包就直接用，别再下一遍。自己下的会落在 cache/miniconda.exe，
    # 手动拷进来的多半是官方原名 Miniconda3-*.exe、丢在 .tools/ 下
    CONDA_EXE="$(find_local \
        "$TOOLS/Miniconda3-*.exe" "$TOOLS/miniconda*.exe" \
        "$CACHE/Miniconda3-*.exe" "$CACHE/miniconda.exe")" || CONDA_EXE=""
    if [ -n "$CONDA_EXE" ]; then
        echo "      用本地安装包（$CONDA_EXE）"
    else
        echo "      本地没有，从官方源下载..."
        # -C - 断点续传：网断了重跑能接着下
        curl -fL --retry 3 -C - -o "$CACHE/miniconda.exe" "$CONDA_URL" \
            && CONDA_EXE="$CACHE/miniconda.exe"
    fi
    if [ -n "$CONDA_EXE" ] && [ -s "$CONDA_EXE" ]; then
        tried_conda_install=1
        CONDA_WIN="$(cygpath -w "$CONDA_ROOT")"
        echo "      静默安装到 $CONDA_WIN ..."
        # 官方文档（Advanced install → Silent mode）规定的用法：
        #   /InstallationType=[JustMe|AllUsers]   默认 JustMe
        #   /AddToPath=[0|1]                      默认 0
        #   /RegisterPython=[0|1]                 默认 0
        #   /S                                    静默
        #   /D=<path>   必须是最后一个参数、不能加引号、静默安装时必填
        #   所有参数大小写敏感
        #
        # /AddToPath 必须是 0（官方默认也是 0，示例里压根没传）。这份 conda 装在
        # 仓库里，跟着仓库走——写进用户 PATH 的话，仓库一挪一删，PATH 就指向空目录，
        # 而且会悄悄接管这台机器上所有命令行的 python。脚本自己 export PATH 给
        # 本进程用就够了。
        #
        # 直接调 exe，不套 cmd //c start //wait：MSYS_NO_PATHCONV=1 会连 // 开头
        # 的参数一起放过，//wait 原样传给 cmd，cmd 不认、后面参数整体串位——
        # 之前现场报的 'egisterPython' is not recognized 就是这么来的。
        # 从 bash 直接执行本来就会等进程退出。
        if [ "$CONDA_WIN" != "${CONDA_WIN// /}" ]; then
            # /D 不能加引号，而 MSYS 给带空格的参数自动补引号——两条撞在一起，
            # NSIS 会收到带引号的路径然后装到别处去。与其装错不如不装。
            fail Miniconda "安装路径里有空格（$CONDA_WIN），NSIS 的 /D 不支持"
            echo "             把仓库挪到没有空格的路径下再跑，例如 C:\\wit\\witmotion_imu"
        else
            MSYS_NO_PATHCONV=1 "$CONDA_EXE" \
                /InstallationType=JustMe /AddToPath=0 /RegisterPython=0 /S \
                /D=$CONDA_WIN
        fi
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
# 这次确实跑了安装器、却没装出 python.exe，说明缓存那个包是坏的（下了一半 /
# 下错了）。删掉，下次重跑会重新下载——不删的话每次都"用缓存的安装包"、
# 每次都装不出来，永远卡在同一处。
#
# 判断条件挂在"装没装出来"上，不是挂在上面那个 fail 上：机器里本来就有
# python 时会走 [退让] 分支、不算失败，但那个坏包照样是坏的，也该清掉。
if [ -n "${tried_conda_install:-}" ] && [ ! -x "$CONDA_ROOT/python.exe" ]; then
    # 只删自己下的那份。手动拷进来的不动——那是人特意放的，删了等于把人家
    # 刚拷进来的东西吞掉，而且下次重跑又要重下一遍
    if [ "$CONDA_EXE" = "$CACHE/miniconda.exe" ]; then
        rm -f "$CACHE/miniconda.exe"
        echo "      已清掉下载的安装包，下次重跑会重新下载"
    else
        echo "      装不出来，但 $CONDA_EXE 是手动放的，没动它"
    fi
fi

# ── 2. ffmpeg ────────────────────────────────────────────────────────────
# 录像是把每一帧喂给 ffmpeg 管道写 VFR mp4 的，没有 ffmpeg 完全录不了视频。
# 下压缩包解压 + 写 PATH，不走安装器：出问题删掉目录重来即可，不会在系统里
# 留一堆卸载不干净的东西。
echo "[2/4] ffmpeg"
if [ -z "$RE_FF" ] && command -v ffmpeg >/dev/null 2>&1; then
    echo "      已安装: $(command -v ffmpeg)"
elif [ -x "$FFDIR_U/bin/ffmpeg.exe" ]; then
    echo "      已安装: $FFDIR"
    export PATH="$FFDIR_U/bin:$PATH"
else
    # 本地已经有压缩包就直接用。手动拷进来的多半是官方原名、带版本号
    # （ffmpeg-8.1.1-essentials_build.zip），所以按通配找，不能只认 ffmpeg.zip
    FF_ZIP="$(find_local \
        "$TOOLS/ffmpeg*.zip" "$CACHE/ffmpeg*.zip")" || FF_ZIP=""
    got=""
    if [ -n "$FF_ZIP" ]; then
        echo "      用本地压缩包（$FF_ZIP）"
        got=1
    fi
    [ -z "$got" ] && echo "      本地没有，下载压缩包..."
    [ -n "$got" ] || for url in \
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" \
        "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
    do
        echo "      试 $url"
        # --max-time 放到 40 分钟：现场实测 106MB 只跑到 100KB/s，预计要 16 分钟，
        # 原来卡 10 分钟必然半途而废，还白下一半。真卡死的情况用 --speed-time/-limit
        # 兜：连续 60 秒低于 10KB/s 才判失败，比一刀切的总时长合理。
        # -C - 断点续传：106MB 在慢网上很容易断，重跑能接着下而不是从头来
        if curl -fL --retry 2 -C - --max-time 2400 --speed-time 60 --speed-limit 10240 \
                -o "$CACHE/ffmpeg.zip" "$url"; then got=1; FF_ZIP="$CACHE/ffmpeg.zip"; break; fi
        echo "      这个源不行，换下一个"
    done
    if [ -n "$got" ]; then
        rm -rf "$CACHE/ffx"
        unzip_to "$FF_ZIP" "$CACHE/ffx" >/dev/null
        # 压缩包里是一层带版本号的目录，把它整个挪成 $FFDIR
        inner="$(find "$CACHE/ffx" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1)"
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
        # 同上：解压不出东西说明这个包是坏的。但只删自己下的那份，
        # 手动拷进来的不动
        if [ "$FF_ZIP" = "$CACHE/ffmpeg.zip" ] && [ -n "$got" ]; then
            rm -f "$CACHE/ffmpeg.zip"
            echo "             已清掉下载的压缩包，下次重跑会重新下载"
        elif [ -n "$FF_ZIP" ]; then
            echo "             $FF_ZIP 是手动放的，没动它；解压不出来的话检查一下这个包完不完整"
        fi
        echo "             手动办法：下 ffmpeg-release-essentials.zip 解压到 $FFDIR"
        echo "             解压后应该能看到 $FFDIR\\bin\\ffmpeg.exe"
    fi
fi
# 写进用户 PATH（不动系统 PATH，影响面小），新开的命令行才生效。
# 注意这条指向仓库里的 .tools/ffmpeg——仓库整个挪走或删掉，这条就失效了。
# 所以 record_multicam.sh 里也会自己找一次 .tools/ffmpeg/bin，不光靠 PATH。
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
