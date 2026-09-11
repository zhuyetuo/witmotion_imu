# -*- coding: utf-8 -*-
"""
检查一次录制里每个设备的实际采样率
====================================

跑完一次录制（或者 cp 到 NAS 之前）拿这个扫一眼：每个 IMU 到底是不是 50Hz，
有没有哪个设备中途掉线、掉了多久。

跟 check_multi_imu_quality.py 的区别：那个读 _meta.csv，狗场清理脚本会把
_meta.csv 删掉；这个直接读每个设备自己的 CSV，清理前后都能跑。

量法跟平台那边一致（app/utils/ffprobe.py 的 measure_csv_hz）：
采样率 = 行数 / 总时长，不是"相邻间隔的倒数"。一次串口读会带回一整批帧，
写进 CSV 就是几行挤在同一瞬间、然后隔一个整包的时间，按间隔倒数算会量出
几千 Hz。真正掉数据的缝（远大于平均间隔）剔掉再算。

用法:
    python check_sample_hz.py data/2026_9_11_gouchang
    python check_sample_hz.py data/2026_9_11_gouchang --hz 50 --tol 0.1
    python check_sample_hz.py data/*/                  # 一次看多天

    --hz    期望采样率，默认 50
    --tol   允许偏差，默认 0.1（±10%）
"""

import argparse
import csv
import glob
import os
import re
import sys
from datetime import datetime

_IMU_RE = re.compile(r'_imu(\d+)(?:_dog[A-Za-z0-9]+)?(?:_raw)?$', re.IGNORECASE)
_FMTS = ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S.%f')


def parse_ts(v):
    """第一列统一转成"秒"。可能是日期串，也可能是 epoch 毫秒的浮点数。"""
    v = v.strip()
    for f in _FMTS:
        try:
            return datetime.strptime(v, f).timestamp()
        except ValueError:
            pass
    try:
        n = float(v)
    except ValueError:
        return None
    # 用绝对量级判断单位：epoch 秒 1.8e9，毫秒 1.8e12，微秒 1.8e15
    a = abs(n)
    if a >= 1e14:
        return n / 1e6
    if a >= 1e11:
        return n / 1e3
    return n


def measure(path):
    ts = []
    try:
        with open(path, encoding='utf-8-sig', newline='') as f:
            reader = csv.reader(f)
            try:
                next(reader)
            except StopIteration:
                return None
            for row in reader:
                if not row or not row[0]:
                    continue
                t = parse_ts(row[0])
                if t is not None:
                    ts.append(t)
    except OSError as exc:
        return {'error': str(exc)}
    if len(ts) < 10:
        return {'error': '数据行不足 10 行'}

    deltas = [d for d in (ts[i + 1] - ts[i] for i in range(len(ts) - 1)) if d >= 0]
    if not deltas or sum(deltas) <= 0:
        return {'error': '时间戳没有推进'}

    # 剔掉"数据缝"：用平均间隔当标尺（帧挤在一起不会把平均值拉动多少，
    # 真正掉一段数据的缝一定远大于平均值），迭代两轮收敛
    kept = deltas
    limit = float('inf')
    for _ in range(3):
        new_limit = (sum(kept) / len(kept)) * 10
        shrunk = [d for d in kept if d <= new_limit]
        if not shrunk or sum(shrunk) <= 0 or len(shrunk) == len(kept):
            break
        kept, limit = shrunk, new_limit
    gaps = sorted((d for d in deltas if d > limit), reverse=True)
    span = sum(kept)
    return {
        'rows': len(ts),
        'hz': len(kept) / span if span > 0 else None,
        'span': span,
        'wall': ts[-1] - ts[0],
        'gap_count': len(deltas) - len(kept),
        'gap_max': gaps[0] if gaps else 0.0,
        'gap_total': sum(gaps),
    }


def main():
    ap = argparse.ArgumentParser(description='检查一次录制里每个设备的实际采样率')
    ap.add_argument('dirs', nargs='+', help='录制目录（可以给多个）')
    ap.add_argument('--hz', type=float, default=50.0, help='期望采样率，默认 50')
    ap.add_argument('--tol', type=float, default=0.1, help='允许偏差，默认 0.1 = ±10%%')
    args = ap.parse_args()

    bad = 0
    for d in args.dirs:
        if not os.path.isdir(d):
            print('目录不存在，跳过: %s' % d)
            continue
        # 每个设备可能有好几个文件名（配对的 camN_imuM、未裁剪的 imuM），
        # 同一个设备只量一次，优先量配对那份（跟样本平台用的是同一份）
        by_imu = {}
        for p in sorted(glob.glob(os.path.join(d, '*.csv'))):
            stem = os.path.splitext(os.path.basename(p))[0]
            m = _IMU_RE.search(stem)
            if not m:
                continue
            imu = int(m.group(1))
            paired = '_cam' in stem.lower()
            if imu not in by_imu or (paired and not by_imu[imu][1]):
                by_imu[imu] = (p, paired)

        print('\n%s  （%d 个设备）' % (d, len(by_imu)))
        if not by_imu:
            print('  没找到 *_imuN*.csv')
            continue
        print('  %-7s %8s %9s %8s  %-28s %s' % ('设备', '行数', '时长(秒)', '实测Hz', '掉数据', '判定'))
        for imu in sorted(by_imu):
            path, _ = by_imu[imu]
            r = measure(path)
            if r is None or 'error' in r:
                bad += 1
                print('  imu%-4d %s' % (imu, (r or {}).get('error', '读不出来')))
                continue
            hz = r['hz']
            off = abs(hz - args.hz) / args.hz
            if off > args.tol:
                bad += 1
                verdict = '!! 偏离期望 %.0f Hz %+.0f%%' % (args.hz, (hz / args.hz - 1) * 100)
            else:
                verdict = 'OK'
            gap = '-' if not r['gap_count'] else '%d 段 / 共 %.1fs / 最长 %.1fs' % (
                r['gap_count'], r['gap_total'], r['gap_max'])
            print('  imu%-4d %8d %9.1f %8.1f  %-28s %s' % (imu, r['rows'], r['wall'], hz, gap, verdict))

    print('')
    if bad:
        print('%d 个设备不正常，见上面 !! 那几行' % bad)
        return 1
    print('全部正常')
    return 0


if __name__ == '__main__':
    sys.exit(main())
