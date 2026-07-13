# -*- coding: utf-8 -*-
"""
检测 IMU 数据文件里是否存在周期性缺口
========================================

支持两种输入格式（按文件内容自动识别）:
    1. HICC 离线日志: HH:MM:SS.MS,AX,AY,AZ,GX,GY,GZ（只有时间，没有日期）
    2. Label Studio 格式 CSV: timestamp,acc_x,acc_y,acc_z,gyro_x,gyro_y,gyro_z

做的事情:
    1. 逐行计算相邻时间戳的间隔，取中位数作为"正常采样间隔"
    2. 间隔超过 中位数 x gap_ratio（默认5倍）的地方判定为一次"缺口"
       （HICC 离线日志里同一分钟内秒数倒退的设备端记录异常，按 12 小时
       阈值跟 hicc_offline_to_labelstudio.py 同样的逻辑剔除，不算作缺口）
    3. 统计缺口之间的"复发间隔"（这次缺口到下次缺口隔了多久），如果这些
       复发间隔彼此很接近（变异系数小），说明缺口是有规律地周期性出现，
       而不是偶发的随机丢包

用法:
    python check_periodic_gaps.py data/26071009.TXT
    python check_periodic_gaps.py data/26071009.csv
    python check_periodic_gaps.py data/26071009.TXT --gap-ratio 3 --max-print 30
"""

import argparse
import csv
import statistics
import sys
from datetime import datetime, timedelta

TS_FMT_CSV_MS = '%Y-%m-%d %H:%M:%S.%f'
TS_FMT_CSV_S = '%Y-%m-%d %H:%M:%S'
_MIDNIGHT_WRAP_THRESHOLD = timedelta(hours=12)


def detect_file_type(path: str) -> str:
    with open(path, encoding='utf-8-sig') as f:
        header = f.readline()
    if 'HH:MM:SS' in header:
        return 'hicc_txt'
    if 'timestamp' in header.lower():
        return 'labelstudio_csv'
    raise ValueError(f'无法识别文件格式（表头: {header.strip()!r}），'
                      '应为 HICC 离线日志（HH:MM:SS.MS,...）或 Label Studio CSV（timestamp,...）')


def load_hicc_txt_timestamps(path: str):
    """
    只有 HH:MM:SS.MS，没有日期，用任意固定日期拼接即可（只关心相邻间隔）。
    倒退幅度 >=12小时 视为真正跨午夜，日期+1；倒退幅度很小视为设备端记录
    异常（不是真缺口也不是真跨天），丢弃该行，与 hicc_offline_to_labelstudio.py
    的处理逻辑保持一致，避免这类异常被误判成"缺口"或污染间隔统计。
    """
    base = datetime(2000, 1, 1)
    timestamps = []
    dropped = 0
    prev_dt = None
    day_offset = 0
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if not row:
                continue
            time_str = row[0].strip()
            try:
                h, m, rest = time_str.split(':')
                s, ms = rest.split('.')
                t = base.replace(hour=int(h), minute=int(m), second=int(s),
                                  microsecond=int(ms) * 1000) + timedelta(days=day_offset)
            except ValueError:
                continue

            if prev_dt is not None and t < prev_dt:
                backward = prev_dt - t
                if backward >= _MIDNIGHT_WRAP_THRESHOLD:
                    day_offset += 1
                    t += timedelta(days=1)
                else:
                    dropped += 1
                    continue

            prev_dt = t
            timestamps.append(t)

    return timestamps, dropped


def load_labelstudio_csv_timestamps(path: str):
    timestamps = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            ts = row.get('timestamp', '').strip()
            if not ts:
                continue
            try:
                t = datetime.strptime(ts, TS_FMT_CSV_MS if '.' in ts else TS_FMT_CSV_S)
            except ValueError:
                continue
            timestamps.append(t)
    return timestamps


def find_gaps(timestamps, gap_ratio: float):
    if len(timestamps) < 3:
        return [], 0.0
    diffs_ms = [(timestamps[i] - timestamps[i - 1]).total_seconds() * 1000
                for i in range(1, len(timestamps))]
    sorted_diffs = sorted(diffs_ms)
    median_ms = sorted_diffs[len(sorted_diffs) // 2]
    if median_ms <= 0:
        return [], median_ms

    threshold_ms = median_ms * gap_ratio
    gaps = []
    for i in range(1, len(timestamps)):
        gap_ms = diffs_ms[i - 1]
        if gap_ms > threshold_ms:
            gaps.append((timestamps[i - 1], timestamps[i], gap_ms))
    return gaps, median_ms


def analyze_periodicity(gaps):
    """
    用"这次缺口开始时刻"到"下次缺口开始时刻"的间隔，判断缺口是否周期性出现。

    用中位数 + 绝对中位差(MAD)而不是均值/标准差：少数几个超大间隔（比如
    设备记录异常导致丢弃一大段数据后遗留的巨大缺口）不该主导"是否规律"
    的判断，稳健统计量能更准确反映"大多数缺口"的复发节奏。
    稳健变异系数 = 1.4826 x MAD / median，越小说明间隔越规律；
    <0.15 判定为强周期性，<0.35 判定为有一定规律，否则判定为不规律/偶发。
    """
    if len(gaps) < 3:
        return None
    starts = [g[0] for g in gaps]
    recur_s = [(starts[i] - starts[i - 1]).total_seconds() for i in range(1, len(starts))]
    median_s = statistics.median(recur_s)
    if median_s <= 0:
        return None
    mean_s = statistics.mean(recur_s)
    stdev_s = statistics.stdev(recur_s) if len(recur_s) > 1 else 0.0
    mad = statistics.median([abs(x - median_s) for x in recur_s])
    robust_cv = (1.4826 * mad) / median_s if median_s > 0 else float('inf')
    close_to_median = sum(1 for x in recur_s if abs(x - median_s) <= median_s * 0.2)
    close_ratio = close_to_median / len(recur_s)
    return {
        'count': len(recur_s),
        'robust_cv': robust_cv,
        'close_ratio': close_ratio,
        'mean_s': mean_s,
        'median_s': median_s,
        'stdev_s': stdev_s,
    }


def main():
    ap = argparse.ArgumentParser(description='检测 IMU 数据文件里是否存在周期性缺口')
    ap.add_argument('input', help='HICC 离线 TXT 或 Label Studio CSV 文件路径')
    ap.add_argument('--gap-ratio', type=float, default=5.0,
                     help='判定为缺口的阈值：中位采样间隔的倍数，默认 5')
    ap.add_argument('--max-print', type=int, default=20,
                     help='最多打印多少条缺口明细，默认 20')
    ap.add_argument('--loss-excellent', type=float, default=1.0,
                     help='丢包率(%%) 低于此值判定为"优秀"，默认 1%%')
    ap.add_argument('--loss-warn', type=float, default=3.0,
                     help='丢包率(%%) 低于此值判定为"合格"，默认 3%%（介于 warn 和 fail 之间为"有条件通过"）')
    ap.add_argument('--loss-fail', type=float, default=5.0,
                     help='丢包率(%%) 达到或超过此值判定验收不通过，默认 5%%')
    ap.add_argument('--periodic-cv-threshold', type=float, default=0.15,
                     help='稳健变异系数低于此值判定为"强周期性"，默认 0.15。'
                          '检测到强周期性缺口时，无论丢包率多低，验收结论都直接判定不通过'
                          '（说明是固件/硬件系统性问题，不是偶发噪声）')
    args = ap.parse_args()

    try:
        file_type = detect_file_type(args.input)
    except (ValueError, OSError) as e:
        print(e)
        sys.exit(1)

    if file_type == 'hicc_txt':
        print(f'识别为 HICC 离线日志格式: {args.input}')
        timestamps, dropped = load_hicc_txt_timestamps(args.input)
        if dropped:
            print(f'（已剔除 {dropped} 行设备端记录异常/小幅时间戳倒退，不计入缺口统计）')
    else:
        print(f'识别为 Label Studio CSV 格式: {args.input}')
        timestamps, dropped = load_labelstudio_csv_timestamps(args.input), 0

    if len(timestamps) < 3:
        print('有效数据行太少，无法分析。')
        sys.exit(1)

    total_span_s = (timestamps[-1] - timestamps[0]).total_seconds()
    print(f'总行数: {len(timestamps)}   时间跨度: {total_span_s:.1f}s'
          f'（{timestamps[0].strftime("%H:%M:%S.%f")[:-3]} ~ {timestamps[-1].strftime("%H:%M:%S.%f")[:-3]}）')

    gaps, median_ms = find_gaps(timestamps, args.gap_ratio)
    print(f'正常采样间隔（中位数）: {median_ms:.1f} ms   判定阈值: {median_ms * args.gap_ratio:.1f} ms')
    print()

    total_span_ms = total_span_s * 1000.0

    if not gaps:
        print('未发现明显缺口。')
        print_acceptance_verdict(args, loss_pct=0.0, periodicity_stats=None)
        return

    gap_durations = [g[2] for g in gaps]
    loss_pct = sum(gap_durations) / total_span_ms * 100.0 if total_span_ms > 0 else 0.0
    print(f'── 发现 {len(gaps)} 处缺口 ──')
    print(f'  缺口时长: min={min(gap_durations):.0f}ms  max={max(gap_durations):.0f}ms  '
          f'mean={statistics.mean(gap_durations):.0f}ms  median={statistics.median(gap_durations):.0f}ms')
    print(f'  丢包率: {loss_pct:.2f}%  （缺口总时长 {sum(gap_durations)/1000:.1f}s / 总采集时长 {total_span_s:.1f}s）')
    print()

    for before, after, gap_ms in gaps[:args.max_print]:
        print(f'  {before.strftime("%H:%M:%S.%f")[:-3]}  →  {after.strftime("%H:%M:%S.%f")[:-3]}'
              f'  （缺口约 {gap_ms:.0f} ms）')
    if len(gaps) > args.max_print:
        print(f'  ...（其余 {len(gaps) - args.max_print} 处从略，用 --max-print 调整显示数量）')
    print()

    stats = analyze_periodicity(gaps)
    print('── 周期性分析 ──')
    if stats is None:
        print('缺口数量太少（<3），无法判断是否周期性出现。')
        print_acceptance_verdict(args, loss_pct=loss_pct, periodicity_stats=None)
        return

    print(f'  缺口复发间隔: mean={stats["mean_s"]:.2f}s  median={stats["median_s"]:.2f}s  '
          f'stdev={stats["stdev_s"]:.2f}s  稳健变异系数={stats["robust_cv"]:.3f}  '
          f'（{stats["close_ratio"]*100:.0f}% 的复发间隔落在中位数 ±20% 以内）')
    if stats['robust_cv'] < args.periodic_cv_threshold:
        print(f'  ✔ 强周期性：缺口大约每隔 {stats["median_s"]:.1f} 秒规律性出现一次，'
              f'建议反馈给硬件/固件排查采集端是否存在周期性卡顿。')
    elif stats['robust_cv'] < 0.35:
        print(f'  ⚠ 有一定规律：缺口复发间隔大多集中在 {stats["median_s"]:.1f} 秒左右，'
              f'倾向于周期性但不够严格规律（可能混有个别偶发的大缺口）。')
    else:
        print('  ✘ 未发现明显周期性，缺口出现时间点比较分散/随机，更像偶发丢包。')

    print_acceptance_verdict(args, loss_pct=loss_pct, periodicity_stats=stats)


def print_acceptance_verdict(args, loss_pct: float, periodicity_stats):
    """
    验收结论（阈值可通过 CLI 参数调整，默认值参考行业惯例：
    临床级可穿戴设备 QC 常用 5% 数据缺失作为验收阈值，优化良好的 BLE
    可穿戴系统能做到 <1% 丢包率）：
        丢包率 < --loss-excellent（默认1%）        → 优秀，通过
        丢包率 < --loss-warn（默认3%）              → 合格，通过
        丢包率 < --loss-fail（默认5%）              → 有条件通过，建议关注
        丢包率 >= --loss-fail                       → 不通过
        检测到强周期性缺口（稳健CV < --periodic-cv-threshold）
                                                      → 无论丢包率多低，直接不通过
                                                        （系统性设计缺陷，不是偶发噪声，
                                                         长期使用会持续复现）
    """
    is_periodic = (periodicity_stats is not None
                   and periodicity_stats['robust_cv'] < args.periodic_cv_threshold)

    print()
    print('══ 验收结论 ══')
    print(f'  丢包率: {loss_pct:.2f}%  （优秀<{args.loss_excellent}%  合格<{args.loss_warn}%  '
          f'有条件通过<{args.loss_fail}%  不通过>={args.loss_fail}%）')

    if is_periodic:
        print(f'  判定: ✘ 不通过')
        print(f'  原因: 检测到强周期性缺口（约每 {periodicity_stats["median_s"]:.1f} 秒规律性丢一段，'
              f'当前丢包率 {loss_pct:.2f}%）。规律性说明这是设备固件/硬件存在的系统性问题'
              f'（不是偶发噪声），不会因为多测几次就消失，长期使用会持续复现——'
              f'即便丢包率本身低于阈值也应判定不通过，建议要求厂家整改后重新验收。')
        return

    if loss_pct >= args.loss_fail:
        verdict = '✘ 不通过'
        reason = f'丢包率 {loss_pct:.2f}% 超过不通过阈值 {args.loss_fail}%。'
    elif loss_pct >= args.loss_warn:
        verdict = '⚠ 有条件通过'
        reason = (f'丢包率 {loss_pct:.2f}% 处于 {args.loss_warn}%~{args.loss_fail}% 区间，'
                  f'未达到不通过标准，但建议关注、多测几组数据确认稳定性。')
    elif loss_pct >= args.loss_excellent:
        verdict = '✔ 合格，通过'
        reason = f'丢包率 {loss_pct:.2f}% 低于 {args.loss_warn}%，属于合格范围。'
    else:
        verdict = '✔ 优秀，通过'
        reason = f'丢包率 {loss_pct:.2f}% 低于 {args.loss_excellent}%，数据质量优秀。'

    print(f'  判定: {verdict}')
    print(f'  原因: {reason}')


if __name__ == '__main__':
    main()
