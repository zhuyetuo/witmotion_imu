#!/bin/bash
# 正式长期录制：N个摄像头 + N个IMU设备，1080p原生采集缩放到720p输出，
# 按整点自动切分文件，循环录制直到手动停止。
#
# 用法:
#   SITE=影棚 ./record_multicam.sh     ← 平时就用这个
#   SITE=狗场 ./record_multicam.sh
#
#   PREVIEW=1 SITE=狗场 ./record_multicam.sh   ← 起手就开预览窗口（默认不开）
#   DEBUG=1   SITE=狗场 ./record_multicam.sh   ← 只看画面，什么都不存
#
# 命令行上多写的参数会原样传给 imu_camera_sync_multicam.py，接在 EXTRA_ARGS 后面
# （所以能盖过场地配置里的同名开关）：
#   SITE=影棚 ./record_multicam.sh --no-precheck
#   SITE=影棚 ./record_multicam.sh --profile --scan-timeout 20
#
# 这里以前是不传的——脚本从头到尾没用过 "$@"，命令行上写的参数被安静吞掉，
# 你以为加了 --no-precheck，预检照样把你拦下来，还看不出为什么。
#
# SITE 会去读 sites/<名字>.env，那里写死了这个场地的设备 MAC、狗名、摄像头路数。
# 为什么要有它：这个脚本原来的默认值是 IMUS="wit=WT901BLE68 wit=WTSDCL"、
# CAMS="0 1"，那是很早以前两个出厂名设备加两个摄像头时留下的，现在影棚是 8 个
# 设备 3 路摄像头、狗场 12 个设备 6 路摄像头，两个场地也不一样——一套默认值
# 不可能同时对。而这些默认值错了不会报错，只会安静地录一整天空数据。
# 所以默认值撤掉，配置按场地放进文件里，谁改了都能在 git 里看见。
#
# 不用 SITE 也行，环境变量照旧（调试/临时用）：
#   ./record_multicam.sh
#   （不想改这个文件的话，也可以用环境变量覆盖，比如:
#    OUT_DIR=data/multicam_multiimu2 CAM_FPS=30 ./record_multicam.sh）
#
# IMU设备用 IMUS 环境变量传（空格分隔，每个是"类型=标识"，比如 wit=WT3 或
# wit=XX:XX:XX:XX:XX:01），几个都行，不限于2个：
#   IMUS="wit=WT3 wit=WT4 wit=WT5" ./record_multicam.sh
# 摄像头同理用 CAMS（空格分隔的编号）：
#   CAMS="0 1" ./record_multicam.sh
#
# 降采样怎么处理，用 RESAMPLE_MODE 控制：
#   none（默认）：只保留原始文件，不生成任何降采样配对文件（--no-resample）
#   both        ：原始文件和降采样配对文件都保留（不传 --resample-only 也不传 --no-resample）
#   only        ：只保留降采样后的 camX_imuY_resampled{HZ}hz.mp4/.csv 配对文件，
#                 **当场删掉**原始的 {base}_camN.mp4/.csv/_meta.csv/_{imu}_raw.csv
#
# 默认从 only 改成了 none。only 是不可逆的：原始流水在每段录完时就删在本地，
# 早于归档上传，NAS 上和暂存目录里都不会有（暂存是硬链接，链的就是降采样版）。
# 而降采样回不去——16Hz 的奈奎斯特频率是 8Hz，8Hz 以上的成分存盘那一刻就没了，
# 插值只能把点变密，不会把信息变回来。抓挠的判据恰好是陀螺仪 4–8Hz 的能量占比，
# 正卡在这个频带的上沿，谐波全丢。
#
# 硬盘便宜，重录一遍那几个月不可能。要省地方就用 both，或者事后再降采样
# （resample_csv_hz.py），别在采集这一步就把原件删了。
#
# 画面上想显示狗狗名字而不是 imu1/imu2 这种编号，用 DOG_NAMES 传（空格分隔，
# 顺序要跟 IMUS 一一对应，第几个名字对应第几个IMU），比如：
#   IMUS="wit=WT1 wit=WT5 wit=WT4 wit=WT8" DOG_NAMES="bibi bali titi lulu" ./record_multicam.sh
# 换了狗、换了设备组合，下次直接改这两个环境变量就行，不用改脚本本身；
# 只影响画面显示，不影响文件名（文件名还是固定用 imu1/imu2...）。

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# setup_windows.sh 把 ffmpeg 装在仓库的 .tools/ffmpeg 下。它虽然也往用户 PATH
# 里写了一条，但那条是"新开的命令行才生效"，而且仓库一挪位置就失效。这里自己
# 找一次，录制就不依赖 PATH 配没配对——没有 ffmpeg 是完全录不了视频的，
# 不值得为这个再排查一轮环境变量。
if [ -x ".tools/ffmpeg/bin/ffmpeg.exe" ] && ! command -v ffmpeg >/dev/null 2>&1; then
    export PATH="$(pwd)/.tools/ffmpeg/bin:$PATH"
fi

# 同理找 python。setup_windows.sh 装 miniconda 时用的是 /AddToPath=0（官方默认，
# 也是对的：那份 conda 装在仓库里、跟着仓库走，写进用户 PATH 的话仓库一挪一删
# PATH 就指向空目录，还会悄悄接管这台机器上所有命令行的 python）。
# 代价是新开的终端里 python 不在 PATH 上，而下面就是直接调 python 的——
# 所以这里自己找一次。
if [ -x ".tools/miniconda3/python.exe" ] && ! command -v python >/dev/null 2>&1; then
    export PATH="$(pwd)/.tools/miniconda3:$(pwd)/.tools/miniconda3/Scripts:$PATH"
fi

# 先读场地配置，再让环境变量覆盖它——命令行上临时改一项不用去动文件
# 场地名：命令行没给就读 sites/.current。
#
# 为什么要这个文件：每日归档是计划任务跑的，没法每次手敲 SITE=狗场；而
# daily_archive.bat 是仓库里的文件，在它里面写死场地名的话，每台机器都要改一次，
# 而且 git pull 每次都冲突。.current 是每台机器自己的一行小文件、不进版本库，
# 配一次就完事，两个脚本都读它。
#
# 设置：  echo 狗场 > sites/.current
#
# tr 去掉 \r：这文件多半是在 Windows 上用记事本建的，带 CRLF，不去掉的话
# 场地名会变成 "狗场\r"，找不到 sites/狗场\r.env，报错还看不出哪儿不对。
SITE="${SITE:-}"

# 命令行上直接把设备写清楚了，就是手动跑，不去读 sites/.current。
#
# 这条是修一个回归。老的稳定用法是纯环境变量、不带 SITE：
#   IMUS="wit=WT6 wit=WT5 wit=WT1 wit=WT4" DOG_NAMES="Bibi Bali lulu Xima" \
#     RESAMPLE_MODE=none ./record_multicam.sh
# 它一直好好的。后来我把 CAMS 的默认值 "0 1" 删了（#137，理由是场地配置错了
# 会安静录一整天废数据），于是这条命令没了 CAMS，退回去读 sites/.current，
# 拿到影棚的 CAMS="0 1 2" 去开第三路摄像头——直接 Segmentation fault。
#
# 那条命令从头到尾没要过第三个摄像头。它把 IMUS、DOG_NAMES 都写死了，
# 意思很明确就是"按我说的来"，不该被一个它没提过的场地文件接管。
# 显式优先于隐式：写了 IMUS 就当手动跑，.current 不生效（SITE= 照样管用）。
_MANUAL_RUN=0
if [ -n "${IMUS:-}" ] && [ -z "$SITE" ]; then
    _MANUAL_RUN=1
elif [ -z "$SITE" ] && [ -f "sites/.current" ]; then
    SITE="$(tr -d '\r\n ' < sites/.current)"
fi
# SITE 可以写 ASCII 别名（gouchang / yingpeng），跟中文场地名等价。
# 别名登记在场地文件自己的 SITE_ALIAS= 里，这里现扫，不写死对照表——写死的
# 清单跟文件内容早晚走岔（EXTRA_ARGS 那次就是）。
# 为什么需要：install_autostart.bat 是 .bat，cmd 按字节读 .bat，里面出现中文
# 早晚出乱子（setup_windows.bat 栽过一次）。有了别名，.bat 全程只碰 ASCII。
if [ -n "$SITE" ] && [ ! -f "sites/${SITE}.env" ]; then
    for _f in sites/*.env; do
        [ -f "$_f" ] || continue
        if grep -q "^[[:space:]]*SITE_ALIAS=[\"']\?${SITE}[\"']\?[[:space:]]*\$" "$_f"; then
            SITE="$(basename "$_f" .env)"
            break
        fi
    done
fi
if [ -n "$SITE" ]; then
    site_file="sites/${SITE}.env"
    if [ ! -f "$site_file" ]; then
        echo "找不到场地配置 $site_file。现有的："
        ls sites/*.env 2>/dev/null | sed 's|^|  |' || echo "  （一个都没有）"
        exit 1
    fi
    # 场地文件里是直接赋值（IMUS="..."），source 之后会盖掉命令行传进来的同名
    # 变量。想要的是反过来：文件当底、命令行临时覆盖。所以先存一份，source 完
    # 再放回去。
    #
    # 要存哪些变量，从文件里现读，不写死清单——写死过一次，结果 WARMUP_SEC、
    # CAPTURE_WIDTH 这些后来加进场地文件的项不在清单里，命令行传了也盖不上，
    # 而且完全没有提示。清单和文件内容早晚会走岔，让它自己去看就不会。
    _site_vars="$(grep -oE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=' "$site_file" \
                  | tr -d ' \t=' | sort -u)"
    for _v in $_site_vars; do
        eval "_saved_$_v=\${$_v:-}"
    done
    # shellcheck disable=SC1090
    . "$site_file"
    for _v in $_site_vars; do
        eval "_s=\$_saved_$_v"
        [ -n "$_s" ] && eval "$_v=\$_s"
    done
    echo "场地：$SITE（$site_file）"
fi

IMUS="${IMUS:-}"
DOG_NAMES="${DOG_NAMES:-}"
# 手动跑（命令行给了 IMUS、没给 SITE）时 CAMS 退回老默认值 "0 1"。
# 场地模式下仍然不给默认值——那边填错是安静录一整天废数据，宁可不启动。
if [ "$_MANUAL_RUN" = "1" ] && [ -z "${CAMS:-}" ]; then
    CAMS="0 1"
    echo "手动模式（没传 SITE）：摄像头用默认的 CAMS=\"$CAMS\"，要几路自己传 CAMS=\"0 1 2\""
fi
CAMS="${CAMS:-}"

# 没有默认值可以退：这两项填错不会报错，只会安静地录一整天废数据，
# 所以宁可不启动
if [ -z "$IMUS" ] && [ -z "${DEVICES:-}" ]; then
    echo "没有指定场地。用："
    echo "    SITE=狗场 ./record_multicam.sh"
    echo "  归档同理：SITE=狗场 ./daily_archive.sh"
    echo
    echo "  （也可以把场地记在这台机器上，之后不用每次传：echo 狗场 > sites/.current"
    echo "    命令行传的 SITE= 优先级更高，随时能盖过它）"
    echo "现有场地：$(ls sites/*.env 2>/dev/null | sed 's|sites/||;s|\.env||' | paste -sd' ')"
    echo "拿设备 MAC：python wit_ble_live.py --scan"
    exit 1
fi
if [ -z "$CAMS" ]; then
    echo "没有配置摄像头。用 SITE=... 或者传 CAMS=\"0 1 2\"。"
    exit 1
fi
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
CAPTURE_WIDTH="${CAPTURE_WIDTH:-1920}"
CAPTURE_HEIGHT="${CAPTURE_HEIGHT:-1080}"
RESAMPLE_HZ="${RESAMPLE_HZ:-16}"
CAM_FPS="${CAM_FPS:-25}"
WARMUP_SEC="${WARMUP_SEC:-10}"
OUT_DIR="${OUT_DIR:-data/multicam_multiimu}"

RESAMPLE_MODE="${RESAMPLE_MODE:-none}"
# 设备一直连不上时重连间隔的封顶秒数（指数退避2→4→8→...封顶这个值），默认
# 300秒（5分钟）；长时间无人值守录制建议保持默认或调更高，避免频繁反复扫描
# 把Windows蓝牙栈拖垮（症状：整个蓝牙适配器搜不到任何设备，得重启电脑）。
RECONNECT_MAX_BACKOFF="${RECONNECT_MAX_BACKOFF:-300}"


# DEVICES：一行一个设备，「真实编号 MAC 狗名」。优先于 IMUS/IMU_IDS/DOG_NAMES。
#
# 为什么换成一张表：那三个是平行数组，要靠人手工对齐——错位一格不会报错，
# 只是从此以后 A 狗的数据记在 B 狗名下。而"真实编号"这件事本来就该跟 MAC
# 写在同一行，分成三处填就是在制造错位的机会。
_parse_devices_block() {
    local _block="$1" _tag="$2"
    [ -n "$_block" ] || return 0
    while IFS= read -r _line; do
        # 先砍掉行尾注释，再切三列。不砍的话 read 会把 # 后面整段都塞进狗名，
        # 而狗名是要画到画面上的，出来就是一长串乱码
        _line="${_line%%#*}"
        # 用数组读，不要 set --：set -- 会把脚本自己的位置参数覆盖掉
        read -ra _f <<< "$_line"
        [ ${#_f[@]} -eq 0 ] && continue
        if [ ${#_f[@]} -ne 3 ]; then
            echo "${_tag} 这一行应该是「编号 MAC 狗名」三列，收到 ${#_f[@]} 列: $_line"
            exit 1
        fi
        IMU_IDS="$IMU_IDS ${_f[0]#imu}"
        IMUS="$IMUS wit=${_f[1]}"
        DOG_NAMES="$DOG_NAMES ${_f[2]}"
    done <<< "$_block"
}

# USE_STANDBY=1：这次用备用那组（DEVICES_STANDBY），不用改文件。
#
# 为什么要有：狗场是每天轮换充电，今天戴偶数、明天戴奇数。以前换班要把场地
# 文件里两个块的内容对调——十二行剪切粘贴，在狗场现场半夜干这个，早晚会漏，
# 而漏了的后果是脚本去连躺在充电座上的那一个，录一整夜静止数据，看起来还完全
# 正常（有数据、50Hz、时长对）。这个问题已经发生过一次了。
#
# 改成开关之后，场地文件从此不用因为换班而改动：
#     SITE=狗场 ./record_multicam.sh                # DEVICES 那组（奇数）
#     USE_STANDBY=1 SITE=狗场 ./record_multicam.sh  # DEVICES_STANDBY 那组（偶数）
#
# 跟 ALL_DEVICES=1 同时开没有意义——那个两组都录，本来就不用选。
if [ "${USE_STANDBY:-0}" = "1" ]; then
    if [ -z "${DEVICES_STANDBY:-}" ]; then
        echo "USE_STANDBY=1 但场地配置里没有 DEVICES_STANDBY，没有备用设备可用。"
        exit 1
    fi
    _swap="$DEVICES"; DEVICES="$DEVICES_STANDBY"; DEVICES_STANDBY="$_swap"
    if [ "${ALL_DEVICES:-0}" = "1" ]; then
        echo "USE_STANDBY=1 跟 ALL_DEVICES=1 一起用没有意义（两组都会录），忽略前者的影响"
    fi
fi

if [ -n "${DEVICES:-}" ]; then
    IMUS=""; IMU_IDS=""; DOG_NAMES=""
    _parse_devices_block "$DEVICES" "DEVICES"
    if [ "${USE_STANDBY:-0}" = "1" ] && [ "${ALL_DEVICES:-0}" != "1" ]; then
        echo "USE_STANDBY=1：这次录备用那组，imu$(echo $IMU_IDS | tr ' ' ',')"
    fi
fi

# 两个块合起来的「设备号 ↔ 狗名」对照。USE_STANDBY 换了组、ALL_DEVICES 两组都要，
# 都得知道"这个号是哪只狗的、那只狗还有哪个号"。
_dev_dog_map=""
_collect_dev_dog() {
    local _block="$1"
    [ -n "$_block" ] || return 0
    while IFS= read -r _line; do
        _line="${_line%%#*}"
        read -ra _f <<< "$_line"
        [ ${#_f[@]} -eq 3 ] || continue
        _dev_dog_map="$_dev_dog_map ${_f[0]#imu}:${_f[2]}"
    done <<< "$_block"
}
_collect_dev_dog "${DEVICES:-}"
_collect_dev_dog "${DEVICES_STANDBY:-}"

# PAIRS 写在场地文件里，写的是某一组的编号（狗场写的是奇数那组）。换组录的时候
# 这些编号一个都不存在，parse_pairs 直接报"配对不存在"退出——USE_STANDBY 第一版
# 就是这么炸的，而且是在 Python 里炸的，shell 这边看参数完全正常。
#
# 所以按狗名把 PAIRS 改写到这次真正在录的那个设备上。不换组时这是恒等变换
# （每条都已经在录），所以不用加条件，一视同仁地跑一遍更不容易漏。
if [ -n "${PAIRS:-}" ] && [ -n "$_dev_dog_map" ]; then
    _active=" $IMU_IDS "
    _remapped=""; _dropped=""
    for _pr in $PAIRS; do
        _cam="${_pr%%:*}"
        _n="${_pr##*:}"; _n="${_n#imu}"
        case "$_active" in
            *" $_n "*) _remapped="$_remapped ${_cam}:imu${_n}"; continue ;;
        esac
        _dog=""; _hit=""
        for _kv in $_dev_dog_map; do
            if [ "${_kv%%:*}" = "$_n" ]; then _dog="${_kv#*:}"; break; fi
        done
        if [ -n "$_dog" ]; then
            for _kv in $_dev_dog_map; do
                if [ "${_kv#*:}" = "$_dog" ]; then
                    case "$_active" in *" ${_kv%%:*} "*) _hit="${_kv%%:*}"; break ;; esac
                fi
            done
        fi
        if [ -n "$_hit" ]; then
            _remapped="$_remapped ${_cam}:imu${_hit}"
        else
            _dropped="$_dropped ${_cam}:imu${_n}"
        fi
    done
    if [ "$(echo $PAIRS)" != "$(echo $_remapped)" ]; then
        echo "  PAIRS 按狗名改写到这次在录的设备上（$(echo $_remapped | wc -w) 条）"
    fi
    PAIRS="$_remapped"
    if [ -n "$_dropped" ]; then
        echo "  ⚠ 这几条配对的设备这次没在录，已去掉：$_dropped"
    fi
fi

# ALL_DEVICES=1：当班 + 备用一起录（每只狗两个 IMU 同时采）。
#
# 解决的是这个问题：现场换了设备而配置没跟上，脚本就会去连躺在充电座上的
# 那一个，录一整夜静止数据——而且看起来完全正常（有数据、50Hz、时长对），
# 事后在平台上也看不出来。两个都录就没有"选错"这回事了，哪个是真的事后
# 看数据一眼就分得出来（充电座那份是纯静止），换班也不用再改配置。
#
# ⚠ 先测蓝牙：SITE=狗场 ./check_ble_capacity.sh
#   12 条 BLE 链路抢同一个射频的连接间隔，很可能撑不住，或者撑住了但每台的
#   采样率都掉下来——那是拿真正在乎的 6 台数据去换一个配置错误的保险，不划算。
if [ "${ALL_DEVICES:-0}" = "1" ]; then
    if [ -z "${DEVICES:-}" ]; then
        # 没有 DEVICES 表就意味着没有真实编号，文件名里的 imu 号是按 IMUS 的
        # 位置排的。把备用那组接在后面，它们会变成 imu5、imu6…——而平台狗档案
        # 里那些号登记的是别的狗，等于把 A 狗备用设备的数据记到 B 狗名下。
        # 这种错不报警、不可见，只能在这儿拦住。
        echo "ALL_DEVICES=1 需要场地配置里有 DEVICES 表（「真实编号 MAC 狗名」）。"
        echo "这个场地用的是 IMUS，文件名里的 imu 号是按位置排的；把备用设备接在"
        echo "后面会让它们拿到别的狗的编号，而且完全看不出来。"
        echo
        echo "要测蓝牙容量用 check_ble_capacity.sh —— 它不写文件，没有这个问题："
        echo "  SITE=${SITE:-<场地>} ./check_ble_capacity.sh"
        exit 1
    fi
    if [ -z "${DEVICES_STANDBY:-}" ]; then
        echo "ALL_DEVICES=1 但场地配置里没有 DEVICES_STANDBY，没有备用设备可加。"
        exit 1
    fi
    _parse_devices_block "$DEVICES_STANDBY" "DEVICES_STANDBY"
    _n_all=$(echo $IMU_IDS | wc -w)
    echo "ALL_DEVICES=1：当班 + 备用一起录，共 $_n_all 个设备（imu$(echo $IMU_IDS | tr ' ' ',')）"

    read -ra _ids <<< "$IMU_IDS"
    read -ra _dogs <<< "$DOG_NAMES"

    # 每只狗必须正好两个设备（当班一个、备用一个）。少一个或多一个都说明两个块
    # 没对齐，而这种错不会自己冒出来——只会把 A 狗的备用设备配到 B 狗的摄像头上，
    # 录出来的文件名看着完全正常。
    for _d in $(printf '%s\n' "${_dogs[@]}" | sort -u); do
        _cnt=$(printf '%s\n' "${_dogs[@]}" | grep -cx -- "$_d" || true)
        if [ "$_cnt" -ne 2 ]; then
            echo "  ✗ 狗名 \"$_d\" 在 DEVICES + DEVICES_STANDBY 里出现了 $_cnt 次，应该正好 2 次"
            echo "    （当班一个、备用一个）。两个块的狗名要能一一配上，否则配对会串。"
            exit 1
        fi
    done

    # 同一只狗的另一个设备号
    _alt_imu_of() {
        local want="$1" i dog=''
        for i in "${!_ids[@]}"; do
            if [ "${_ids[$i]}" = "$want" ]; then dog="${_dogs[$i]}"; break; fi
        done
        [ -n "$dog" ] || return 1
        for i in "${!_ids[@]}"; do
            if [ "${_dogs[$i]}" = "$dog" ] && [ "${_ids[$i]}" != "$want" ]; then
                echo "${_ids[$i]}"; return 0
            fi
        done
        return 1
    }

    # PAIRS 自动扩到备用设备：每条 camX:imuN 再配一条 camX:imuM（M = 同一只狗的
    # 另一个设备）。不这么做的话备用那几个只会留下 {base}_imuM_raw.csv，不配对、
    # 不成样本、不归档——全采了个寂寞。
    #
    # 按**狗名**找对应，不按行号：行号对齐错一格不会报错，只会把 A 狗的备用
    # 配到 B 狗的摄像头上。狗名对不上就在上面那个检查里已经退出了。
    #
    # 也不写死在场地文件里：parse_pairs 对不存在的设备是直接报错退出的，
    # 把 24 条配对常驻在 PAIRS 里，不带 ALL_DEVICES 跑就一条都跑不起来。
    if [ -n "${PAIRS:-}" ]; then
        _add=""
        for _pr in $PAIRS; do
            _cam="${_pr%%:*}"
            _n="${_pr##*:}"; _n="${_n#imu}"
            _alt="$(_alt_imu_of "$_n")" || continue
            _add="$_add ${_cam}:imu${_alt}"
        done
        PAIRS="$PAIRS$_add"
        echo "  PAIRS 自动扩到 $(echo $PAIRS | wc -w) 条（备用设备各自跟着当班那条走）"
        # 扩完还漏的话，说明 PAIRS 本来就没覆盖全部当班设备。喊一声，
        # 别让人以为"全采"了而其实有设备只落了个原始 CSV。
        for _id in $IMU_IDS; do
            case " $PAIRS " in
                *":imu${_id} "*|*":imu${_id}") ;;
                *) echo "  ⚠ PAIRS 里仍然没有 imu${_id}，它只会留下原始 CSV，不配对、不归档" ;;
            esac
        done
    fi

    echo "  ⚠ 平台那边会多出一倍样本：躺在充电座上的那几个也会被配对成样本，"
    echo "    数据是纯静止的。哪个是真的看数据一眼能分出来，但别忘了清掉。"
fi


imu_args=()
for spec in $IMUS; do
    imu_args+=(--imu "$spec")
done

# IMU_IDS：每个设备在文件名里用的全局编号，顺序跟 IMUS 一一对应。
# 不设就按位置排 imu1、imu2…——那样两个场地会撞：狗场的第一个设备叫 imu1，
# 影棚的第一个也叫 imu1，而平台是靠文件名里的 imu 号认是哪只狗的
# （狗档案登记的是全局唯一的 IMU1..IMU20）。撞了就会把一个场地的数据算到另一个
# 场地的狗身上，皮肤评估那张按（日期,imu,来源）唯一的表还会直接撞行写不进去。
IMU_IDS="${IMU_IDS:-}"
imu_label_args=()
for gid in $IMU_IDS; do
    imu_label_args+=(--imu-label "imu${gid#imu}")
done

# 没给真实编号就只能按位置排 imu1、imu2…，而位置是会漂的：今天 imu1 是 WT9，
# 明天顺序一改就成了别的设备，文件名却看不出任何差别。平台是按这个号认狗的，
# 所以这不是小事，喊一声。
if [ -z "$IMU_IDS" ]; then
    echo "⚠ 没有设 DEVICES/IMU_IDS，文件名里的 imu 号将按 IMUS 的位置排（imu1、imu2…）。"
    echo "  位置序号会漂：调换 IMUS 的顺序，同一个 imu1 就变成另一台设备，而文件名看不出来；"
    echo "  两个场地也会撞（各自都从 imu1 开始）。平台按这个号认是哪只狗。"
    echo "  正确做法：在 sites/<场地>.env 里用 DEVICES 写「真实编号 MAC 狗名」。"
    echo
fi

dog_name_args=()
for name in $DOG_NAMES; do
    dog_name_args+=(--dog-name "$name")
done

cam_args=()
for idx in $CAMS; do
    cam_args+=(--camera "$idx")
done

# PAIRS：只生成这些 cam x imu 配对。留空就是全排列（老行为）。
# 一间一狗一摄像头的场地（狗场）必须设：cam_i 和 imu_i 严格一一对应，全排列
# 出来大半是「A 房间的画面配 B 房间的狗」，纯废文件，而且每份都是一小时的
# 720p 视频拷贝，磁盘成倍烧。
# 一个大空间多只狗的场地（影棚）不要设：哪路摄像头拍到哪只狗事先不知道。
# EXTRA_ARGS：临时往底层脚本多传几个参数，不用为了试一个开关改脚本。
# 比如查帧率瓶颈：EXTRA_ARGS=--profile SITE=狗场 ./record_multicam.sh
# 故意不加引号展开（下面 $EXTRA_ARGS 按空格分词），这样能一次传多个。
EXTRA_ARGS="${EXTRA_ARGS:-}"

# EXTRA_ARGS_APPEND：在场地配置的 EXTRA_ARGS 后面再追加，而不是把它替换掉。
# 给 record_autostart.bat 用的——它要强制加 --no-preview（开机自动跑的时候
# 没人坐在那儿按 p，六个窗口白白吃掉四成帧率）。用 EXTRA_ARGS 的话，哪天往
# 场地配置里加了别的开关就会被它悄悄顶掉，而且是不报错的那种。
EXTRA_ARGS="$EXTRA_ARGS ${EXTRA_ARGS_APPEND:-}"

# PREVIEW：启动时开不开预览窗口。默认关（0）。
#   PREVIEW=1 SITE=狗场 ./record_multicam.sh    ← 起手就开着
#
# 为什么默认关：录制的常态是无人值守跑一整晚，没人看那些窗口，而七路 720p 的
# imshow 加 waitKey 实测吃掉每 tick 30 多毫秒（25fps 的预算一共才 40ms），
# 白白掉四成帧率。以前默认开是为了认摄像头，现在那件事有 ./debug_cameras.sh
# 专门管——它不落盘、随开随关，比在正式录制里开着画面合适得多。
#
# 跑起来之后随时能在终端敲 p + 回车 把画面调出来看一眼，看完再敲一次关掉，
# 所以"默认关"不等于"想看看不了"。
case "$(echo "${PREVIEW:-0}" | tr 'A-Z' 'a-z')" in
    0|no|off|false|n)  EXTRA_ARGS="$EXTRA_ARGS --no-preview"; _preview_say="关（默认；PREVIEW=1 起手就开）" ;;
    1|yes|on|true|y)   _preview_say="开（PREVIEW=1）" ;;
    *) echo "PREVIEW 只认 0/1（或 on/off、yes/no），收到: ${PREVIEW}"; exit 1 ;;
esac

# ROTATE：把某几路画面转过来，空格分隔，比如 ROTATE="cam7:180"。
# 天花板上倒装的那路要用——不转的话画面是反的，标注时判断方向更容易出错。
ROTATE="${ROTATE:-}"
rotate_args=()
for r in $ROTATE; do
    rotate_args+=(--rotate "$r")
done

PAIRS="${PAIRS:-}"
pair_args=()
for pr in $PAIRS; do
    pair_args+=(--pair "$pr")
done

case "$RESAMPLE_MODE" in
    only)
        # 唯一一个会删原始数据的选项，删了不可逆，所以吵一句再走
        echo "警告：RESAMPLE_MODE=only 会在每段录完后删掉原始 ${RESAMPLE_HZ}Hz 之上的流水文件，不可恢复。"
        echo "      想同时留原始和降采样版用 RESAMPLE_MODE=both。5 秒后继续，要停按 Ctrl-C。"
        sleep 5
        resample_flag=(--resample-only)
        ;;
    none) resample_flag=(--no-resample) ;;
    both) resample_flag=() ;;
    *) echo "RESAMPLE_MODE 只能是 only/none/both，收到的是: $RESAMPLE_MODE"; exit 1 ;;
esac

# DURATION：只录这么多秒然后正常结束（用来验证，跑完会走完整的收尾流程：
# 对齐校验、生成配对文件、ffmpeg 正常写完索引）。
# 不设就是默认的"按整点切分、一直循环录"。
#   DURATION=60 SITE=狗场 ./record_multicam.sh
# DAY_SUFFIX：按天分的目录名后缀，2026_9_9 → 2026_9_9_gouchang。
# 写在场地配置里（老名字 NAS_DAY_SUFFIX 继续认）。两个场地各自往同一个 NAS 传，
# 日期目录是同一个，混在一起就没法一眼看出某个场地今天录全了没有。
# 本地目录就带上后缀，NAS 那边照搬同名目录，少一处拼接少一处能拼错的地方。
DAY_SUFFIX="${DAY_SUFFIX:-${NAS_DAY_SUFFIX:-}}"

# DEBUG：调试模式，什么都不存，只开画面。
#   DEBUG=1 SITE=狗场 ./record_multicam.sh
#
# 用来干一件具体的事：认清楚系统里的「摄像头 0」是哪个单间。画面上有 camN、
# 配对的狗名、imu 编号和实时 Hz，对着屏幕数一遍就知道谁是谁——这件事光看
# --probe 的能力表是看不出来的，那里面没有画面。
#
# 为什么要单独一个模式而不是"录一段再删"：调试要反复开关，每次都在硬盘上留一
# 段 720p 视频和一堆 CSV，还会污染归档目录（归档是按目录扫的）。
# 底层脚本不给 --duration 也不给 --align-hourly 时本来就不写任何文件，
# 这里只是把它接出来，顺便强制开预览、跳过 45 秒预热（调试等不起）。
DEBUG="${DEBUG:-0}"
case "$(echo "$DEBUG" | tr 'A-Z' 'a-z')" in
    1|yes|on|true|y) DEBUG=1 ;;
    0|no|off|false|n) DEBUG=0 ;;
    *) echo "DEBUG 只认 0/1（或 on/off、yes/no），收到: $DEBUG"; exit 1 ;;
esac

DURATION="${DURATION:-}"
if [ "$DEBUG" = "1" ]; then
    # 不传 --duration 也不传 --align-hourly = 只预览不落盘
    segment_args=()
    WARMUP_SEC="0"
    EXTRA_ARGS="${EXTRA_ARGS//--no-preview/}"   # 调试就是要看画面，PREVIEW=0 也不作数
    _preview_say="开（调试模式强制）"
    echo "调试模式：不保存任何文件，只开画面。按 q + 回车 或 Ctrl-C 退出。"
    echo "  画面上每路都写着 camN + 配对的狗名 + imu 编号 + 实时 Hz，对着屏幕认一遍就行。"
elif [ -n "$DURATION" ]; then
    # 定时和"按整点切"是互斥的：--align-hourly 会把每段的时长改成"到下一个
    # 整点还剩多久"，那样 --duration 根本不起作用
    segment_args=(--duration "$DURATION")
    echo "定时录制：$DURATION 秒后正常结束（不按整点切分、不循环）"
else
    segment_args=(--align-hourly --loop)
fi

# ── 一台机器只准跑一份 ───────────────────────────────────────────────
# 自动启动接上之后，"开机自己跑着一份 + 人手又敲一次"是迟早会发生的。两份
# 抢同一批摄像头和同一个蓝牙适配器，结果不是干脆报错，而是两边都断断续续
# 地录——文件都在、都能播，缺帧要对着 meta.csv 数才看得出来。
#
# 锁文件存的是这个 bash 的 PID，靠 kill -0 判断它还活不活着。进程没了锁文件
# 还在（断电、任务管理器强杀）不算数，下一次直接接管。
# 万一 PID 被复用误判了，按提示加 FORCE=1 跳过。
LOCK=".recording.lock"
if [ -f "$LOCK" ] && [ "${FORCE:-0}" != "1" ]; then
    old="$(cat "$LOCK" 2>/dev/null || true)"
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
        echo "已经有一份录制在跑了（PID $old），这次不启动。" >&2
        echo "  想停掉它：kill $old" >&2
        echo "  确认那个 PID 其实已经没了：FORCE=1 SITE=$SITE ./record_multicam.sh" >&2
        exit 1
    fi
    echo "清掉上次没删干净的锁文件（PID ${old:-空} 已经不在了）"
fi
echo $$ > "$LOCK"
echo "预览窗口：$_preview_say"
echo "  想看画面：敲 p + 回车 调出来，看完再敲一次关掉（开着大概掉四成帧率）"
echo "  只想认摄像头、不录数据：先退出，改跑 ./debug_cameras.sh"
trap 'rm -f "$LOCK"' EXIT

python imu_camera_sync_multicam.py \
    "${imu_args[@]}" "${imu_label_args[@]+"${imu_label_args[@]}"}" "${dog_name_args[@]}" \
    "${segment_args[@]}" --resample-hz "$RESAMPLE_HZ" \
    "${cam_args[@]}" "${pair_args[@]+"${pair_args[@]}"}" \
    "${rotate_args[@]+"${rotate_args[@]}"}" \
    --width "$WIDTH" --height "$HEIGHT" \
    --capture-width "$CAPTURE_WIDTH" --capture-height "$CAPTURE_HEIGHT" \
    "${resample_flag[@]}" --out-dir "$OUT_DIR" \
    --warmup-sec "$WARMUP_SEC" --cam-fps "$CAM_FPS" \
    --day-suffix "$DAY_SUFFIX" \
    --reconnect-max-backoff "$RECONNECT_MAX_BACKOFF" \
    $EXTRA_ARGS "${@}"
