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
if [ "${1:-}" = "-y" ] || [ "${1:-}" = "--yes" ]; then
    ASSUME_YES=1
    shift
fi

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
