#!/bin/bash
# 把 --loop 循环录制产生的一堆按分钟/小段切分的 resampled mp4/csv，按文件名里的
# 时间戳合并成每小时一份，减少标注时要处理的文件数量。
#
# 识别的文件名格式（imu_camera_sync_multicam.py / imu_camera_sync_rtsp_multicam.py
# 生成的降采样配对文件）:
#   {前缀}_{YYYYMMDD}_{HHMMSS}_{camX_imuY}_resampled{HZ}hz.mp4/.csv
# 按 日期+小时 + camX_imuY + 频率 分组，同一组内的文件按时间顺序合并成一个：
#   {前缀}_{YYYYMMDD}{HH}_{camX_imuY}_resampled{HZ}hz.mp4/.csv
#
# 用法:
#   ./merge_hourly_segments.sh <目录> [--delete-originals]
#
#   --delete-originals   合并成功后删除参与合并的原始小段文件（默认保留，
#                         只生成合并后的新文件，不删源文件，更安全）
#
# 依赖: ffmpeg（合并mp4用 concat demuxer + -c copy，不重新编码，速度快、无损）

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "用法: $0 <目录> [--delete-originals]"
    exit 1
fi

DIR="$1"
shift
DELETE_ORIGINALS=0
if [ "${1:-}" == "--delete-originals" ]; then
    DELETE_ORIGINALS=1
fi

if [ ! -d "$DIR" ]; then
    echo "目录不存在: $DIR"
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "错误: 未找到 ffmpeg，合并mp4需要它（csv本身不需要，但为保持逻辑一致这里统一要求）。"
    exit 1
fi

# 把 mp4/csv 都拿出来按 (日期+小时, camX_imuY, hz) 分组，值是 "时间戳 完整路径"
# 用一个临时文件做分组索引（bash 4的关联数组在某些环境下不好搞多值，用文件更稳）
INDEX=$(mktemp)
trap 'rm -f "$INDEX"' EXIT

TAB=$'\t'
for f in "$DIR"/*_resampled*hz.mp4 "$DIR"/*_resampled*hz.csv; do
    [ -e "$f" ] || continue
    fname=$(basename "$f")
    ext="${fname##*.}"
    stem="${fname%.*}"
    # 匹配: 前缀_YYYYMMDD_HHMMSS_camX_imuY_resampledHZhz
    if [[ "$stem" =~ ^(.+)_([0-9]{8})_([0-9]{6})_(cam[0-9]+_imu[0-9]+)_resampled([0-9.]+)hz$ ]]; then
        prefix="${BASH_REMATCH[1]}"
        date="${BASH_REMATCH[2]}"
        time="${BASH_REMATCH[3]}"
        combo="${BASH_REMATCH[4]}"
        hz="${BASH_REMATCH[5]}"
        hour="${time:0:2}"
        # 每行7个字段，统一用 TAB 分隔（文件名/路径不含TAB，不会跟字段内容冲突）：
        # prefix  datehour  combo  hz  ext  datetime(排序用)  完整路径
        printf '%s\t%s\t%s\t%s\t%s\t%s%s\t%s\n' \
            "$prefix" "${date}${hour}" "$combo" "$hz" "$ext" "$date" "$time" "$f" >> "$INDEX"
    fi
done

if [ ! -s "$INDEX" ]; then
    echo "在 $DIR 里没找到符合命名规则的 resampled mp4/csv 文件（{前缀}_YYYYMMDD_HHMMSS_camX_imuY_resampledHZhz.mp4/.csv）。"
    exit 0
fi

# 按 (prefix, datehour, combo, hz, ext) 分组处理
cut -f1-5 "$INDEX" | sort -u | while IFS="$TAB" read -r prefix datehour combo hz ext; do
    mapfile -t files < <(awk -F'\t' -v p="$prefix" -v d="$datehour" -v c="$combo" -v h="$hz" -v e="$ext" \
        '$1==p && $2==d && $3==c && $4==h && $5==e' "$INDEX" | sort -t $'\t' -k6,6 | cut -f7)
    n="${#files[@]}"
    if [ "$n" -eq 0 ]; then
        continue
    fi

    out="$DIR/${prefix}_${datehour}_${combo}_resampled${hz}hz.${ext}"
    echo "── ${combo} (${datehour}, .${ext}) ── 合并 ${n} 个文件 → $(basename "$out")"

    if [ "$ext" == "mp4" ]; then
        listfile=$(mktemp)
        for f in "${files[@]}"; do
            printf "file '%s'\n" "$(cd "$(dirname "$f")" && pwd)/$(basename "$f")" >> "$listfile"
        done
        if ffmpeg -y -loglevel error -f concat -safe 0 -i "$listfile" -c copy "$out"; then
            echo "  ✔ 已生成: $out"
        else
            echo "  ✘ ffmpeg 合并失败，跳过删除源文件"
            rm -f "$listfile"
            continue
        fi
        rm -f "$listfile"
    else
        # csv: 只保留第一个文件的表头，后面文件跳过表头只拼数据行
        first=1
        : > "$out"
        for f in "${files[@]}"; do
            if [ "$first" -eq 1 ]; then
                cat "$f" >> "$out"
                first=0
            else
                tail -n +2 "$f" >> "$out"
            fi
        done
        echo "  ✔ 已生成: $out"
    fi

    if [ "$DELETE_ORIGINALS" -eq 1 ]; then
        for f in "${files[@]}"; do
            rm -f "$f"
        done
        echo "  已删除 ${n} 个原始小段文件"
    fi
done

echo
echo "全部处理完成。"
if [ "$DELETE_ORIGINALS" -eq 0 ]; then
    echo "（原始小段文件已保留，确认合并结果没问题后可以手动删除，或加 --delete-originals 自动删除）"
fi
