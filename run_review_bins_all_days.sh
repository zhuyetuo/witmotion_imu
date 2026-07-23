#!/bin/bash
# 批量跑 run_review_bins.sh：对 DATA_ROOT 下面每一个日期子文件夹（比如
# data/2026_7_17, data/2026_7_18, data/2026_7_19...）各跑一遍，不用一天天
# 手动改 CSV_DIR 敲命令。
#
# 用法:
#   DATA_ROOT=data MODEL=ml_rf.pkl WORKERS=16 CONF_THRESHOLD=0.7 \
#       RESULT_ROOT=infer_result ./run_review_bins_all_days.sh
#
# MODEL/WORKERS/CONF_THRESHOLD/RESULT_ROOT/STEP/PATTERN/DEVICE_HZ/
# CLIP_OUT_PREFIX 这些环境变量都会原样透传给每一次 run_review_bins.sh 调用，
# 用法跟平时单独跑 run_review_bins.sh 完全一样，只是 CSV_DIR 由这个脚本
# 自动按子目录一个个传（每天的推理结果还是各自建在 RESULT_ROOT/对应日期/ 下，
# 互不覆盖）。
#
# 只想跑其中几天，用 DAYS 指定（空格分隔的文件夹名，不带 DATA_ROOT 前缀）：
#   DATA_ROOT=data DAYS="2026_7_18 2026_7_19" ./run_review_bins_all_days.sh
# 不指定 DAYS 就跑 DATA_ROOT 下所有子目录。
#
# 天数多的时候（比如60天），想全部跑但排除个别文件夹（比如 test），用
# EXCLUDE_DAYS 指定要排除的名字（空格分隔），不用把剩下几十天名字全列出来：
#   DATA_ROOT=data EXCLUDE_DAYS="test" ./run_review_bins_all_days.sh
#
# 某一天处理失败（比如那天目录里没有CSV、模型加载出错）不会中断整批，会跳到
# 下一天继续跑，最后汇总打印哪几天失败。

set -euo pipefail

DATA_ROOT="${DATA_ROOT:-data}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -n "${DAYS:-}" ]; then
    read -ra day_names <<< "$DAYS"
else
    day_names=()
    for d in "$DATA_ROOT"/*/; do
        [ -d "$d" ] || continue
        day_names+=("$(basename "$d")")
    done
fi

if [ -n "${EXCLUDE_DAYS:-}" ]; then
    read -ra exclude_names <<< "$EXCLUDE_DAYS"
    filtered=()
    for day in "${day_names[@]}"; do
        skip=0
        for ex in "${exclude_names[@]}"; do
            [ "$day" = "$ex" ] && skip=1 && break
        done
        [ "$skip" -eq 0 ] && filtered+=("$day")
    done
    day_names=("${filtered[@]}")
fi

if [ ${#day_names[@]} -eq 0 ]; then
    echo "在 ${DATA_ROOT} 下没找到任何子目录，检查 DATA_ROOT/EXCLUDE_DAYS 是否传对了。"
    exit 1
fi

echo "共 ${#day_names[@]} 天要跑: ${day_names[*]}"
echo

failed=()
for day in "${day_names[@]}"; do
    echo "════════════════════════════════════════════"
    echo "════ 处理 ${day} ════"
    echo "════════════════════════════════════════════"
    if CSV_DIR="${DATA_ROOT}/${day}" "${SCRIPT_DIR}/run_review_bins.sh"; then
        echo "✔ ${day} 处理完成"
    else
        echo "✘ ${day} 处理失败，继续跑下一天"
        failed+=("$day")
    fi
    echo
done

echo "全部 ${#day_names[@]} 天跑完。"
if [ ${#failed[@]} -gt 0 ]; then
    echo "其中失败: ${failed[*]}"
    exit 1
fi
