# -*- coding: utf-8 -*-
"""
按时间范围截取 CSV
====================

从 Label Studio 格式的 CSV（timestamp, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z）
里截取指定起止时间之间的行，写出一份格式完全相同的新 CSV。

输出文件名 = 输入文件名（去掉扩展名）+ 时间范围后缀 + .csv，与输入同目录。
例如 26071009.csv 截取 2026-07-10 09:22:57 ~ 2026-07-10 09:24:01：
    26071009_092257-092401.csv

用法:
    python csv_time_slice.py 26071009.csv "2026-07-10 09:22:57" "2026-07-10 09:24:01"

    # 自定义输出路径（覆盖自动命名）
    python csv_time_slice.py 26071009.csv "2026-07-10 09:22:57" "2026-07-10 09:24:01" -o out.csv

起止时间支持带或不带毫秒：
    "2026-07-10 09:22:57"       （不带毫秒，视为 .000）
    "2026-07-10 09:22:57.500"   （带毫秒）
截取区间为闭区间 [start, end]（含边界时间点）。
"""

import argparse
import csv
import os
import sys
from datetime import datetime

TS_FMT_MS = '%Y-%m-%d %H:%M:%S.%f'
TS_FMT_S = '%Y-%m-%d %H:%M:%S'


def parse_ts(s: str) -> datetime:
    s = s.strip()
    try:
        return datetime.strptime(s, TS_FMT_MS)
    except ValueError:
        pass
    try:
        return datetime.strptime(s, TS_FMT_S)
    except ValueError:
        raise ValueError(f'无法解析时间: {s!r}，应为 "YYYY-MM-DD HH:MM:SS" 或 "YYYY-MM-DD HH:MM:SS.fff"')


def parse_row_ts(s: str) -> datetime:
    s = s.strip()
    if '.' in s:
        return datetime.strptime(s, TS_FMT_MS)
    return datetime.strptime(s, TS_FMT_S)


def slice_csv(input_path: str, start: datetime, end: datetime, output_path: str):
    with open(input_path, newline='', encoding='utf-8-sig') as fin:
        reader = csv.reader(fin)
        header = next(reader)
        kept = 0
        with open(output_path, 'w', newline='', encoding='utf-8') as fout:
            writer = csv.writer(fout)
            writer.writerow(header)
            for row in reader:
                if not row:
                    continue
                try:
                    ts = parse_row_ts(row[0])
                except ValueError:
                    continue
                if start <= ts <= end:
                    writer.writerow(row)
                    kept += 1
    return kept


def main():
    ap = argparse.ArgumentParser(description='按时间范围截取 Label Studio 格式 CSV')
    ap.add_argument('input', help='输入 CSV 路径')
    ap.add_argument('start', help='起始时间，"YYYY-MM-DD HH:MM:SS[.fff]"')
    ap.add_argument('end', help='结束时间，"YYYY-MM-DD HH:MM:SS[.fff]"')
    ap.add_argument('-o', '--output', default=None,
                     help='输出路径，默认在输入文件同目录，文件名加上时间范围后缀')
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print(f'文件不存在: {args.input}')
        sys.exit(1)

    try:
        start = parse_ts(args.start)
        end = parse_ts(args.end)
    except ValueError as e:
        print(e)
        sys.exit(1)

    if start > end:
        print(f'起始时间晚于结束时间: {start} > {end}')
        sys.exit(1)

    if args.output:
        out_path = args.output
    else:
        suffix = f'_{start.strftime("%H%M%S")}-{end.strftime("%H%M%S")}'
        out_path = os.path.splitext(args.input)[0] + suffix + '.csv'

    kept = slice_csv(args.input, start, end, out_path)
    if kept == 0:
        print(f'警告: 指定的时间范围内没有匹配到任何数据行（{start} ~ {end}），仍生成了只有表头的文件: {out_path}')
    else:
        print(f'已生成: {out_path}（{kept} 行，{start} ~ {end}）')


if __name__ == '__main__':
    main()
