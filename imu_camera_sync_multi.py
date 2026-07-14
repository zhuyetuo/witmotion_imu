# -*- coding: utf-8 -*-
"""
一个摄像头 + 多个 IMU 设备 同步采集脚本
==========================================

跟 imu_camera_sync.py 是同一套思路（VFR 视频写入、事件驱动对齐、真实数据
缺口不外推），扩展成支持同时连接多台 IMU 设备（WitMotion 和/或 HICC 混用
都可以），每台设备独立记录，共用同一路摄像头画面。

v1 版本先做核心功能，暂不包含 imu_camera_sync.py 里的 --loop / --resample-hz
/ --probe / --resample-only 这些高级选项——先跑通多设备同步采集，有需要再加。

用法:
    # 一个摄像头 + 2 个 WitMotion 设备
    python imu_camera_sync_multi.py --imu wit=WTSDCL1 --imu wit=WTSDCL2 --duration 60

    # 1个 WitMotion + 1个 HICC（HICC 必须用 MAC 地址）
    python imu_camera_sync_multi.py --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --duration 60

    # WitMotion 也可以直接用 MAC 地址指定（自动识别，不用连大小写名字模糊匹配）
    python imu_camera_sync_multi.py --imu wit=D5:34:E2:B9:6F:32 --imu hicc=EA:CB:3E:CF:00:1A --duration 60

--imu 可重复传，每个设备格式为 "类型=标识"：
    wit=<名称关键字或MAC地址>   （自动识别标识是不是 MAC 格式）
    hicc=<MAC地址>              （HICC 必须用 MAC 地址）
第一个 --imu 对应 imu1，第二个对应 imu2，以此类推（输出列名/文件名用这个编号）。

输出:
    {base}.mp4                              视频（VFR，含叠加信息）
    {base}.csv                              每帧一行，列为 timestamp, imu1_acc_x...imu1_gyro_z,
                                             imu2_acc_x...imu2_gyro_z ...（按 --imu 顺序）
    {base}_meta.csv                         每帧一行，每个设备的 lag_ms/missing/hz 等对齐信息
    {base}_imu1_raw.csv, {base}_imu2_raw.csv...  各设备的原始 IMU 全量流水（不受摄像头帧率影响）
"""

import argparse
import asyncio
import csv
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from datetime import datetime

try:
    import cv2
except ImportError:
    print('缺少 opencv-python，请先安装: pip install opencv-python')
    sys.exit(1)

try:
    from bleak import BleakClient
except ImportError:
    print('缺少 bleak，请先安装: pip install bleak')
    sys.exit(1)

from ble_utils import find_device
from imu_camera_sync import _FfmpegVfrSink, _Cv2CfrSink, _measure_actual_fps

_MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$')

stop_event = threading.Event()


class ImuDevice:
    """一个 IMU 设备的独立状态：接收缓冲、Hz 统计、原始流水日志。"""

    def __init__(self, dev_type: str, ident: str, label: str):
        self.dev_type = dev_type
        self.ident = ident
        self.label = label
        self.mac = 'unknown'
        self.buffer: deque = deque(maxlen=500)
        self.lock = threading.Lock()
        self.hz_window: list[float] = []
        self.hz_lock = threading.Lock()
        self.raw_writer = None
        self.raw_lock = threading.Lock()

    def push(self, row: dict):
        now = time.time()
        with self.lock:
            self.buffer.append(row)
        with self.hz_lock:
            self.hz_window.append(now)
        with self.raw_lock:
            if self.raw_writer is not None:
                self.raw_writer.writerow([
                    f"{row['pc_ms']:.3f}",
                    f"{row['acc_x']:.6f}", f"{row['acc_y']:.6f}", f"{row['acc_z']:.6f}",
                    f"{row['gyro_x']:.6f}", f"{row['gyro_y']:.6f}", f"{row['gyro_z']:.6f}",
                ])

    def set_raw_writer(self, writer):
        with self.raw_lock:
            self.raw_writer = writer

    def current_hz(self) -> float:
        now = time.time()
        cutoff = now - 1.0
        with self.hz_lock:
            while self.hz_window and self.hz_window[0] < cutoff:
                self.hz_window.pop(0)
            return float(len(self.hz_window))

    def find_nearest(self, cam_ts_ms: float, max_lag_ms: float):
        with self.lock:
            if not self.buffer:
                return None, float('inf'), True
            best = min(self.buffer, key=lambda r: abs(r['pc_ms'] - cam_ts_ms))
        lag = abs(best['pc_ms'] - cam_ts_ms)
        return best, lag, lag > max_lag_ms


# ── BLE 连接（每个设备一个协程，同一个事件循环里并发跑） ────────────────────

async def run_wit_device(device: ImuDevice, scan_timeout: float):
    from wit_parse import DEFAULT_NOTIFY_CANDIDATES, StreamingByteBuffer, parse_one_packet

    is_mac = bool(_MAC_RE.match(device.ident))
    ble_device = await find_device(
        None if is_mac else device.ident,
        device.ident if is_mac else None,
        timeout=scan_timeout,
    )
    if ble_device is None:
        print(f'[{device.label}] 找不到 WitMotion 设备: {device.ident}')
        return

    print(f'[{device.label}] WitMotion 已连接: {ble_device.name}  {ble_device.address}')
    device.mac = ble_device.address
    buf = StreamingByteBuffer()

    def on_data(_, data: bytearray):
        pc_ms = time.time() * 1000.0
        for pkt in buf.feed(bytes(data)):
            p = parse_one_packet(pkt)
            if p is None:
                continue
            device.push({
                'pc_ms': pc_ms,
                'acc_x': p['acc'][0], 'acc_y': p['acc'][1], 'acc_z': p['acc'][2],
                'gyro_x': p['gyro'][0], 'gyro_y': p['gyro'][1], 'gyro_z': p['gyro'][2],
            })

    async with BleakClient(ble_device) as client:
        subscribed = None
        for uuid in DEFAULT_NOTIFY_CANDIDATES:
            try:
                await client.start_notify(uuid, on_data)
                subscribed = uuid
                break
            except Exception:
                continue
        if subscribed is None:
            print(f'[{device.label}] 订阅 Notify 失败')
            return
        print(f'[{device.label}] 已订阅: {subscribed}')
        while not stop_event.is_set():
            await asyncio.sleep(0.1)
        try:
            await client.stop_notify(subscribed)
        except Exception:
            pass
    print(f'[{device.label}] WitMotion 已断开')


async def run_hicc_device(device: ImuDevice, scan_timeout: float):
    from hicc_parse import (
        FrameBuffer, parse_dp_sequence, find_tx_uuid, find_rx_uuid, send_timesync,
        DP_ACC_X, DP_ACC_Y, DP_ACC_Z, DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z, CMD_REPORT,
    )

    if not _MAC_RE.match(device.ident):
        print(f'[{device.label}] HICC 设备必须用 MAC 地址指定，收到: {device.ident}')
        return

    address = device.ident
    device.mac = address
    print(f'[{device.label}] 连接 HICC: {address}')
    fb = FrameBuffer()

    def on_data(_, data: bytearray):
        pc_ms = time.time() * 1000.0
        for frame in fb.feed(bytes(data)):
            if frame[3] != CMD_REPORT:
                continue
            dps = parse_dp_sequence(frame[6:-1])
            if DP_ACC_X not in dps or DP_GYRO_X not in dps:
                continue
            device.push({
                'pc_ms': pc_ms,
                'acc_x': dps[DP_ACC_X] / 1_000_000.0, 'acc_y': dps[DP_ACC_Y] / 1_000_000.0,
                'acc_z': dps[DP_ACC_Z] / 1_000_000.0,
                'gyro_x': dps[DP_GYRO_X] / 1_000_000.0, 'gyro_y': dps[DP_GYRO_Y] / 1_000_000.0,
                'gyro_z': dps[DP_GYRO_Z] / 1_000_000.0,
            })

    async with BleakClient(address) as client:
        tx_uuid = await find_tx_uuid(client)
        rx_uuid = await find_rx_uuid(client)
        if tx_uuid is None:
            print(f'[{device.label}] 找不到 HICC TX 特征值')
            return
        if rx_uuid:
            await send_timesync(client, rx_uuid)
        await client.start_notify(tx_uuid, on_data)
        print(f'[{device.label}] 已订阅: {tx_uuid}')
        while not stop_event.is_set():
            await asyncio.sleep(0.1)
        try:
            await client.stop_notify(tx_uuid)
        except Exception:
            pass
    print(f'[{device.label}] HICC 已断开')


def ble_thread_main(devices: list[ImuDevice], scan_timeout: float):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_all():
        tasks = []
        for d in devices:
            if d.dev_type == 'wit':
                tasks.append(run_wit_device(d, scan_timeout))
            else:
                tasks.append(run_hicc_device(d, scan_timeout))
        await asyncio.gather(*tasks)

    try:
        loop.run_until_complete(run_all())
    except Exception as e:
        print(f'BLE 线程异常: {e}')
    finally:
        stop_event.set()
        loop.close()


# ── 摄像头主循环 ──────────────────────────────────────────────────────────

def draw_overlay(frame, devices, elapsed, cam_fps, target_fps, frame_idx):
    h, w = frame.shape[:2]
    overlay = frame.copy()
    box_h = 30 + 26 * (len(devices) + 1)
    cv2.rectangle(overlay, (0, 0), (w, box_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.45, frame, 0.55, 0, frame)

    def put(text, row, color=(200, 255, 200)):
        cv2.putText(frame, text, (12, 28 + row * 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)

    ts = datetime.now().strftime('%H:%M:%S.%f')[:12]
    put(f'{ts}  #{frame_idx}  t={elapsed:.1f}s  CAM {cam_fps:.1f}/{target_fps}fps', 0, (255, 255, 100))
    for i, (device, hz, lag_ms, missing) in enumerate(devices):
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


def run_camera(args, devices: list[ImuDevice]):
    target_fps = args.cam_fps
    frame_interval = 1.0 / target_fps

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f'无法打开摄像头 {args.camera}')
        stop_event.set()
        return
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, target_fps)
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f'摄像头分辨率: {actual_w}x{actual_h}  目标帧率: {target_fps}fps')

    record_mode = args.duration and args.duration > 0

    if record_mode and args.warmup_sec > 0:
        print(f'预热 {args.warmup_sec:.1f}s...')
        until = time.time() + args.warmup_sec
        while time.time() < until and not stop_event.is_set():
            cap.read()
            time.sleep(1.0 / target_fps)
        print(f'预热结束: CAM {_measure_actual_fps(cap, warmup=0, sample=10):.1f}fps  '
              + '  '.join(f'{d.label}={d.current_hz():.1f}Hz' for d in devices))

    ts_tag = datetime.now().strftime('%Y%m%d_%H%M%S')
    os.makedirs(args.out_dir, exist_ok=True)
    base = os.path.join(args.out_dir, f'multi_{ts_tag}')

    video_writer = None
    csv_file = meta_file = None
    csv_writer = meta_writer = None

    csv_header = ['timestamp']
    meta_header = ['frame_idx', 'cam_timestamp', 'cam_fps']
    for d in devices:
        csv_header += [f'{d.label}_acc_x', f'{d.label}_acc_y', f'{d.label}_acc_z',
                        f'{d.label}_gyro_x', f'{d.label}_gyro_y', f'{d.label}_gyro_z']
        meta_header += [f'{d.label}_imu_timestamp', f'{d.label}_lag_ms', f'{d.label}_missing',
                         f'{d.label}_hz', f'{d.label}_acc_x', f'{d.label}_acc_y', f'{d.label}_acc_z',
                         f'{d.label}_gyro_x', f'{d.label}_gyro_y', f'{d.label}_gyro_z']

    if record_mode:
        video_path = f'{base}.mp4'
        use_ffmpeg = shutil.which('ffmpeg') is not None
        if use_ffmpeg:
            video_writer = _FfmpegVfrSink(video_path, actual_w, actual_h, crf=args.video_crf)
        else:
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            video_writer = _Cv2CfrSink(video_path, fourcc, float(target_fps), actual_w, actual_h)
            print('警告: 未找到 ffmpeg，退化为固定 fps 写入视频。')

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
            d._raw_file = raw_file  # 挂在对象上方便统一关闭

        print(f'录制模式: {args.duration}s  视频→{video_path}')
        print(f'  组合CSV→{base}.csv  对齐信息→{base}_meta.csv')
        for d in devices:
            print(f'  {d.label} 原始流水→{base}_{d.label}_raw.csv')
    else:
        print('实时模式（按 Q 或 Ctrl+C 退出）。')

    start_time = time.time()
    next_tick = start_time
    frame_idx = 0
    elapsed = 0.0
    cam_ts_window: list[float] = []
    max_lag_ms = 3 * (1000.0 / target_fps)

    def cam_fps_tick(now):
        cutoff = now - 1.0
        while cam_ts_window and cam_ts_window[0] < cutoff:
            cam_ts_window.pop(0)
        cam_ts_window.append(now)
        return float(len(cam_ts_window))

    try:
        while not stop_event.is_set():
            now = time.time()
            sleep_s = next_tick - now
            if sleep_s > 0:
                time.sleep(sleep_s)
            next_tick += frame_interval

            ret, frame = cap.read()
            if not ret:
                print('摄像头读取失败，退出。')
                break

            cam_ts = time.time()
            cam_ts_ms = cam_ts * 1000.0
            frame_idx += 1
            elapsed = cam_ts - start_time
            if cam_ts < start_time:
                continue

            cam_fps = cam_fps_tick(cam_ts)
            cam_ts_str = datetime.fromtimestamp(cam_ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            csv_row = [cam_ts_str]
            meta_row = [frame_idx, cam_ts_str, f'{cam_fps:.1f}']
            overlay_info = []

            for d in devices:
                imu_row, lag_ms, missing = d.find_nearest(cam_ts_ms, max_lag_ms)
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
                overlay_info.append((d, hz, lag_ms, missing))

            if csv_writer:
                csv_writer.writerow(csv_row)
            if meta_writer:
                meta_writer.writerow(meta_row)

            display = draw_overlay(frame.copy(), overlay_info, elapsed, cam_fps, target_fps, frame_idx)
            if video_writer:
                video_writer.write(display)

            try:
                cv2.imshow('IMU(multi) + Camera Sync', display)
            except cv2.error:
                if not record_mode:
                    print('cv2.imshow 不支持（可能是 headless 版本）。')
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
        cap.release()
        if video_writer:
            video_writer.close()
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
        print(f'\n共采集 {frame_idx} 帧视频  {elapsed:.1f}s')
        if record_mode:
            print(f'已保存: {base}.mp4  {base}.csv  {base}_meta.csv')
            for d in devices:
                print(f'       {base}_{d.label}_raw.csv')
            print()
            print('── 自动对齐校验 ──')
            try:
                import check_alignment
                check_alignment.run_check(base)
            except Exception as e:
                print(f'对齐校验运行失败: {e}（可手动运行 python check_alignment.py {base}）')


def parse_imu_spec(spec: str) -> tuple[str, str]:
    if '=' not in spec:
        raise ValueError(f'--imu 格式应为 类型=标识，例如 wit=WTSDCL 或 hicc=EA:CB:3E:CF:00:1A，收到: {spec!r}')
    dev_type, ident = spec.split('=', 1)
    dev_type = dev_type.strip().lower()
    if dev_type not in ('wit', 'hicc'):
        raise ValueError(f'--imu 类型只能是 wit 或 hicc，收到: {dev_type!r}')
    return dev_type, ident.strip()


def main():
    ap = argparse.ArgumentParser(description='一个摄像头 + 多个 IMU 设备同步采集')
    ap.add_argument('--imu', action='append', required=True,
                    help='IMU 设备，格式 类型=标识，可重复传多个。例如 --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A。'
                         'wit 的标识可以是名称关键字或 MAC 地址（自动识别）；hicc 必须用 MAC 地址。')
    ap.add_argument('--camera', type=int, default=0, help='摄像头编号，默认 0')
    ap.add_argument('--width', type=int, default=1280, help='摄像头请求分辨率宽，默认 1280（720p）')
    ap.add_argument('--height', type=int, default=720, help='摄像头请求分辨率高，默认 720（720p）')
    ap.add_argument('--cam-fps', type=int, default=20, help='摄像头目标帧率，默认 20')
    ap.add_argument('--duration', type=float, default=0, help='录制时长（秒），0=实时模式不保存')
    ap.add_argument('--warmup-sec', type=float, default=5.0, help='预热时长（秒），默认 5，设 0 关闭')
    ap.add_argument('--video-crf', type=int, default=28, help='H.264 CRF，默认 28')
    ap.add_argument('--out-dir', default='data', help='输出目录，默认 data/')
    ap.add_argument('--scan-timeout', type=float, default=8.0, help='BLE 扫描超时（秒），默认 8')
    args = ap.parse_args()

    devices = []
    for i, spec in enumerate(args.imu, start=1):
        try:
            dev_type, ident = parse_imu_spec(spec)
        except ValueError as e:
            print(e)
            sys.exit(1)
        devices.append(ImuDevice(dev_type, ident, label=f'imu{i}'))

    t = threading.Thread(target=ble_thread_main, args=(devices, args.scan_timeout), daemon=True)
    t.start()

    print('等待 BLE 连接中...')
    time.sleep(2.0)

    run_camera(args, devices)

    stop_event.set()
    t.join(timeout=3.0)


if __name__ == '__main__':
    main()
