#!/bin/bash
# 正式长期录制：2个摄像头 + WT901BLE68/WTSDCL 两个IMU设备，1080p原生采集
# 缩放到720p输出，按整点自动切分文件，循环录制直到手动停止。
#
# 用法:
#   ./record_multicam.sh
#   （不想改这个文件的话，也可以用环境变量覆盖，比如:
#    OUT_DIR=data/multicam_multiimu2 CAM_FPS=30 ./record_multicam.sh）

set -euo pipefail

IMU1="${IMU1:-wit=WT901BLE68}"
IMU2="${IMU2:-wit=WTSDCL}"
CAMERAS=("${CAM1:-0}" "${CAM2:-1}")
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
CAPTURE_WIDTH="${CAPTURE_WIDTH:-1920}"
CAPTURE_HEIGHT="${CAPTURE_HEIGHT:-1080}"
RESAMPLE_HZ="${RESAMPLE_HZ:-16}"
CAM_FPS="${CAM_FPS:-25}"
WARMUP_SEC="${WARMUP_SEC:-10}"
OUT_DIR="${OUT_DIR:-data/multicam_multiimu}"

python imu_camera_sync_multicam.py \
    --imu "$IMU1" --imu "$IMU2" \
    --align-hourly --resample-hz "$RESAMPLE_HZ" \
    --camera "${CAMERAS[0]}" --camera "${CAMERAS[1]}" \
    --width "$WIDTH" --height "$HEIGHT" \
    --capture-width "$CAPTURE_WIDTH" --capture-height "$CAPTURE_HEIGHT" \
    --loop --resample-only --out-dir "$OUT_DIR" \
    --warmup-sec "$WARMUP_SEC" --cam-fps "$CAM_FPS"
