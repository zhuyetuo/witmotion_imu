# -*- coding: utf-8 -*-
"""
多个摄像头 + 多个 IMU 设备 同步采集脚本
==========================================

在 imu_camera_sync_multi.py（一个摄像头+多IMU）的基础上再扩展一维：支持
同时开多路摄像头，每路摄像头独立写视频，所有摄像头 + 所有 IMU 设备共用
同一份"每帧一行"的组合 CSV（同一时刻抓取所有摄像头的画面 + 匹配所有 IMU
设备最近的样本）。

v1 版本先做核心录制功能，暂不包含 imu_camera_sync.py / imu_camera_sync_multi.py
里的 --loop / --resample-hz / --probe / --resample-only 这些高级选项——
先跑通"多摄像头+多IMU"这一层同步，验证没问题后再按需搬过来。

用法:
    # 2个摄像头 + 2个IMU设备
    python imu_camera_sync_multicam.py --camera 0 --camera 1 \\
        --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --duration 60

--camera 可重复传，第一个对应 cam1，第二个对应 cam2，以此类推。
--imu 用法跟 imu_camera_sync_multi.py 完全一样（type=标识，可重复传）。

输出:
    {base}_cam1.mp4, {base}_cam2.mp4...       每路摄像头各自的视频（VFR，含叠加信息）
    {base}.csv                                 每个"tick"一行：timestamp, imu1_acc_x...
                                                （所有 IMU 设备的 acc/gyro，按 --imu 顺序）
    {base}_meta.csv                            每行的对齐信息：各摄像头的 fps，各IMU设备的
                                                lag_ms/missing/hz
    {base}_imu1_raw.csv, {base}_imu2_raw.csv... 各 IMU 设备的原始全量流水
"""

import argparse
import csv
import os
import shutil
import sys
import threading
import time
from datetime import datetime

try:
    import cv2
except ImportError:
    print('缺少 opencv-python，请先安装: pip install opencv-python')
    sys.exit(1)

from imu_camera_sync import _FfmpegVfrSink, _Cv2CfrSink, _measure_actual_fps
from imu_camera_sync_multi import (
    ImuDevice, ble_thread_main, parse_imu_spec, stop_event, _new_sample_event,
)


class CameraStream:
    """一路摄像头的独立状态：VideoCapture、视频写入、fps 统计。"""

    def __init__(self, index: int, label: str, width: int, height: int, target_fps: int):
        self.index = index
        self.label = label
        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise RuntimeError(f'无法打开摄像头 {index}（{label}）')
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, target_fps)
        self.actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.video_writer = None
        self.ts_window: list[float] = []

    def read(self):
        return self.cap.read()

    def fps_tick(self, now: float) -> float:
        cutoff = now - 1.0
        while self.ts_window and self.ts_window[0] < cutoff:
            self.ts_window.pop(0)
        self.ts_window.append(now)
        return float(len(self.ts_window))

    def release(self):
        self.cap.release()
        if self.video_writer:
            self.video_writer.close()


def draw_overlay(frame, cam_label, cam_fps, target_fps, imu_info, elapsed, frame_idx):
    h, w = frame.shape[:2]
    overlay = frame.copy()
    box_h = 30 + 26 * (len(imu_info) + 1)
    cv2.rectangle(overlay, (0, 0), (w, box_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.45, frame, 0.55, 0, frame)

    def put(text, row, color=(200, 255, 200)):
        cv2.putText(frame, text, (12, 28 + row * 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)

    ts = datetime.now().strftime('%H:%M:%S.%f')[:12]
    put(f'{ts}  [{cam_label}]  #{frame_idx}  t={elapsed:.1f}s  {cam_fps:.1f}/{target_fps}fps', 0, (255, 255, 100))
    for i, (device, hz, lag_ms, missing) in enumerate(imu_info):
        if missing:
            color = (80, 80, 255)
            text = f'[{device.label}] MISSING'
        elif lag_ms < 50:
            color = (100, 255, 100)
            text = f'[{device.label}] {hz:.1f}Hz  lag={lag_ms:.0f}ms'
        elif lag_ms < 150:
            color = (50, 200, 255)
            text = f'[{device.label}] {hz:.1f}Hz  lag={lag_ms:.0f}ms'
        else:
            color = (80, 80, 255)
            text = f'[{device.label}] {hz:.1f}Hz  lag={lag_ms:.0f}ms !'
        put(text, i + 1, color)
    return frame


def run_cameras(args, cameras: list[CameraStream], devices: list[ImuDevice]):
    target_fps = args.cam_fps
    frame_interval = 1.0 / target_fps
    save_overlay = not args.no_save_overlay
    imu_sync = not args.no_imu_sync

    for cam in cameras:
        print(f'{cam.label} ({cam.index}): {cam.actual_w}x{cam.actual_h}  目标帧率: {target_fps}fps')

    record_mode = args.duration and args.duration > 0

    if record_mode and args.warmup_sec > 0:
        print(f'预热 {args.warmup_sec:.1f}s...')
        until = time.time() + args.warmup_sec
        while time.time() < until and not stop_event.is_set():
            for cam in cameras:
                cam.read()
            time.sleep(1.0 / target_fps)
        cam_report = '  '.join(
            f'{c.label}={_measure_actual_fps(c.cap, warmup=0, sample=10):.1f}fps' for c in cameras)
        imu_report = '  '.join(f'{d.label}={d.current_hz():.1f}Hz' for d in devices)
        print(f'预热结束: {cam_report}  {imu_report}')

    ts_tag = datetime.now().strftime('%Y%m%d_%H%M%S')
    os.makedirs(args.out_dir, exist_ok=True)
    base = os.path.join(args.out_dir, f'multicam_{ts_tag}')

    csv_file = meta_file = None
    csv_writer = meta_writer = None

    csv_header = ['timestamp']
    meta_header = ['frame_idx', 'timestamp']
    for cam in cameras:
        meta_header.append(f'{cam.label}_fps')
    for d in devices:
        csv_header += [f'{d.label}_acc_x', f'{d.label}_acc_y', f'{d.label}_acc_z',
                        f'{d.label}_gyro_x', f'{d.label}_gyro_y', f'{d.label}_gyro_z']
        meta_header += [f'{d.label}_imu_timestamp', f'{d.label}_lag_ms', f'{d.label}_missing',
                         f'{d.label}_hz', f'{d.label}_acc_x', f'{d.label}_acc_y', f'{d.label}_acc_z',
                         f'{d.label}_gyro_x', f'{d.label}_gyro_y', f'{d.label}_gyro_z']

    if record_mode:
        use_ffmpeg = shutil.which('ffmpeg') is not None
        for cam in cameras:
            video_path = f'{base}_{cam.label}.mp4'
            if use_ffmpeg:
                cam.video_writer = _FfmpegVfrSink(video_path, cam.actual_w, cam.actual_h, crf=args.video_crf)
            else:
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                cam.video_writer = _Cv2CfrSink(video_path, fourcc, float(target_fps), cam.actual_w, cam.actual_h)
                print(f'警告: 未找到 ffmpeg，{cam.label} 退化为固定 fps 写入视频。')
            print(f'  {cam.label} 视频→{video_path}')

        csv_file = open(f'{base}.csv', 'w', newline='', encoding='utf-8-sig')
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(csv_header)
        meta_file = open(f'{base}_meta.csv', 'w', newline='', encoding='utf-8-sig')
        meta_writer = csv.writer(meta_file)
        meta_writer.writerow(meta_header)

        for d in devices:
            raw_file = open(f'{base}_{d.label}_raw.csv', 'w', newline='', encoding='utf-8-sig')
            raw_writer = csv.writer(raw_file)
            raw_writer.writerow(['pc_ms', 'acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z'])
            d.set_raw_writer(raw_writer)
            d._raw_file = raw_file

        print(f'录制模式: {args.duration}s  组合CSV→{base}.csv  对齐信息→{base}_meta.csv')
        for d in devices:
            print(f'  {d.label} 原始流水→{base}_{d.label}_raw.csv')
    else:
        print('实时模式（按 Q 或 Ctrl+C 退出）。')

    start_time = time.time()
    next_tick = start_time
    frame_idx = 0
    elapsed = 0.0
    max_lag_ms = 3 * (1000.0 / target_fps)

    try:
        while not stop_event.is_set():
            if imu_sync:
                _new_sample_event.wait(timeout=frame_interval * 3)
                _new_sample_event.clear()
                now = time.time()
                if now < next_tick:
                    time.sleep(next_tick - now)
                next_tick = time.time() + frame_interval
            else:
                now = time.time()
                sleep_s = next_tick - now
                if sleep_s > 0:
                    time.sleep(sleep_s)
                next_tick += frame_interval

            tick_ts = time.time()
            tick_ts_ms = tick_ts * 1000.0
            frame_idx += 1
            elapsed = tick_ts - start_time
            if tick_ts < start_time:
                continue

            frames = []
            read_failed = False
            for cam in cameras:
                ret, frame = cam.read()
                if not ret:
                    print(f'{cam.label} 读取失败，退出。')
                    read_failed = True
                    break
                frames.append(frame)
            if read_failed:
                break

            tick_ts_str = datetime.fromtimestamp(tick_ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            cam_fps_list = [cam.fps_tick(tick_ts) for cam in cameras]

            csv_row = [tick_ts_str]
            meta_row = [frame_idx, tick_ts_str] + [f'{fps:.1f}' for fps in cam_fps_list]
            imu_info = []

            for d in devices:
                imu_row, lag_ms, missing = d.find_nearest(tick_ts_ms, max_lag_ms)
                hz = d.current_hz()
                if missing or imu_row is None:
                    acc = ['', '', '']
                    gyro = ['', '', '']
                    imu_ts_str = ''
                    lag_str = f'{lag_ms:.1f}' if lag_ms != float('inf') else ''
                    missing_flag = 1
                else:
                    acc = [f"{imu_row['acc_x']:.6f}", f"{imu_row['acc_y']:.6f}", f"{imu_row['acc_z']:.6f}"]
                    gyro = [f"{imu_row['gyro_x']:.6f}", f"{imu_row['gyro_y']:.6f}", f"{imu_row['gyro_z']:.6f}"]
                    imu_ts_str = datetime.fromtimestamp(imu_row['pc_ms'] / 1000.0).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                    lag_str = f'{lag_ms:.1f}'
                    missing_flag = 0
                csv_row += acc + gyro
                meta_row += [imu_ts_str, lag_str, missing_flag, f'{hz:.1f}', *acc, *gyro]
                imu_info.append((d, hz, lag_ms, missing))

            if csv_writer:
                csv_writer.writerow(csv_row)
            if meta_writer:
                meta_writer.writerow(meta_row)

            for cam, frame, cam_fps in zip(cameras, frames, cam_fps_list):
                display = draw_overlay(frame.copy(), cam.label, cam_fps, target_fps, imu_info, elapsed, frame_idx)
                if cam.video_writer:
                    cam.video_writer.write(display if save_overlay else frame)
                try:
                    cv2.imshow(f'IMU(multicam) {cam.label}', display)
                except cv2.error:
                    if not record_mode:
                        print('cv2.imshow 不支持（可能是 headless 版本）。')
                        read_failed = True

            if read_failed:
                break

            if record_mode and elapsed >= args.duration:
                print(f'\n已达到录制时长 {args.duration}s，停止。')
                break

            try:
                key = cv2.waitKey(1) & 0xFF
            except cv2.error:
                key = 0xFF
            if key in (ord('q'), ord('Q'), 27):
                break

    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for cam in cameras:
            cam.release()
        if csv_file:
            csv_file.close()
        if meta_file:
            meta_file.close()
        for d in devices:
            d.set_raw_writer(None)
            if hasattr(d, '_raw_file'):
                d._raw_file.close()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass
        print(f'\n共采集 {frame_idx} 个同步tick  {elapsed:.1f}s')
        if record_mode:
            print(f'已保存: {base}.csv  {base}_meta.csv')
            for cam in cameras:
                print(f'       {base}_{cam.label}.mp4')
            for d in devices:
                print(f'       {base}_{d.label}_raw.csv')
            print()
            print('── 自动对齐校验（各摄像头帧数 vs 组合CSV行数）──')
            # 每个 tick 都同时读了所有摄像头一帧，所以每路摄像头的视频帧数理论上
            # 应该都严格等于 frame_idx（组合 CSV 的行数）；check_alignment.py 假设
            # 视频和 CSV 同名成对，这里视频是 {base}_{label}.mp4、CSV 是共享的
            # {base}.csv，不满足它的命名假设，所以直接读帧数自己比对，不复用它。
            for cam in cameras:
                video_path = f'{base}_{cam.label}.mp4'
                cap_check = cv2.VideoCapture(video_path)
                actual_frames = int(cap_check.get(cv2.CAP_PROP_FRAME_COUNT))
                cap_check.release()
                if actual_frames == frame_idx:
                    print(f'  [{cam.label}] ✔ 帧数与组合CSV行数一致: {actual_frames}')
                else:
                    print(f'  [{cam.label}] ✘ 帧数不一致: 视频 {actual_frames} 帧, CSV {frame_idx} 行')


def main():
    ap = argparse.ArgumentParser(description='多个摄像头 + 多个 IMU 设备同步采集')
    ap.add_argument('--camera', action='append', type=int, required=True,
                    help='摄像头编号，可重复传多个，例如 --camera 0 --camera 1')
    ap.add_argument('--imu', action='append', required=True,
                    help='IMU 设备，格式 类型=标识，可重复传多个。见 imu_camera_sync_multi.py 说明。')
    ap.add_argument('--width', type=int, default=1280, help='摄像头请求分辨率宽，默认 1280（720p）')
    ap.add_argument('--height', type=int, default=720, help='摄像头请求分辨率高，默认 720（720p）')
    ap.add_argument('--cam-fps', type=int, default=20, help='摄像头目标帧率，默认 20')
    ap.add_argument('--duration', type=float, default=0, help='录制时长（秒），0=实时模式不保存')
    ap.add_argument('--warmup-sec', type=float, default=5.0, help='预热时长（秒），默认 5，设 0 关闭')
    ap.add_argument('--video-crf', type=int, default=28, help='H.264 CRF，默认 28')
    ap.add_argument('--out-dir', default='data', help='输出目录，默认 data/')
    ap.add_argument('--scan-timeout', type=float, default=8.0, help='BLE 扫描超时（秒），默认 8')
    ap.add_argument('--no-save-overlay', action='store_true', help='保存干净视频（不含叠加信息）')
    ap.add_argument('--no-imu-sync', action='store_true', help='关闭事件驱动同步，改用固定定时器抓帧')
    args = ap.parse_args()

    devices = []
    for i, spec in enumerate(args.imu, start=1):
        try:
            dev_type, ident = parse_imu_spec(spec)
        except ValueError as e:
            print(e)
            sys.exit(1)
        devices.append(ImuDevice(dev_type, ident, label=f'imu{i}'))

    cameras = []
    for i, cam_idx in enumerate(args.camera, start=1):
        try:
            cameras.append(CameraStream(cam_idx, f'cam{i}', args.width, args.height, args.cam_fps))
        except RuntimeError as e:
            print(e)
            sys.exit(1)

    t = threading.Thread(target=ble_thread_main, args=(devices, args.scan_timeout), daemon=True)
    t.start()

    print('等待 BLE 连接中...')
    time.sleep(2.0)

    run_cameras(args, cameras, devices)

    stop_event.set()
    t.join(timeout=3.0)


if __name__ == '__main__':
    main()
