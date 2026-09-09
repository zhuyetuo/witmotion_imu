#!/bin/bash
# 正式长期录制：N个摄像头 + N个IMU设备，1080p原生采集缩放到720p输出，
# 按整点自动切分文件，循环录制直到手动停止。
#
# 用法:
#   SITE=影棚 ./record_multicam.sh     ← 平时就用这个
#   SITE=狗场 ./record_multicam.sh
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
if [ -z "$SITE" ] && [ -f "sites/.current" ]; then
    SITE="$(tr -d '\r\n ' < sites/.current)"
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
if [ -n "${DEVICES:-}" ]; then
    IMUS=""; IMU_IDS=""; DOG_NAMES=""
    while IFS= read -r _line; do
        # 先砍掉行尾注释，再切三列。不砍的话 read 会把 # 后面整段都塞进狗名，
        # 而狗名是要画到画面上的，出来就是一长串乱码
        _line="${_line%%#*}"
        # 用数组读，不要 set --：set -- 会把脚本自己的位置参数覆盖掉
        read -ra _f <<< "$_line"
        [ ${#_f[@]} -eq 0 ] && continue
        if [ ${#_f[@]} -ne 3 ]; then
            echo "DEVICES 这一行应该是「编号 MAC 狗名」三列，收到 ${#_f[@]} 列: $_line"
            exit 1
        fi
        IMU_IDS="$IMU_IDS ${_f[0]#imu}"
        IMUS="$IMUS wit=${_f[1]}"
        DOG_NAMES="$DOG_NAMES ${_f[2]}"
    done <<< "$DEVICES"
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
DURATION="${DURATION:-}"
if [ -n "$DURATION" ]; then
    # 定时和"按整点切"是互斥的：--align-hourly 会把每段的时长改成"到下一个
    # 整点还剩多久"，那样 --duration 根本不起作用
    segment_args=(--duration "$DURATION")
    echo "定时录制：$DURATION 秒后正常结束（不按整点切分、不循环）"
else
    segment_args=(--align-hourly --loop)
fi

python imu_camera_sync_multicam.py \
    "${imu_args[@]}" "${imu_label_args[@]+"${imu_label_args[@]}"}" "${dog_name_args[@]}" \
    "${segment_args[@]}" --resample-hz "$RESAMPLE_HZ" \
    "${cam_args[@]}" "${pair_args[@]+"${pair_args[@]}"}" \
    --width "$WIDTH" --height "$HEIGHT" \
    --capture-width "$CAPTURE_WIDTH" --capture-height "$CAPTURE_HEIGHT" \
    "${resample_flag[@]}" --out-dir "$OUT_DIR" \
    --warmup-sec "$WARMUP_SEC" --cam-fps "$CAM_FPS" \
    --reconnect-max-backoff "$RECONNECT_MAX_BACKOFF" \
    $EXTRA_ARGS
