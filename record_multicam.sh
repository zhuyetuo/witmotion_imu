#!/bin/bash
# 正式长期录制：N个摄像头 + N个IMU设备，1080p原生采集缩放到720p输出，
# 按整点自动切分文件，循环录制直到手动停止。
#
# 用法:
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
#   only（默认）：只保留降采样后的 camX_imuY_resampled{HZ}hz.mp4/.csv 配对文件，
#                 删除原始的 {base}_camN.mp4/.csv/_meta.csv/_{imu}_raw.csv
#   none        ：只保留原始文件，不生成任何降采样配对文件（--no-resample）
#   both        ：原始文件和降采样配对文件都保留（不传 --resample-only 也不传 --no-resample）

set -euo pipefail

IMUS="${IMUS:-wit=WT901BLE68 wit=WTSDCL}"
CAMS="${CAMS:-0 1}"
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
CAPTURE_WIDTH="${CAPTURE_WIDTH:-1920}"
CAPTURE_HEIGHT="${CAPTURE_HEIGHT:-1080}"
RESAMPLE_HZ="${RESAMPLE_HZ:-16}"
CAM_FPS="${CAM_FPS:-25}"
WARMUP_SEC="${WARMUP_SEC:-10}"
OUT_DIR="${OUT_DIR:-data/multicam_multiimu}"
RESAMPLE_MODE="${RESAMPLE_MODE:-only}"

imu_args=()
for spec in $IMUS; do
    imu_args+=(--imu "$spec")
done

cam_args=()
for idx in $CAMS; do
    cam_args+=(--camera "$idx")
done

case "$RESAMPLE_MODE" in
    only) resample_flag=(--resample-only) ;;
    none) resample_flag=(--no-resample) ;;
    both) resample_flag=() ;;
    *) echo "RESAMPLE_MODE 只能是 only/none/both，收到的是: $RESAMPLE_MODE"; exit 1 ;;
esac

python imu_camera_sync_multicam.py \
    "${imu_args[@]}" \
    --align-hourly --resample-hz "$RESAMPLE_HZ" \
    "${cam_args[@]}" \
    --width "$WIDTH" --height "$HEIGHT" \
    --capture-width "$CAPTURE_WIDTH" --capture-height "$CAPTURE_HEIGHT" \
    --loop "${resample_flag[@]}" --out-dir "$OUT_DIR" \
    --warmup-sec "$WARMUP_SEC" --cam-fps "$CAM_FPS"
