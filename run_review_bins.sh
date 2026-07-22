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
# infer_scratch.py 现在支持 --review_bins：特征提取+预测只做一次，同时对下面
# 列的所有区间分别出报告，不用像以前那样每个区间重新跑一遍完整推理。
#
# 只想跑其中一部分（比如已经有推理日志了，只想重新剪辑；或者只想先跑推理，
# 剪辑留着以后再说）可以用 STEP 环境变量控制：
#   STEP=all    默认，推理+剪辑都跑
#   STEP=infer  只跑推理，生成 scratch_log_review_*.txt，不剪辑
#   STEP=clip   只用已有的 scratch_log_review_*.txt 剪辑，不重新推理
#     （用法: STEP=clip ./run_review_bins.sh）

set -euo pipefail

CSV_DIR="${CSV_DIR:-data/multicam_multiimu3}"
PATTERN="${PATTERN:-*_resampled16hz.csv}"
MODEL="${MODEL:-ml_rf.pkl}"
DEVICE_HZ="${DEVICE_HZ:-16}"
CONF_THRESHOLD="${CONF_THRESHOLD:-0.7}"
WORKERS="${WORKERS:-16}"
CLIP_OUT_PREFIX="${CLIP_OUT_PREFIX:-clips}"
LOG_DIR="${LOG_DIR:-.}"
STEP="${STEP:-all}"

case "$STEP" in
  all|infer|clip) ;;
  *) echo "STEP 只能是 all/infer/clip，收到的是: $STEP"; exit 1 ;;
esac

# 阈值区间列表，按需增删；每项是 "lo-hi"（中间一个短横线）。
BINS=(
  "0.0-0.3"
  "0.3-0.4"
  "0.4-0.5"
  "0.5-0.6"
  "0.6-0.7"
  "0.7-0.8"
  "0.8-0.9"
  "0.9-1.0"
)

if [ "$STEP" = "all" ] || [ "$STEP" = "infer" ]; then
    echo "════ 一次推理，同时对 ${#BINS[@]} 个区间出报告 → ${LOG_DIR}/scratch_log_review_*.txt ════"
    python infer_scratch.py --csv_dir "$CSV_DIR" --pattern "$PATTERN" \
        --model "$MODEL" --device_hz "$DEVICE_HZ" --confidence_threshold "$CONF_THRESHOLD" \
        --scratch_only --quiet --workers "$WORKERS" \
        --review_bins "${BINS[@]}" --out_dir "$LOG_DIR"
    echo
else
    echo "════ STEP=clip，跳过推理，直接用 ${LOG_DIR}/scratch_log_review_*.txt 里已有的结果剪辑 ════"
fi

if [ "$STEP" = "all" ] || [ "$STEP" = "clip" ]; then
    for bin in "${BINS[@]}"; do
        lo="${bin%-*}"
        hi="${bin#*-}"
        log="${LOG_DIR}/scratch_log_review_${lo}-${hi}.txt"
        outdir="${CLIP_OUT_PREFIX}_${lo}-${hi}"

        if [ ! -f "$log" ]; then
            echo "⚠ 找不到 ${log}，跳过（STEP=clip 时需要先有对应的推理日志）"
            continue
        fi

        echo "════ 剪辑阈值段 ${lo}~${hi} → ${outdir}/ ════"
        python clip_scratch_segments.py "$log" --csv-dir "$CSV_DIR" --out-dir "$outdir"
        echo
    done

    echo "全部区间处理完成："
    for bin in "${BINS[@]}"; do
        lo="${bin%-*}"
        hi="${bin#*-}"
        echo "  ${lo}~${hi}:  ${LOG_DIR}/scratch_log_review_${lo}-${hi}.txt  /  ${CLIP_OUT_PREFIX}_${lo}-${hi}/"
    done
else
    echo "STEP=infer，只跑了推理，日志已生成在 ${LOG_DIR}/scratch_log_review_*.txt，剪辑请后续用 STEP=clip 跑。"
fi
