# -*- coding: utf-8 -*-
"""
校验 imu_camera_sync.py 输出的视频与 CSV 是否对齐
====================================================

读取 {base}.mp4 和 {base}.csv（Label Studio 格式），对比：
    - 帧数 vs CSV 行数（应严格相等，一帧一行）
    - 视频时长 vs CSV 时间戳覆盖的时长
    - 视频/CSV 的起止时间

用法:
    python check_alignment.py data/wit_d534e2b96f32_20260703_105514
    python check_alignment.py data/wit_d534e2b96f32_20260703_105514.mp4
    python check_alignment.py data/wit_d534e2b96f32_20260703_105514.csv
"""

import csv
import sys
from datetime import datetime

try:
    import cv2
except ImportError:
    print('缺少 opencv-python，请先安装: pip install opencv-python')
    sys.exit(1)

TS_FMT = '%Y-%m-%d %H:%M:%S.%f'


def _resolve_base(arg: str) -> str:
    for suffix in ('.mp4', '.csv', '_meta.csv'):
        if arg.endswith(suffix):
            return arg[: -len(suffix)]
    return arg


def check_video(video_path: str):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f'无法打开视频: {video_path}')
        return None
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    duration = frame_count / fps if fps > 0 else 0.0
    return {'frame_count': frame_count, 'fps': fps, 'duration': duration}


def check_csv(csv_path: str):
    timestamps = []
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            ts = row.get('timestamp', '').strip()
            if not ts:
                continue
            try:
                timestamps.append(datetime.strptime(ts, TS_FMT))
            except ValueError:
                continue
    if not timestamps:
        return None
    duration = (timestamps[-1] - timestamps[0]).total_seconds()
    return {
        'row_count': len(timestamps),
        'start': timestamps[0],
        'end': timestamps[-1],
        'duration': duration,
    }


def main():
    if len(sys.argv) != 2:
        print('用法: python check_alignment.py <base 或 .mp4 或 .csv 路径>')
        sys.exit(1)

    base = _resolve_base(sys.argv[1])
    video_path = f'{base}.mp4'
    csv_path   = f'{base}.csv'

    video_info = check_video(video_path)
    csv_info   = check_csv(csv_path)

    if video_info is None or csv_info is None:
        print('校验失败：视频或 CSV 无法读取，请检查路径。')
        sys.exit(1)

    print(f'视频: {video_path}')
    print(f'  帧数: {video_info["frame_count"]}   fps: {video_info["fps"]:.2f}   时长: {video_info["duration"]:.2f}s')
    print()
    print(f'CSV: {csv_path}')
    print(f'  行数: {csv_info["row_count"]}')
    print(f'  起始: {csv_info["start"].strftime(TS_FMT)[:-3]}')
    print(f'  结束: {csv_info["end"].strftime(TS_FMT)[:-3]}')
    print(f'  时长: {csv_info["duration"]:.2f}s')
    print()

    frame_diff = video_info['frame_count'] - csv_info['row_count']
    duration_diff = abs(video_info['duration'] - csv_info['duration'])

    print('── 对齐结果 ──')
    if frame_diff == 0:
        print(f'✔ 帧数与 CSV 行数一致: {video_info["frame_count"]}')
    else:
        print(f'✘ 帧数与 CSV 行数不一致: 视频 {video_info["frame_count"]} 帧, CSV {csv_info["row_count"]} 行 (差 {frame_diff})')

    if duration_diff <= 0.5:
        print(f'✔ 时长基本一致: 视频 {video_info["duration"]:.2f}s vs CSV {csv_info["duration"]:.2f}s (差 {duration_diff:.3f}s)')
    else:
        print(f'✘ 时长差异较大: 视频 {video_info["duration"]:.2f}s vs CSV {csv_info["duration"]:.2f}s (差 {duration_diff:.3f}s)')
        print('  （若未做 fps 元数据修正，视频用固定 fps 编码，播放时长可能与真实时长有出入，'
              '但只要帧数与 CSV 行数一致，帧对齐本身依然是准确的。）')


if __name__ == '__main__':
    main()
