#!/bin/bash
# 清理 imu_camera_sync_multicam.py / imu_camera_sync_rtsp_multicam.py 生成的
# {base}_camX_imuY_resampled{HZ}hz.mp4/.csv 配对文件，只保留指定的几组，其余
# 全部删除（比如筛选完标注要用的具体组合后，把没用到的配对清掉省地方）。
#
# 用法:
#   ./cleanup_resampled_pairs.sh [-y] <目录> <保留关键字> [<保留关键字> ...]
#
#   -y / --yes  跳过确认直接删（给 daily_archive.sh 这类自动化脚本用；
#               手动跑的时候别加，还是让它列出清单等你确认）
#
#   --no-rescue 关掉「每路摄像头至少保住一对」的兜底，回到完全按关键字来的老行为。
#               兜底只会**少删**、不会多删，正常不需要关；留这个开关是为了
#               「我就是要把这一路全删掉」的时候有办法。
#
#   每个"保留关键字"默认同时保留 mp4 和 csv；只想留其中一种时加后缀
#   :mp4 或 :csv。关键字用文件名里的 camX_imuY 片段（子串匹配，不用写全名）。
#
# 例子:
#   # 保留 cam1_imu1(mp4+csv)、cam2_imu2(mp4+csv)、cam3_imu1(只留mp4)，其余全删
#   ./cleanup_resampled_pairs.sh data/multicam_multiimu cam1_imu1 cam2_imu2 cam3_imu1:mp4
#
# 运行后会先列出打算删除的文件清单，输入 y 确认后才会真正删除。

set -euo pipefail

ASSUME_YES=0
RESCUE=1
while true; do
    case "${1:-}" in
        -y|--yes)    ASSUME_YES=1; shift ;;
        --no-rescue) RESCUE=0; shift ;;
        *) break ;;
    esac
done

if [ "$#" -lt 2 ]; then
    echo "用法: $0 [-y] <目录> <保留关键字> [<保留关键字> ...]"
    echo "例子: $0 data/multicam_multiimu cam1_imu1 cam2_imu2 cam3_imu1:mp4"
    exit 1
fi

DIR="$1"
shift

if [ ! -d "$DIR" ]; then
    echo "目录不存在: $DIR"
    exit 1
fi

# 构造 find 的排除条件：每个保留关键字展开成对应扩展名的 -name 匹配
keep_args=()
for spec in "$@"; do
    key="${spec%%:*}"
    if [[ "$spec" == *:mp4 ]]; then
        keep_args+=(-o -name "*${key}*.mp4")
    elif [[ "$spec" == *:csv ]]; then
        keep_args+=(-o -name "*${key}*.csv")
    else
        keep_args+=(-o -name "*${key}*.mp4" -o -name "*${key}*.csv")
    fi
done
# 去掉第一个多余的 -o
keep_args=("${keep_args[@]:1}")

echo "扫描目录: $DIR"
echo "保留关键字: $*"
echo

mapfile -t to_delete < <(find "$DIR" -maxdepth 1 -type f \( -name '*.mp4' -o -name '*.csv' \) \
    ! \( "${keep_args[@]}" \))

# ── 兜底：每路摄像头至少保住一对 ─────────────────────────────────────
#
# KEEP_PAIRS 是写死的设备号（cam1_imu1 cam2_imu3 …），它假设那几个设备一定连得上。
# 一旦某个没连上，它的配对文件根本不会生成，而那路摄像头的源视频
# （{base}_camN_raw.mp4）又不含任何关键字——于是那一路的画面被整个删掉，
# 而且一声不吭。
#
# 这不是假想：影棚全采 8 个设备的时候，一晚上有几个连不上是常态（BLE 这么多
# 设备本来就吃力）。真发生过：某天只有 1 个设备连上，跑一次清理，另外两路
# 摄像头的画面就没了。
#
# 所以这里补一道：某路摄像头一份 mp4 都没保住时，从它现有的配对里挑一对
# （mp4 + 同名 csv）留下来。**必须是完整的一对**——平台是按 camN_imuM 成对
# 建样本的，只留视频的话平台看到的是一个没有 CSV 的视频。
rescued=()
rescued_csv=()
if [ "$RESCUE" = "1" ]; then
for cam in $(printf '%s\n' "$DIR"/*.mp4 2>/dev/null | sed -n 's/.*_\(cam[0-9]\+\)\(_imu[0-9]\+\)\?_raw\.mp4$/\1/p' | sort -u); do
    # 这一路还有 mp4 活着吗
    alive=0
    for f in "$DIR"/*_"$cam"_*.mp4 "$DIR"/*_"$cam"_raw.mp4; do
        [ -f "$f" ] || continue
        keep_it=1
        for d in "${to_delete[@]}"; do [ "$d" = "$f" ] && keep_it=0 && break; done
        [ "$keep_it" = "1" ] && alive=1 && break
    done
    [ "$alive" = "1" ] && continue

    # 挑一对完整的救回来（按文件名排序，同一天多次跑结果一致）
    for f in $(printf '%s\n' "$DIR"/*_"$cam"_imu*_raw.mp4 2>/dev/null | sort); do
        [ -f "$f" ] || continue
        csv="${f%.mp4}.csv"
        [ -f "$csv" ] || continue
        keep=()
        for d in "${to_delete[@]}"; do
            [ "$d" = "$f" ] || [ "$d" = "$csv" ] || keep+=("$d")
        done
        # ${arr[@]+"${arr[@]}"}：空数组在老版本 bash + set -u 下直接展开会报
        # "unbound variable"。救回最后一对时 keep 正好可能是空的
        to_delete=(${keep[@]+"${keep[@]}"})
        rescued+=("$(basename "${f%.mp4}")")
        break
    done
done
fi
# ── 兜底之二：每个连上的设备至少保住一份 CSV ────────────────────────
#
# 上面那道保的是摄像头，这道保的是设备——而这道更容易漏，因为设备数比摄像头多。
#
# 影棚全采 8 个设备时 KEEP_PAIRS 是按「这个设备配这路摄像头」写死的
# （cam2_imu3、cam3_imu5…）。可哪路摄像头配哪个设备本来就是笛卡尔积、没有物理
# 含义，只要那一路当晚没开、或者设备编号跟当初写的对不上，这个设备的数据就
# 一份都留不下来——它的 CSV 只活在配对文件里，独立的 {base}_imuM_raw.csv
# 又不含任何关键字，一起被删。
#
# 真实例子：2026-09-13 那次 6 个设备连上了，KEEP_PAIRS 只点到 imu1/imu2，
# imu3/4/5/6 四个设备的数据会被全删光。
#
# 同一个设备配到不同摄像头的 CSV 内容**完全一样**（采集端就是先裁一份、其余
# 硬链接过去的），所以留哪一份都行，留一份就够。
[ "$RESCUE" = "1" ] || rescued_csv=()
for imu in $([ "$RESCUE" = "1" ] && printf '%s\n' "$DIR"/*.csv 2>/dev/null | sed -n 's/.*_cam[0-9]\+_\(imu[0-9]\+\)_raw\.csv$/\1/p' | sort -u); do
    alive=0
    for f in "$DIR"/*_"$imu"_raw.csv; do
        [ -f "$f" ] || continue
        keep_it=1
        for d in "${to_delete[@]}"; do [ "$d" = "$f" ] && keep_it=0 && break; done
        [ "$keep_it" = "1" ] && alive=1 && break
    done
    [ "$alive" = "1" ] && continue

    for f in $(printf '%s\n' "$DIR"/*_cam[0-9]*_"$imu"_raw.csv 2>/dev/null | sort); do
        [ -f "$f" ] || continue
        keep=()
        for d in "${to_delete[@]}"; do [ "$d" = "$f" ] || keep+=("$d"); done
        to_delete=(${keep[@]+"${keep[@]}"})
        rescued_csv+=("$(basename "$f")")
        break
    done
done
if [ "${#rescued_csv[@]}" -gt 0 ]; then
    echo "⚠ 这几个设备按 KEEP_PAIRS 一份 CSV 都保不住（它配的那路摄像头当晚没开，"
    echo "  或者设备编号跟 KEEP_PAIRS 写的对不上），各留了一份，免得整个设备的数据没了："
    printf '    %s\n' "${rescued_csv[@]}"
    echo
fi

if [ "${#rescued[@]}" -gt 0 ]; then
    echo "⚠ 这几路摄像头按 KEEP_PAIRS 一份视频都保不住（多半是那个设备当晚没连上），"
    echo "  各留了一对，免得整路画面消失："
    printf '    %s (.mp4 + .csv)\n' "${rescued[@]}"
    echo
fi

if [ "${#to_delete[@]}" -eq 0 ]; then
    echo "没有需要删除的文件。"
    exit 0
fi

echo "以下 ${#to_delete[@]} 个文件将被删除:"
printf '  %s\n' "${to_delete[@]}"
echo

if [ "$ASSUME_YES" = "1" ]; then
    echo "(-y 已指定，直接删除)"
else
    read -r -p "确认删除以上文件？(y/N) " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "已取消，未删除任何文件。"
        exit 0
    fi
fi

rm -v -- "${to_delete[@]}"
echo
echo "删除完成，共删除 ${#to_delete[@]} 个文件。"
