# -*- coding: utf-8 -*-
"""
检查 imu_camera_sync_multi.py 生成的 _meta.csv 里每个 IMU 设备的对齐质量
=========================================================================

自动识别 _meta.csv 里有哪些设备（根据 {label}_lag_ms 列名），分别统计每个
设备的 lag_ms 分布、imu_missing 比例、hz 统计，方便快速判断多设备同步质量。

用法:
    python check_multi_imu_quality.py data/multi_20260714_164918_meta.csv
"""

import argparse
import csv
import re
import statistics
import sys

_LABEL_RE = re.compile(r'^(.+)_lag_ms$')


def detect_labels(header):
    labels = []
    for col in header:
        m = _LABEL_RE.match(col)
        if m:
            labels.append(m.group(1))
    return labels


def main():
    ap = argparse.ArgumentParser(description='统计 imu_camera_sync_multi.py 生成的 _meta.csv 里每个设备的对齐质量')
    ap.add_argument('meta_csv', help='_meta.csv 文件路径')
    args = ap.parse_args()

    try:
        with open(args.meta_csv, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames
            rows = list(reader)
    except OSError as e:
        print(f'无法读取文件: {e}')
        sys.exit(1)

    if not rows:
        print('文件没有数据行。')
        sys.exit(1)

    labels = detect_labels(header)
    if not labels:
        print('未能从表头识别出任何设备（找不到 {label}_lag_ms 列），确认这是 imu_camera_sync_multi.py 生成的 _meta.csv。')
        sys.exit(1)

    total_rows = len(rows)
    print(f'总帧数: {total_rows}')
    print(f'识别到 {len(labels)} 个设备: {", ".join(labels)}')
    print()

    for label in labels:
        lag_col = f'{label}_lag_ms'
        missing_col = f'{label}_missing'
        hz_col = f'{label}_hz'

        lags = []
        missing_count = 0
        hz_values = []
        for row in rows:
            if row.get(missing_col, '') == '1':
                missing_count += 1
            else:
                lag_str = row.get(lag_col, '').strip()
                if lag_str:
                    try:
                        lags.append(float(lag_str))
                    except ValueError:
                        pass
            hz_str = row.get(hz_col, '').strip()
            if hz_str:
                try:
                    hz_values.append(float(hz_str))
                except ValueError:
                    pass

        missing_pct = missing_count / total_rows * 100.0
        print(f'── {label} ──')
        print(f'  imu_missing: {missing_count}/{total_rows} ({missing_pct:.1f}%)')
        if hz_values:
            print(f'  imu_hz: mean={statistics.mean(hz_values):.1f}  '
                  f'min={min(hz_values):.1f}  max={max(hz_values):.1f}')
        if lags:
            sorted_lags = sorted(lags)
            print(f'  lag_ms: min={min(lags):.1f}  max={max(lags):.1f}  '
                  f'mean={statistics.mean(lags):.1f}  median={statistics.median(lags):.1f}')
            for th in (10, 20, 30, 50, 100):
                cnt = sum(1 for l in lags if l <= th)
                print(f'    <= {th:3d} ms: {cnt:5d} 帧 ({cnt / len(lags) * 100:.1f}%)')
        else:
            print('  lag_ms: 无有效数据（该设备全程 missing）')
        print()


if __name__ == '__main__':
    main()
