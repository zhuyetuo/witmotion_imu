#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断"视频画面跟IMU时间戳对不齐、而且偏差会累积"这个问题的根因，区分两种
可能：

    (A) 真的是数据问题：IMU信号不稳定、断联重连，CSV里有真实的时间缺口，
        缺口累加起来导致后段时间对不上。
    (B) 不是数据问题，是播放器（比如 Label Studio）按拿视频文件当成恒定帧率
        (CFR) 来算"第N秒对应第几帧"，但我们录的其实是可变帧率(VFR)——每帧
        按真实时间戳写入的，帧间隔本身就不均匀。如果按"总帧数/平均帧率"这种
        线性公式换算播放位置，跟真实时间戳的差距会随时长累积，录得越久偏差
        越大，看起来像"数据跟不上"，其实是播放器的对齐假设跟VFR视频不匹配。

用法:
    python check_video_imu_drift.py multicam_20260822_110000919_cam1_imu1_raw

    （也可以分别指定，文件名不同名时用这个）
    python check_video_imu_drift.py --video xxx.mp4 --csv xxx.csv

依赖: ffprobe（ffmpeg自带，需要在PATH里）
"""

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime


def ffprobe_info(video_path: str) -> dict:
    cmd = [
        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=nb_frames,avg_frame_rate,r_frame_rate,duration',
        '-show_entries', 'format=duration',
        '-of', 'json', video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'ffprobe 执行失败: {result.stderr}', file=sys.stderr)
        sys.exit(1)
    data = json.loads(result.stdout)
    stream = data.get('streams', [{}])[0]
    fmt = data.get('format', {})

    def parse_rate(s):
        if not s or s == '0/0':
            return None
        num, den = s.split('/')
        den = float(den)
        return float(num) / den if den else None

    return {
        'nb_frames': int(stream['nb_frames']) if stream.get('nb_frames') not in (None, 'N/A') else None,
        'avg_frame_rate': parse_rate(stream.get('avg_frame_rate')),
        'r_frame_rate': parse_rate(stream.get('r_frame_rate')),
        'stream_duration': float(stream['duration']) if stream.get('duration') not in (None, 'N/A') else None,
        'format_duration': float(fmt['duration']) if fmt.get('duration') not in (None, 'N/A') else None,
    }


TS_FMT_CANDIDATES = ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S')


def parse_ts(s):
    for fmt in TS_FMT_CANDIDATES:
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def csv_info(csv_path: str) -> dict:
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        rows = [row for row in reader if row and row[0].strip()]

    timestamps = []
    valid_rows = 0
    missing_rows = 0
    for row in rows:
        ts = parse_ts(row[0])
        if ts is None:
            continue
        timestamps.append(ts)
        # 简单判断：除了 timestamp 外，acc_x 那一列（第2列）是不是空
        if len(row) > 1 and row[1].strip():
            valid_rows += 1
        else:
            missing_rows += 1

    if len(timestamps) < 2:
        return {'error': '有效时间戳行数太少'}

    span_s = (timestamps[-1] - timestamps[0]).total_seconds()
    diffs = [(b - a).total_seconds() for a, b in zip(timestamps, timestamps[1:])]
    median_diff = sorted(diffs)[len(diffs) // 2]
    gap_threshold = max(median_diff * 5, 0.5)  # 明显超过正常间隔5倍才算"缺口"
    gaps = [(i, d) for i, d in enumerate(diffs) if d > gap_threshold]

    return {
        'total_rows': len(rows),
        'valid_rows': valid_rows,
        'missing_rows': missing_rows,
        'first_ts': timestamps[0],
        'last_ts': timestamps[-1],
        'span_s': span_s,
        'median_interval_s': median_diff,
        'n_gaps': len(gaps),
        'total_gap_s': sum(d for _, d in gaps),
        'top_gaps': sorted(gaps, key=lambda x: -x[1])[:10],
    }


def main():
    ap = argparse.ArgumentParser(description='诊断视频/IMU对齐偏差是数据缺口还是VFR/CFR假设不匹配')
    ap.add_argument('base', nargs='?', help='不带扩展名的公共文件名（会自动找 {base}.mp4 / {base}.csv）')
    ap.add_argument('--video', help='视频文件路径（不用 base 时指定）')
    ap.add_argument('--csv', help='CSV文件路径（不用 base 时指定）')
    args = ap.parse_args()

    video_path = args.video or (f'{args.base}.mp4' if args.base else None)
    csv_path = args.csv or (f'{args.base}.csv' if args.base else None)
    if not video_path or not csv_path:
        print('请提供 base 或者 --video/--csv')
        sys.exit(1)

    print(f'视频: {video_path}')
    print(f'CSV : {csv_path}')
    print()

    vinfo = ffprobe_info(video_path)
    cinfo = csv_info(csv_path)

    if 'error' in cinfo:
        print(f'CSV 解析失败: {cinfo["error"]}')
        sys.exit(1)

    print('── 视频信息（ffprobe）──')
    print(f'  帧数(nb_frames)        : {vinfo["nb_frames"]}')
    print(f'  平均帧率(avg_frame_rate): {vinfo["avg_frame_rate"]}')
    print(f'  声明帧率(r_frame_rate)  : {vinfo["r_frame_rate"]}')
    print(f'  视频流时长(stream)      : {vinfo["stream_duration"]}')
    print(f'  容器时长(format)        : {vinfo["format_duration"]}')

    print()
    print('── CSV信息 ──')
    print(f'  总行数       : {cinfo["total_rows"]}')
    print(f'  有效行数     : {cinfo["valid_rows"]}')
    print(f'  缺失行数     : {cinfo["missing_rows"]} ({cinfo["missing_rows"]/cinfo["total_rows"]*100:.2f}%)')
    print(f'  起止时间     : {cinfo["first_ts"]}  →  {cinfo["last_ts"]}')
    print(f'  真实时间跨度 : {cinfo["span_s"]:.3f} 秒')
    print(f'  中位数采样间隔: {cinfo["median_interval_s"]*1000:.1f} ms')
    print(f'  检测到的缺口数: {cinfo["n_gaps"]}  总缺口时长: {cinfo["total_gap_s"]:.3f} 秒')
    if cinfo['top_gaps']:
        print('  最大的几个缺口（行号, 秒）:')
        for idx, gap in cinfo['top_gaps']:
            print(f'    第{idx}行附近: {gap:.3f}秒')

    print()
    print('── 关键对比：VFR/CFR假设 vs 真实数据缺口，谁的"锅"更大 ──')
    if vinfo['nb_frames'] and vinfo['avg_frame_rate']:
        duration_by_avgfps = vinfo['nb_frames'] / vinfo['avg_frame_rate']
        diff_avgfps_vs_csv = duration_by_avgfps - cinfo['span_s']
        print(f'  按"帧数/平均帧率"算出的视频时长: {duration_by_avgfps:.3f} 秒')
        print(f'  CSV真实时间跨度                : {cinfo["span_s"]:.3f} 秒')
        print(f'  两者差值（Label Studio这种线性播放假设 vs 真实时间的偏差）: '
              f'{diff_avgfps_vs_csv:+.3f} 秒')
    if vinfo['format_duration']:
        diff_container_vs_csv = vinfo['format_duration'] - cinfo['span_s']
        print(f'  容器实际时长 vs CSV真实跨度差值（如果视频PTS准确，这个应该接近0）: '
              f'{diff_container_vs_csv:+.3f} 秒')
    print()
    print('  解读: 如果"帧数/平均帧率"算出的时长明显偏离CSV真实跨度，但"容器实际')
    print('  时长"却很接近CSV真实跨度，说明视频本身的时间戳(PTS)是准的，问题出在')
    print('  播放器/标注工具按平均帧率线性换算播放位置——这种情况建议对齐时用视频')
    print('  的真实PTS而不是"帧数/平均帧率"；如果CSV里"缺口"占比很高、总缺口时长')
    print('  跟你感觉到的偏差量级相近，那就是真实信号缺失导致的，需要从信号稳定性')
    print('  这边入手。')


if __name__ == '__main__':
    main()
