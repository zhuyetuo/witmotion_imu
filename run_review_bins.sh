#!/bin/bash
# 一次性把一批置信度区间的"待复核"窗口都跑出来并剪成片段，不用一个个手动敲命令。
# 每个区间会生成:
#   scratch_log_review_<lo>-<hi>.txt   推理输出日志
#   clips_<lo>-<hi>/                    对应剪出来的片段（默认多视角，见 clip_scratch_segments.py）
#
# 用法:
#   ./run_review_bins.sh
#   （先改下面几个变量，或者用环境变量覆盖，比如:
#    CSV_DIR=data/xxx MODEL=ml_rf.pkl ./run_review_bins.sh）
#
# 注意: 每个区间都会重新跑一遍完整推理（特征提取+预测），--review_min/--review_max
# 只是筛选打印哪些窗口，不会复用上一个区间已经算好的结果——区间越多、总耗时越长
# （大致是"单次推理耗时 × 区间数"）。如果区间数很多、CSV文件也很多，之后需要的话
# 可以把 infer_scratch.py 改成一次推理、同时对多个区间分别汇总，效率会高很多；
# 现在先按你的用法一个个跑，逻辑更直接。

set -euo pipefail

CSV_DIR="${CSV_DIR:-data/multicam_multiimu3}"
PATTERN="${PATTERN:-*_resampled16hz.csv}"
MODEL="${MODEL:-ml_rf.pkl}"
DEVICE_HZ="${DEVICE_HZ:-16}"
CONF_THRESHOLD="${CONF_THRESHOLD:-0.7}"
WORKERS="${WORKERS:-16}"
CLIP_OUT_PREFIX="${CLIP_OUT_PREFIX:-clips}"

# 阈值区间列表，按需增删；每行是 "下限 上限"（空格分隔）。
# 注意：0.5~0.6 这一段没列进来（照你给的列表原样保留），想要的话自己加一行。
BINS=(
  "0.0 0.3"
  "0.3 0.4"
  "0.4 0.5"
  "0.6 0.7"
  "0.7 0.8"
  "0.8 0.9"
  "0.9 1.0"
)

for bin in "${BINS[@]}"; do
    read -r lo hi <<< "$bin"
    log="scratch_log_review_${lo}-${hi}.txt"
    outdir="${CLIP_OUT_PREFIX}_${lo}-${hi}"

    echo "════ 推理阈值段 ${lo}~${hi} → ${log} ════"
    python infer_scratch.py --csv_dir "$CSV_DIR" --pattern "$PATTERN" \
        --model "$MODEL" --device_hz "$DEVICE_HZ" --confidence_threshold "$CONF_THRESHOLD" \
        --scratch_only --quiet --workers "$WORKERS" \
        --review_min "$lo" --review_max "$hi" \
        > "$log"

    echo "════ 剪辑阈值段 ${lo}~${hi} → ${outdir}/ ════"
    python clip_scratch_segments.py "$log" --csv-dir "$CSV_DIR" --out-dir "$outdir"
    echo
done

echo "全部区间处理完成："
for bin in "${BINS[@]}"; do
    read -r lo hi <<< "$bin"
    echo "  ${lo}~${hi}:  scratch_log_review_${lo}-${hi}.txt  /  ${CLIP_OUT_PREFIX}_${lo}-${hi}/"
done
