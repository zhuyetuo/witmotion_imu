#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 imu_train (https://github.com/zhuyetuo/imu_train) 里 src/infer_csv_scratch.py
批量推理"抓挠"行为识别出来的时间段，从对应的视频里剪出来，方便人工过一遍确认
模型判断得准不准。

背景:
    imu_train 的 infer_csv_scratch.py 对每个 resampled CSV 跑完推理后，会在
    终端打印类似这样的内容（只把这段输出保存下来，本脚本不调用 imu_train，
    也不需要装它的依赖）:

        ── multicam_20260719_160715_cam1_imu1_resampled16hz.csv ──
          【汇总】总窗口=598  抓挠窗口=17  (2.8%)
          【片段】16:07:24→16:07:25
          【合并】16:07:24→16:07:25

    "【合并】" 那一行就是本脚本要提取的时间段（同一个文件可能有多段，
    空格分隔）。CSV 文件名跟视频文件名同名（去掉扩展名一致，
    witmotion_imu 的 multicam 系列脚本本来就是这么配对生成的），本脚本用
    CSV 自己第一行的 timestamp 当作视频起始时间的锚点（因为是逐帧同步生成
    的 resampled 文件，起始时刻跟配对视频一致），换算出每段抓挠时间在视频
    里的偏移量，再用 ffmpeg 剪出来。

用法:
    # 先把 infer_csv_scratch.py 的输出保存成文件
    python src/infer_csv_scratch.py --csv_dir data/raw_wit/ --pattern "*.csv" \\
        --model results/.../ml_rf.pkl --device_hz 16 --scratch_only --quiet --workers 8 \\
        > scratch_log.txt

    # 再用这个脚本剪视频（--csv-dir 找CSV读起始时间戳，--video-dir 找配对的mp4）
    python clip_scratch_segments.py scratch_log.txt \\
        --csv-dir data/raw_wit --video-dir data/raw_wit --out-dir clips

    # 前后各留2秒上下文（默认），改成留5秒
    python clip_scratch_segments.py scratch_log.txt --csv-dir data/raw_wit --pad-sec 5

    # 只看会剪出哪些片段，不真的跑 ffmpeg
    python clip_scratch_segments.py scratch_log.txt --csv-dir data/raw_wit --dry-run

依赖: ffmpeg
"""

import argparse
import csv
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

HEADER_RE = re.compile(r'^──\s+(?P<fname>\S+\.csv)\s+──$')
MERGED_RE = re.compile(r'^\s*【合并】\s*(?P<segs>.+)$')
PAIR_RE = re.compile(r'(\d{2}:\d{2}:\d{2})→(\d{2}:\d{2}:\d{2})')
DATE_IN_FNAME_RE = re.compile(r'_(\d{8})_\d{6}(?:\d{3})?_')
TS_FMT_CANDIDATES = ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S')

# multicam 系列脚本的降采样配对文件名格式: {session前缀}_camX_imuY_resampled{HZ}hz
# session前缀通常是 {设备/前缀}_{日期}_{时间}，同一次录制里不同摄像头/设备的
# 配对文件共用同一个 session前缀，只有 camX_imuY 不一样。
SESSION_CAM_RE = re.compile(r'^(?P<session>.+)_(?P<cam>cam\d+)_(?P<imu>imu\d+)_resampled(?P<hz>[\d.]+)hz$')


def parse_log(lines):
    """解析 infer_csv_scratch.py 的终端输出（进度条那些行会被忽略），
    返回 { csv_filename: [(start_time, end_time), ...] }，start_time/end_time
    是 datetime.time 对象（只有时分秒，没有日期——日期从文件名里的 YYYYMMDD 取）。"""
    result = {}
    current_fname = None
    for raw_line in lines:
        line = raw_line.rstrip('\n')
        m = HEADER_RE.match(line.strip())
        if m:
            current_fname = m['fname']
            continue
        m = MERGED_RE.match(line)
        if m and current_fname:
            pairs = PAIR_RE.findall(m['segs'])
            if pairs:
                segs = [(datetime.strptime(a, '%H:%M:%S').time(),
                         datetime.strptime(b, '%H:%M:%S').time())
                        for a, b in pairs]
                result.setdefault(current_fname, []).extend(segs)
    return result


def find_file(directory: str, filename: str):
    """先直接拼路径找，找不到再递归搜一遍（imu_train 的 csv_dir 可能有子目录）。"""
    direct = os.path.join(directory, filename)
    if os.path.exists(direct):
        return direct
    for root, _, files in os.walk(directory):
        if filename in files:
            return os.path.join(root, filename)
    return None


def csv_first_timestamp(csv_path: str):
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if row and row[0].strip():
                for fmt in TS_FMT_CANDIDATES:
                    try:
                        return datetime.strptime(row[0].strip(), fmt)
                    except ValueError:
                        continue
    return None


def _read_lines_any_encoding(path: str):
    """Windows 上把命令行输出重定向到文件（> log.txt）时，Python 控制台默认
    按系统代码页编码（中文Windows是 GBK/CP936），不是 UTF-8——日志里的中文
    "【合并】"这些内容用 UTF-8 解码会直接报 UnicodeDecodeError。这里按
    utf-8 -> utf-8-sig -> gbk 依次尝试，都不行最后用 utf-8 + errors='replace'
    兜底（不会因为编码问题直接崩溃，最多个别乱码字符不影响正则匹配时间戳）。"""
    for enc in ('utf-8', 'utf-8-sig', 'gbk'):
        try:
            with open(path, encoding=enc) as f:
                return f.readlines()
        except UnicodeDecodeError:
            continue
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.readlines()


def find_sibling_camera_videos(video_dir: str, session: str, hz: str, primary_cam: str, primary_video: str):
    """
    同一次多摄像头录制里，所有摄像头是在同一个"tick"事件驱动循环里同步抓帧的
    （imu_camera_sync_multicam.py / imu_camera_sync_rtsp_multicam.py 的设计），
    所以同一个 session 前缀下，不同 camX 的视频起始时刻是同一时刻，共用同一个
    时间锚点——不需要分别去读每个摄像头自己的CSV，识别出来的时间段直接对
    所有摄像头视频生效，这样多视角复核同一个动作会方便很多。

    返回 { camX: 视频路径 }，包含 primary_cam 自己。同一个 camX 可能因为配了
    多个IMU设备而存在好几份内容完全相同的视频拷贝（camX_imu1/camX_imu2...），
    每个camX只取找到的第一份即可。
    """
    result = {primary_cam: primary_video}
    prefix = f'{session}_cam'
    suffix = f'_resampled{hz}hz.mp4'
    for root, _, files in os.walk(video_dir):
        for fname in files:
            if not (fname.startswith(prefix) and fname.endswith(suffix)):
                continue
            stem = fname[:-len('.mp4')]
            m = SESSION_CAM_RE.match(stem)
            if not m or m['session'] != session or m['hz'] != hz:
                continue
            cam = m['cam']
            if cam not in result:
                result[cam] = os.path.join(root, fname)
    return result


def combine_date_time(date_str: str, t) -> datetime:
    """date_str: 'YYYYMMDD'；t: datetime.time。日期+时分秒拼成完整 datetime。"""
    base = datetime.strptime(date_str, '%Y%m%d')
    return base.replace(hour=t.hour, minute=t.minute, second=t.second)


def cut_clip(video_path: str, out_path: str, offset_sec: float, duration_sec: float,
             use_copy: bool) -> tuple[bool, str]:
    if use_copy:
        codec_args = ['-c', 'copy']
    else:
        codec_args = ['-c:v', 'libx264', '-preset', 'fast', '-crf', '20', '-c:a', 'copy']
    cmd = [
        'ffmpeg', '-y', '-loglevel', 'error',
        '-ss', f'{offset_sec:.3f}', '-i', video_path,
        '-t', f'{duration_sec:.3f}',
        *codec_args,
        out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True)
        return result.returncode == 0, result.stderr.decode(errors='replace')
    except FileNotFoundError:
        return False, '找不到 ffmpeg，请先安装并加入 PATH。'


def main():
    ap = argparse.ArgumentParser(
        description='把 infer_csv_scratch.py 输出里识别到的抓挠时间段从对应视频剪出来')
    ap.add_argument('log', help='infer_csv_scratch.py 的终端输出保存成的文本文件（或用 "-" 从标准输入读）')
    ap.add_argument('--csv-dir', required=True,
                     help='resampled CSV 所在目录（用来读每个文件的起始时间戳锚点），会递归查找')
    ap.add_argument('--video-dir', default=None,
                     help='配对视频所在目录，默认跟 --csv-dir 一样（视频与CSV同名，扩展名 .mp4）')
    ap.add_argument('--out-dir', default='clips', help='剪出来的片段存放目录，默认 clips/')
    ap.add_argument('--pad-sec', type=float, default=2.0,
                     help='每段前后各留多少秒上下文，默认2秒（避免刚好卡在动作边缘看不清）')
    ap.add_argument('--copy', action='store_true',
                     help='用 -c copy 快速裁剪（不重新编码，速度快，但切点会被吸附到最近的关键帧，'
                          '实际起止时间可能有0~几秒误差）；默认重新编码，切点精确，速度慢一些')
    ap.add_argument('--dry-run', action='store_true', help='只打印会剪出哪些片段，不真的跑 ffmpeg')
    ap.add_argument('--no-multi-view', action='store_true',
                     help='默认会把同一次录制里其它摄像头（camX）在同一时间段的画面也一起剪出来，'
                          '方便多视角复核同一个动作；加这个参数关掉，只剪日志里那个文件本身对应的'
                          '那一路摄像头。')
    args = ap.parse_args()

    video_dir = args.video_dir or args.csv_dir

    if args.log == '-':
        lines = sys.stdin.readlines()
    else:
        if not os.path.isfile(args.log):
            print(f'日志文件不存在: {args.log}')
            sys.exit(1)
        lines = _read_lines_any_encoding(args.log)

    segments_by_file = parse_log(lines)
    if not segments_by_file:
        print('没有从日志里解析到任何 "【合并】" 时间段，确认传入的是 infer_csv_scratch.py 的完整输出。')
        return

    if not args.dry_run:
        os.makedirs(args.out_dir, exist_ok=True)

    total_clips = 0
    total_ok = 0
    for fname, segs in segments_by_file.items():
        m = DATE_IN_FNAME_RE.search(fname)
        if not m:
            print(f'⚠ 跳过 {fname}：文件名里找不到 YYYYMMDD 日期，没法把日志里的 HH:MM:SS 换算成完整时间')
            continue
        date_str = m.group(1)

        csv_path = find_file(args.csv_dir, fname)
        if not csv_path:
            print(f'⚠ 跳过 {fname}：在 {args.csv_dir} 里找不到这个CSV文件')
            continue
        video_start = csv_first_timestamp(csv_path)
        if video_start is None:
            print(f'⚠ 跳过 {fname}：读不到CSV第一行的时间戳')
            continue

        stem = os.path.splitext(fname)[0]
        video_name = stem + '.mp4'
        video_path = find_file(video_dir, video_name)
        if not video_path:
            print(f'⚠ 跳过 {fname}：在 {video_dir} 里找不到配对视频 {video_name}')
            continue

        # 默认还会把同一次录制里其它摄像头的画面也一起剪出来（多视角复核）——
        # 同一个 session 下所有摄像头是在同一个tick循环里同步抓帧的，起始时刻
        # 相同，识别出来的时间段可以直接套用到每一路摄像头，不用分别读各自的
        # CSV时间戳。cams_to_clip: {camX: (视频路径, 用于命名的文件stem)}
        session_m = SESSION_CAM_RE.match(stem)
        if session_m and not args.no_multi_view:
            sibling_map = find_sibling_camera_videos(
                video_dir, session_m['session'], session_m['hz'], session_m['cam'], video_path)
            cams_to_clip = {}
            for cam, path in sibling_map.items():
                sib_stem = os.path.splitext(os.path.basename(path))[0]
                cams_to_clip[cam] = (path, sib_stem)
            if len(cams_to_clip) > 1:
                other_cams = [c for c in cams_to_clip if c != session_m['cam']]
                print(f'  （多视角：同一session下还找到 {", ".join(other_cams)} 的视频，会一起剪）')
        else:
            cams_to_clip = {'': (video_path, stem)}

        print(f'── {fname} ── {len(segs)} 段')
        for i, (t_start, t_end) in enumerate(segs, start=1):
            seg_start = combine_date_time(date_str, t_start)
            seg_end = combine_date_time(date_str, t_end)
            if seg_end < seg_start:
                # 跨过午夜的极少数情况
                seg_end += timedelta(days=1)

            padded_start = max(seg_start - timedelta(seconds=args.pad_sec), video_start)
            padded_end = seg_end + timedelta(seconds=args.pad_sec)

            offset = (padded_start - video_start).total_seconds()
            duration = (padded_end - padded_start).total_seconds()
            if offset < 0 or duration <= 0:
                print(f'  [{i}] {t_start}→{t_end}  ⚠ 算出来的偏移/时长不合理（offset={offset:.1f}s '
                      f'duration={duration:.1f}s），跳过，检查CSV起始时间戳是否正确')
                continue

            for cam, (cam_video_path, cam_stem) in cams_to_clip.items():
                out_name = f'{cam_stem}_clip{i:02d}_{t_start.strftime("%H%M%S")}-{t_end.strftime("%H%M%S")}.mp4'
                out_path = os.path.join(args.out_dir, out_name)
                total_clips += 1

                if args.dry_run:
                    print(f'  [{i}] {t_start}→{t_end}  offset={offset:.1f}s  duration={duration:.1f}s  '
                          f'→ {out_name}（--dry-run，未真正剪辑）')
                    continue

                ok, err = cut_clip(cam_video_path, out_path, offset, duration, args.copy)
                if ok:
                    total_ok += 1
                    print(f'  [{i}] {t_start}→{t_end}  ✔ {out_name}')
                else:
                    print(f'  [{i}] {t_start}→{t_end}  ✘ 剪辑失败: {err.strip()}')

    print()
    if args.dry_run:
        print(f'共 {total_clips} 段（--dry-run，未真正剪辑）')
    else:
        print(f'共 {total_clips} 段，成功剪出 {total_ok} 段 → {args.out_dir}/')


if __name__ == '__main__':
    main()
