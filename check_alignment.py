# -*- coding: utf-8 -*-
"""
校验 imu_camera_sync.py 输出的视频与 CSV 是否对齐
====================================================

读取 {base}.mp4 和 {base}.csv（Label Studio 格式），对比：
    - 帧数 vs CSV 行数（应严格相等，一帧一行）
    - 视频时长 vs CSV 时间戳覆盖的时长
    - 视频/CSV 的起始时间、结束时间

视频文件本身不含绝对时间戳，起止时间通过以下方式确定（优先级从高到低）：
    1. 同目录下的 {base}_meta.csv（若存在）：直接读取第一/最后一行的 cam_timestamp，
       这是录制时每一帧真实的系统时间，最准确。
    2. 文件名里的时间戳（形如 ..._20260703_105514）作为起始时间，
       再加上 视频时长(帧数/fps) 推算结束时间（仅精确到秒，且若未做 fps 修正会有误差）。

用法:
    python check_alignment.py data/wit_d534e2b96f32_20260703_105514
    python check_alignment.py data/wit_d534e2b96f32_20260703_105514.mp4
    python check_alignment.py data/wit_d534e2b96f32_20260703_105514.csv
"""

import csv
import os
import re
import sys
from datetime import datetime, timedelta

try:
    import cv2
except ImportError:
    print('缺少 opencv-python，请先安装: pip install opencv-python')
    sys.exit(1)

TS_FMT = '%Y-%m-%d %H:%M:%S.%f'
# 兼容两种精度：旧文件名时间戳是6位秒级 HHMMSS，新文件名是9位毫秒级
# HHMMSSmmm（避免 --loop 循环录制文件名撞车）。
FNAME_TS_RE = re.compile(r'(\d{8}_\d{6}(?:\d{3})?)$')


def _resolve_base(arg: str) -> str:
    for suffix in ('.mp4', '.csv', '_meta.csv'):
        if arg.endswith(suffix):
            return arg[: -len(suffix)]
    return arg


def _read_csv_timestamps(csv_path: str, column: str):
    timestamps = []
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            ts = row.get(column, '').strip()
            if not ts:
                continue
            try:
                timestamps.append(datetime.strptime(ts, TS_FMT))
            except ValueError:
                continue
    return timestamps


def check_video_frames(video_path: str):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f'无法打开视频: {video_path}')
        return None
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    duration = frame_count / fps if fps > 0 else 0.0
    return {'frame_count': frame_count, 'fps': fps, 'duration': duration}


def resolve_video_start_end(base: str, video_duration: float, meta_base: str = None):
    """
    返回 (start, end, source)。优先用 _meta.csv 的真实 cam_timestamp，
    没有的话退化为文件名时间戳 + 视频时长估算。

    meta_base: 若指定，用这个 base 去找 _meta.csv（用于校验 resampled 版本时，
    其真实的逐帧时间戳记录在原始录制的 {base}_meta.csv 里，而不是
    resampled_base 自己名下）。
    """
    meta_path = f'{meta_base or base}_meta.csv'
    if os.path.exists(meta_path):
        cam_ts = _read_csv_timestamps(meta_path, 'cam_timestamp')
        if cam_ts:
            return cam_ts[0], cam_ts[-1], f'{os.path.basename(meta_path)} (逐帧真实时间戳)'

    m = FNAME_TS_RE.search(base)
    if m:
        raw = m.group(1)
        date_part, time_part = raw.split('_')
        hhmmss, ms = time_part[:6], time_part[6:9]
        start = datetime.strptime(date_part + hhmmss, '%Y%m%d%H%M%S')
        if ms:
            start = start.replace(microsecond=int(ms) * 1000)
        end = start + timedelta(seconds=video_duration)
        return start, end, '文件名时间戳 + 视频时长推算（仅供参考，精度较低）'

    return None, None, None


def run_check(base: str, meta_base: str = None, strict_frame_match: bool = True) -> bool:
    """
    执行校验并打印结果，返回 True 表示各项检查都通过。

    strict_frame_match: 按视频帧对齐的 {base}.csv 应设 True（帧数必须等于行数）；
    降采样后的 {base}_resampled{HZ}hz.csv 行数由目标频率决定，跟视频帧数无关，
    应设 False（只检查时长/起止时间，不检查帧数=行数）。
    meta_base: 校验 resampled 版本时，传原始录制的 base，用于定位真实的 _meta.csv。
    """
    video_path = f'{base}.mp4'
    csv_path   = f'{base}.csv'

    video_info = check_video_frames(video_path)
    csv_ts     = _read_csv_timestamps(csv_path, 'timestamp')

    if video_info is None or not csv_ts:
        print('校验失败：视频或 CSV 无法读取，请检查路径。')
        return False

    csv_start, csv_end = csv_ts[0], csv_ts[-1]
    csv_duration = (csv_end - csv_start).total_seconds()
    csv_row_count = len(csv_ts)

    video_start, video_end, source = resolve_video_start_end(base, video_info['duration'], meta_base=meta_base)

    print(f'【视频】{video_path}')
    print(f'  帧数:   {video_info["frame_count"]}')
    print(f'  fps:    {video_info["fps"]:.2f}')
    print(f'  时长:   {video_info["duration"]:.2f}s')
    if video_start is not None:
        print(f'  起始:   {video_start.strftime(TS_FMT)[:-3]}')
        print(f'  结束:   {video_end.strftime(TS_FMT)[:-3]}')
        print(f'  （起止时间来源: {source}）')
    else:
        print('  起止时间: 无法确定（文件名不含时间戳，且无 _meta.csv）')
    print()

    print(f'【CSV】{csv_path}')
    print(f'  行数:   {csv_row_count}')
    print(f'  起始:   {csv_start.strftime(TS_FMT)[:-3]}')
    print(f'  结束:   {csv_end.strftime(TS_FMT)[:-3]}')
    print(f'  时长:   {csv_duration:.2f}s')
    print()

    duration_diff = abs(video_info['duration'] - csv_duration)
    ok = True

    print('── 对齐结果 ──')
    if strict_frame_match:
        frame_diff = video_info['frame_count'] - csv_row_count
        if frame_diff == 0:
            print(f'✔ 帧数与 CSV 行数一致: {video_info["frame_count"]}')
        else:
            ok = False
            print(f'✘ 帧数与 CSV 行数不一致: 视频 {video_info["frame_count"]} 帧, CSV {csv_row_count} 行 (差 {frame_diff})')
    else:
        print(f'ℹ 降采样模式：行数由目标频率决定，不要求等于视频帧数（视频 {video_info["frame_count"]} 帧, CSV {csv_row_count} 行）')

    if duration_diff <= 0.5:
        print(f'✔ 时长基本一致: 视频 {video_info["duration"]:.2f}s vs CSV {csv_duration:.2f}s (差 {duration_diff:.3f}s)')
    else:
        ok = False
        print(f'✘ 时长差异较大: 视频 {video_info["duration"]:.2f}s vs CSV {csv_duration:.2f}s (差 {duration_diff:.3f}s)')
        print('  （若未做 fps 元数据修正，视频用固定 fps 编码，播放时长可能与真实时长有出入，'
              '但只要帧数与 CSV 行数一致，帧对齐本身依然是准确的。）')

    if video_start is not None:
        start_diff = abs((video_start - csv_start).total_seconds())
        end_diff = abs((video_end - csv_end).total_seconds())
        if start_diff <= 0.5 and end_diff <= 0.5:
            print(f'✔ 起止时间基本一致（起始差 {start_diff:.3f}s，结束差 {end_diff:.3f}s）')
        else:
            ok = False
            print(f'✘ 起止时间差异较大（起始差 {start_diff:.3f}s，结束差 {end_diff:.3f}s）')

    return ok


def main():
    if len(sys.argv) != 2:
        print('用法: python check_alignment.py <base 或 .mp4 或 .csv 路径>')
        sys.exit(1)
    base = _resolve_base(sys.argv[1])
    strict = '_resampled' not in os.path.basename(base)
    meta_base = re.sub(r'_resampled[\d.]+hz$', '', base) if not strict else None
    ok = run_check(base, meta_base=meta_base, strict_frame_match=strict)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
