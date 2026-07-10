# -*- coding: utf-8 -*-
"""
HICC_PetCollar 离线数据 -> Label Studio 格式 CSV
==================================================

HICC 设备导出的离线日志格式（逗号分隔文本，示例 26060314.TXT）:
    HH:MM:SS.MS,AX,AY,AZ,GX,GY,GZ
    14:23:48.000,1.124950,-7.168772,4.831635,0.092175,0.862847,0.139626
    ...

只有"时:分:秒.毫秒"，没有年月日。日期按以下优先级确定:
    1. --date 显式指定（YYYY-MM-DD）
    2. 文件名形如 YYMMDDHH 的前 6~8 位数字（例如 26060314.TXT
       -> 20 26-06-03，其中末两位 14 是小时，与文件内第一行的
       时间小时数一致，用来交叉验证文件名确实是这种编码）
    3. 都识别不到就用今天日期，并打印警告（此时绝对日期可能不对，
       但同一份文件内的相对时间顺序仍然正确）

输出:
    Label Studio 兼容格式: timestamp, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z
    timestamp 格式 %Y-%m-%d %H:%M:%S.%L（D3 用 %L 三位毫秒，不支持 %f 微秒）
    输出文件默认跟输入同名同目录，仅把扩展名换成 .csv
    （例如 data/26060314.TXT -> data/26060314.csv），不需要额外传 -o。

用法:
    python hicc_offline_to_labelstudio.py data/26060314.TXT
    python hicc_offline_to_labelstudio.py data/26060314.TXT --date 2026-06-03
    python hicc_offline_to_labelstudio.py data/26060314.TXT -o custom_out.csv
"""

import argparse
import csv
import os
import re
import sys
from datetime import date, datetime, timedelta

LABELSTUDIO_HEADER = ['timestamp', 'acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z']

_FNAME_DATE_RE = re.compile(r'(\d{2})(\d{2})(\d{2})(\d{2})')


def guess_date_from_filename(path: str, first_row_hour: int = None):
    """
    尝试从文件名解析日期，形如 YYMMDDHH（8位数字，末两位是小时）。
    如果 first_row_hour 提供且与文件名末两位小时不一致，视为识别失败。
    """
    name = os.path.basename(path)
    m = _FNAME_DATE_RE.search(name)
    if not m:
        return None
    yy, mm, dd, hh = m.groups()
    try:
        year = 2000 + int(yy)
        d = date(year, int(mm), int(dd))
    except ValueError:
        return None
    if first_row_hour is not None and int(hh) != first_row_hour:
        return None
    return d


def parse_hicc_offline_csv(path: str):
    """读取 HICC 离线 CSV（HH:MM:SS.MS,AX,AY,AZ,GX,GY,GZ），返回逐行 dict 列表。"""
    rows = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for line in reader:
            if not line or len(line) < 7:
                continue
            rows.append({
                'time_str': line[0].strip(),
                'acc_x': line[1].strip(), 'acc_y': line[2].strip(), 'acc_z': line[3].strip(),
                'gyro_x': line[4].strip(), 'gyro_y': line[5].strip(), 'gyro_z': line[6].strip(),
            })
    return rows


_MIDNIGHT_WRAP_THRESHOLD = timedelta(hours=12)


def build_labelstudio_rows(rows, base_date: date):
    """
    把 HH:MM:SS.MS + 日期 拼成完整 timestamp。

    时间戳倒退时区分两种情况：
      1. 真正跨午夜（倒退幅度接近一整天，比如 23:59 -> 00:00）：日期 +1。
      2. 设备日志自身的小毛刺（倒退幅度很小，比如同一分钟内秒数从 59 突然
         跳回 01，分钟数没变——这是 HICC 部分离线日志里实际出现过的设备端
         记录异常，不代表真的跨天了）：不改日期，直接丢弃这一行（Label
         Studio 要求 timestamp 严格递增，混进倒退的行会导致标注工具报错），
         并统计丢弃数量提示用户。
    """
    out = []
    prev_dt = None
    day_offset = 0
    dropped = 0
    for r in rows:
        h, m, rest = r['time_str'].split(':')
        s, ms = rest.split('.')
        t = datetime(base_date.year, base_date.month, base_date.day,
                      int(h), int(m), int(s), int(ms) * 1000) + timedelta(days=day_offset)

        if prev_dt is not None and t < prev_dt:
            backward = prev_dt - t
            if backward >= _MIDNIGHT_WRAP_THRESHOLD:
                day_offset += 1
                t += timedelta(days=1)
            else:
                dropped += 1
                continue

        prev_dt = t
        ts_str = t.strftime('%Y-%m-%d %H:%M:%S.') + f'{t.microsecond // 1000:03d}'
        out.append([ts_str, r['acc_x'], r['acc_y'], r['acc_z'], r['gyro_x'], r['gyro_y'], r['gyro_z']])

    if dropped:
        print(f'警告: 发现 {dropped} 行时间戳小幅倒退（设备日志自身的记录异常，不是跨午夜），'
              f'已丢弃这些行以保证 timestamp 严格递增（Label Studio 要求）。')

    report_time_gaps(out)
    return out


def report_time_gaps(out_rows, gap_ratio: float = 5.0):
    """
    检测输出结果里是否存在真实的时间缺口（设备本身没记录到数据，不是脚本丢弃造成的）。
    用中位数采样间隔估算正常节奏，超过 gap_ratio 倍中位间隔的地方视为一次缺口并打印，
    方便区分"设备真的没录到" vs "脚本因为倒退异常主动丢弃的行"。
    """
    if len(out_rows) < 3:
        return
    ts_list = [datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S.%f') for row in out_rows]
    diffs_ms = [(ts_list[i] - ts_list[i - 1]).total_seconds() * 1000 for i in range(1, len(ts_list))]
    diffs_ms.sort()
    median_ms = diffs_ms[len(diffs_ms) // 2]
    if median_ms <= 0:
        return

    threshold_ms = median_ms * gap_ratio
    gaps = []
    for i in range(1, len(ts_list)):
        gap_ms = (ts_list[i] - ts_list[i - 1]).total_seconds() * 1000
        if gap_ms > threshold_ms:
            gaps.append((out_rows[i - 1][0], out_rows[i][0], gap_ms))

    if gaps:
        print(f'提示: 发现 {len(gaps)} 处真实数据缺口（设备本身没有记录到这段时间的数据，不是脚本丢弃的）：')
        for before, after, gap_ms in gaps[:20]:
            print(f'  {before}  →  {after}  （缺口约 {gap_ms:.0f} ms）')
        if len(gaps) > 20:
            print(f'  ...（其余 {len(gaps) - 20} 处从略）')


def main():
    ap = argparse.ArgumentParser(description='HICC_PetCollar 离线数据转 Label Studio 格式 CSV')
    ap.add_argument('input', help='HICC 离线数据文件路径（HH:MM:SS.MS,AX,AY,AZ,GX,GY,GZ 格式）')
    ap.add_argument('-o', '--output', default=None,
                     help='输出路径，默认跟输入文件同名同目录，仅扩展名改为 .csv')
    ap.add_argument('--date', default=None, help='显式指定日期 YYYY-MM-DD，覆盖文件名自动识别')
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print(f'文件不存在: {args.input}')
        sys.exit(1)

    rows = parse_hicc_offline_csv(args.input)
    if not rows:
        print('未解析到任何数据行。')
        sys.exit(1)

    if args.date:
        try:
            base_date = datetime.strptime(args.date, '%Y-%m-%d').date()
        except ValueError:
            print(f'--date 格式错误，应为 YYYY-MM-DD: {args.date}')
            sys.exit(1)
    else:
        first_hour = int(rows[0]['time_str'].split(':')[0])
        base_date = guess_date_from_filename(args.input, first_row_hour=first_hour)
        if base_date is None:
            base_date = datetime.now().date()
            print(f'警告: 无法从文件名识别日期，使用今天日期 {base_date}（同一份文件内的相对时间顺序仍正确，'
                  f'但绝对日期可能不对；可用 --date YYYY-MM-DD 显式指定）。')
        else:
            print(f'从文件名识别日期: {base_date}')

    ls_rows = build_labelstudio_rows(rows, base_date)

    if args.output:
        out_path = args.output
    else:
        out_path = os.path.splitext(args.input)[0] + '.csv'

    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(LABELSTUDIO_HEADER)
        writer.writerows(ls_rows)

    print(f'已生成: {out_path}（{len(ls_rows)} 行）')
    print('提示: 在 Label Studio 的 Time Series 标注配置里，timeFormat 请填: %Y-%m-%d %H:%M:%S.%L')


if __name__ == '__main__':
    main()
