#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 --loop 循环录制产生的一堆按小段切分的 resampled mp4/csv 合并成每小时一份
（Python 版，替代 merge_hourly_segments.sh）。

比 shell 版多做的事:
    1. 合并前先逐段核对每个视频的实际时长（用 opencv 读帧数/fps）跟对应
       CSV 的时间跨度是否对得上，打印一张诊断表——如果某一分钟的视频明显
       比CSV短，说明问题出在录制那一刻（比如那一分钟摄像头掉帧/被压得只
       写了几秒），不是合并脚本的锅，这张表能直接定位是哪一段。
    2. 合并结束后对比"合并后视频总时长" vs "合并后CSV总时长"，明显不一致
       会打印警告，而不是静默生成一个时长对不上的文件。
    3. 处理过程有进度条（按段数算，不需要额外装 tqdm）。
    4. 用 Python 原生路径处理（os.path.abspath），不会有 Windows/Git Bash
       下 pwd 输出 /c/Users/... 这种 POSIX 风格路径导致 ffmpeg.exe 解析出
       "C:/c/Users/..." 双重盘符报错的问题（shell 版就是栽在这个坑上）。

识别的文件名格式（跟 shell 版一致）:
    {前缀}_{YYYYMMDD}_{HHMMSS}_{camX_imuY}_resampled{HZ}hz.mp4/.csv
按 日期+小时 + camX_imuY + 频率 分组，同一组内按时间顺序合并成:
    {前缀}_{YYYYMMDD}{HH}_{camX_imuY}_resampled{HZ}hz.mp4/.csv

用法:
    # 合并，默认不改动源目录，结果存到 <目录>/merged
    python merge_hourly_segments.py data/multicam_multiimu

    # 指定输出目录
    python merge_hourly_segments.py data/multicam_multiimu --out-dir data/multicam_multiimu_merged

    # 合并成功后删除参与合并的原始小段（默认不删）
    python merge_hourly_segments.py data/multicam_multiimu --delete-originals

依赖: ffmpeg（合并mp4用 concat demuxer + -c copy，不重新编码）；opencv-python（可选，
      用于逐段时长诊断，没装的话跳过诊断表直接合并）
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

try:
    import cv2
except ImportError:
    cv2 = None

# 时间字段兼容两种精度：旧文件是 HHMMSS（6位，秒级），新文件是 HHMMSSmmm
# （9位，精确到毫秒，避免 --loop 循环录制文件名撞车）。
FNAME_RE = re.compile(
    r'^(?P<prefix>.+)_(?P<date>\d{8})_(?P<time>\d{6}(?:\d{3})?)_(?P<combo>cam\d+_imu\d+)_resampled(?P<hz>[\d.]+)hz$'
)
_TS_FMT_CANDIDATES = ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S')


def print_progress(done: int, total: int, prefix: str = ''):
    width = 30
    frac = (done / total) if total else 1.0
    filled = int(width * frac)
    bar = '#' * filled + '-' * (width - filled)
    end = '\n' if done >= total else ''
    print(f'\r{prefix}[{bar}] {done}/{total} ({frac * 100:5.1f}%)', end=end, flush=True)


def video_duration_sec(path: str):
    """用 opencv 读帧数/fps 估算视频实际时长（秒）；没装 opencv 或读取失败返回 None。"""
    if cv2 is None:
        return None
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    if fps and fps > 0 and frames and frames > 0:
        return frames / fps
    return None


def _parse_ts(s: str):
    s = s.strip()
    for fmt in _TS_FMT_CANDIDATES:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def csv_time_range(path: str):
    """返回 (起始时间, 结束时间, 行数)；解析失败返回 (None, None, 0)。"""
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            next(reader, None)  # 表头
            rows = [r for r in reader if r and r[0].strip()]
    except OSError:
        return None, None, 0
    if not rows:
        return None, None, 0
    t0 = _parse_ts(rows[0][0])
    t1 = _parse_ts(rows[-1][0])
    return t0, t1, len(rows)


def scan_groups(directory: str):
    """返回 { (prefix, datehour, combo, hz): {'mp4': [(dt, path), ...], 'csv': [...]} }，
    每个内层列表已按时间排序。"""
    groups = defaultdict(lambda: {'mp4': [], 'csv': []})
    for fname in sorted(os.listdir(directory)):
        stem, ext = os.path.splitext(fname)
        ext = ext.lstrip('.').lower()
        if ext not in ('mp4', 'csv'):
            continue
        m = FNAME_RE.match(stem)
        if not m:
            continue
        date, time_, combo, hz, prefix = m['date'], m['time'], m['combo'], m['hz'], m['prefix']
        key = (prefix, date + time_[:2], combo, hz)
        hhmmss, ms = time_[:6], time_[6:9]
        try:
            dt = datetime.strptime(date + hhmmss, '%Y%m%d%H%M%S')
            if ms:
                dt = dt.replace(microsecond=int(ms) * 1000)
        except ValueError:
            continue
        groups[key][ext].append((dt, os.path.join(directory, fname)))

    for key in groups:
        groups[key]['mp4'].sort(key=lambda x: x[0])
        groups[key]['csv'].sort(key=lambda x: x[0])
    return groups


def diagnose_group(mp4_files, csv_files):
    """打印每一段视频实际时长 vs 对应CSV时间跨度的对照表，帮助定位哪一段有问题。
    按时间戳配对（同一段录制的mp4/csv文件名除后缀外完全一致）。

    返回 (total_video_dur, total_csv_dur, mismatched_dts)，mismatched_dts 是
    判定为"视频/CSV时长明显不一致"的那些段的时间戳集合，供调用方决定要不要
    跳过这些段不参与合并（--skip-mismatched）。"""
    csv_by_dt = {dt: path for dt, path in csv_files}
    print(f'  {"时间戳":<17} {"视频时长":>10} {"CSV跨度":>10} {"CSV行数":>8}  状态')
    total_video_dur = 0.0
    total_csv_dur = 0.0
    mismatched_dts = set()
    for dt, mp4_path in mp4_files:
        vdur = video_duration_sec(mp4_path)
        csv_path = csv_by_dt.get(dt)
        cdur = None
        crows = 0
        if csv_path:
            t0, t1, crows = csv_time_range(csv_path)
            if t0 and t1:
                cdur = (t1 - t0).total_seconds()
        vdur_s = f'{vdur:.1f}s' if vdur is not None else '未知'
        cdur_s = f'{cdur:.1f}s' if cdur is not None else '未知'
        status = '✔'
        if vdur is not None and cdur is not None:
            total_video_dur += vdur
            total_csv_dur += cdur
            if cdur > 0 and abs(vdur - cdur) / cdur > 0.15:
                status = '✘ 视频/CSV时长明显不一致（这一段录制本身可能有问题，不是合并脚本的锅）'
                mismatched_dts.add(dt)
        ts_str = dt.strftime('%Y%m%d_%H%M%S')
        print(f'  {ts_str:<17} {vdur_s:>10} {cdur_s:>10} {crows:>8}  {status}')
    return total_video_dur, total_csv_dur, mismatched_dts


def merge_mp4(files, out_path: str) -> tuple[bool, str]:
    listfile = out_path + '.concat_list.txt'
    with open(listfile, 'w', encoding='utf-8') as f:
        for _, path in files:
            # 用 Python 自己的 abspath，不经过 shell/pwd，天然没有 Git Bash 下
            # POSIX 风格路径导致 ffmpeg.exe 解析出双重盘符的问题。
            abspath = os.path.abspath(path).replace('\\', '/')
            f.write(f"file '{abspath}'\n")
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0',
           '-i', listfile, '-c', 'copy', out_path]
    try:
        result = subprocess.run(cmd, capture_output=True)
        ok = result.returncode == 0
        err = result.stderr.decode(errors='replace')
    except FileNotFoundError:
        ok, err = False, '找不到 ffmpeg，请先安装并加入 PATH。'
    finally:
        try:
            os.remove(listfile)
        except OSError:
            pass
    return ok, err


def merge_csv(files, out_path: str):
    with open(out_path, 'w', newline='', encoding='utf-8-sig') as out_f:
        writer = None
        for _, path in files:
            with open(path, newline='', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is None:
                    continue
                if writer is None:
                    writer = csv.writer(out_f)
                    writer.writerow(header)
                for row in reader:
                    if row:
                        writer.writerow(row)


def main():
    ap = argparse.ArgumentParser(description='把 --loop 循环录制的小段 resampled mp4/csv 合并成每小时一份')
    ap.add_argument('directory', help='源目录（只读，不会被修改，除非加 --delete-originals）')
    ap.add_argument('--out-dir', default=None,
                     help='合并结果输出目录，默认 <目录>/merged（新建子目录，不会覆盖源文件）')
    ap.add_argument('--delete-originals', action='store_true',
                     help='合并成功后删除参与合并的原始小段文件（默认不删）')
    ap.add_argument('--mismatch-only', action='store_true',
                     help='只打印诊断表，不做合并（用来先排查哪些段有时长不一致的问题）')
    ap.add_argument('--skip-mismatched', action='store_true',
                     help='合并时跳过诊断表里标✘的段（视频/CSV时长明显不一致的那些），'
                          '只合并没问题的段；被跳过的原始文件不受影响，即使加了 --delete-originals 也不会删')
    args = ap.parse_args()

    directory = args.directory
    if not os.path.isdir(directory):
        print(f'目录不存在: {directory}')
        sys.exit(1)

    out_dir = args.out_dir or os.path.join(directory, 'merged')

    if cv2 is None:
        print('提示: 未安装 opencv-python，跳过逐段视频时长诊断（仍会正常合并）。')

    groups = scan_groups(directory)
    if not groups:
        print(f'在 {directory} 里没找到符合命名规则的 resampled mp4/csv 文件'
              '（{前缀}_YYYYMMDD_HHMMSS_camX_imuY_resampledHZhz.mp4/.csv）。')
        return

    print(f'原始数据目录: {directory}（只读，不会修改）')
    if not args.mismatch_only:
        os.makedirs(out_dir, exist_ok=True)
        print(f'合并结果输出目录: {out_dir}')
    print()

    sorted_keys = sorted(groups.keys())
    total_groups = len(sorted_keys)

    for gi, key in enumerate(sorted_keys, start=1):
        prefix, datehour, combo, hz = key
        mp4_files = groups[key]['mp4']
        csv_files = groups[key]['csv']
        n_mp4, n_csv = len(mp4_files), len(csv_files)

        print(f'── {combo} ({datehour}, {hz}Hz) ── {n_mp4} 个视频段 / {n_csv} 个CSV段')
        total_video_dur, total_csv_dur, mismatched_dts = diagnose_group(mp4_files, csv_files)
        if total_video_dur and total_csv_dur:
            diff_pct = abs(total_video_dur - total_csv_dur) / total_csv_dur * 100 if total_csv_dur else 0
            flag = '  ⚠ 合计时长差异较大，建议看上面哪一段标了✘' if diff_pct > 15 else ''
            print(f'  合计: 视频 {total_video_dur:.1f}s  CSV {total_csv_dur:.1f}s'
                  f'（差 {diff_pct:.1f}%）{flag}')

        if args.mismatch_only:
            print()
            print_progress(gi, total_groups, prefix='总进度 ')
            continue

        skipped = []
        if args.skip_mismatched and mismatched_dts:
            skipped = sorted(dt.strftime('%Y%m%d_%H%M%S') for dt in mismatched_dts)
            mp4_files = [(dt, p) for dt, p in mp4_files if dt not in mismatched_dts]
            csv_files = [(dt, p) for dt, p in csv_files if dt not in mismatched_dts]
            print(f'  --skip-mismatched: 跳过 {len(skipped)} 段（{", ".join(skipped)}），'
                  f'只合并剩下 {len(mp4_files)} 个视频段 / {len(csv_files)} 个CSV段')

        if not mp4_files and not csv_files:
            print('  跳过后没有可合并的文件了，本组不生成合并结果。')
            print()
            print_progress(gi, total_groups, prefix='总进度 ')
            continue

        out_mp4 = os.path.join(out_dir, f'{prefix}_{datehour}_{combo}_resampled{hz}hz.mp4')
        out_csv = os.path.join(out_dir, f'{prefix}_{datehour}_{combo}_resampled{hz}hz.csv')

        merged_ok = True
        if mp4_files:
            ok, err = merge_mp4(mp4_files, out_mp4)
            if ok:
                actual_dur = video_duration_sec(out_mp4)
                dur_str = f'{actual_dur:.1f}s' if actual_dur is not None else '未知'
                print(f'  ✔ 视频已合并: {os.path.basename(out_mp4)}（实际时长 {dur_str}）')
            else:
                merged_ok = False
                print(f'  ✘ 视频合并失败: {err.strip()}')

        if csv_files:
            merge_csv(csv_files, out_csv)
            t0, t1, n_rows = csv_time_range(out_csv)
            span = f'{(t1 - t0).total_seconds():.1f}s' if t0 and t1 else '未知'
            print(f'  ✔ CSV已合并: {os.path.basename(out_csv)}（{n_rows} 行，跨度 {span}）')

        if merged_ok and args.delete_originals:
            for _, path in mp4_files + csv_files:
                try:
                    os.remove(path)
                except OSError as e:
                    print(f'  删除 {path} 失败: {e}')
            print(f'  已删除 {n_mp4 + n_csv} 个原始小段文件')

        print()
        print_progress(gi, total_groups, prefix='总进度 ')

    print()
    print('全部处理完成。')
    if not args.mismatch_only and not args.delete_originals:
        print('（原始小段文件已保留，确认合并结果没问题后可以手动删除，或加 --delete-originals 自动删除）')


if __name__ == '__main__':
    main()
