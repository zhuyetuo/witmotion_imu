# -*- coding: utf-8 -*-
"""
一个摄像头 + 多个 IMU 设备 同步采集脚本
==========================================

跟 imu_camera_sync.py 是同一套思路（VFR 视频写入、事件驱动对齐、真实数据
缺口不外推），扩展成支持同时连接多台 IMU 设备（WitMotion 和/或 HICC 混用
都可以），每台设备独立记录，共用同一路摄像头画面。

现已补齐 imu_camera_sync.py 里的全部高级选项：--loop / --resample-hz /
--probe / --resample-only / --no-save-overlay / --no-imu-sync。

用法:
    # 一个摄像头 + 2 个 WitMotion 设备
    python imu_camera_sync_multi.py --imu wit=WTSDCL1 --imu wit=WTSDCL2 --duration 60

    # 1个 WitMotion + 1个 HICC（HICC 必须用 MAC 地址）
    python imu_camera_sync_multi.py --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --duration 60

    # WitMotion 也可以直接用 MAC 地址指定（自动识别，不用连大小写名字模糊匹配）
    python imu_camera_sync_multi.py --imu wit=D5:34:E2:B9:6F:32 --imu hicc=EA:CB:3E:CF:00:1A --duration 60

    # 探测硬件能力：摄像头 + 每个 IMU 设备当前实际输出频率
    python imu_camera_sync_multi.py --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --probe

    # 每个设备都降采样到16Hz，只保留降采样版文件，循环录制每段3分钟
    python imu_camera_sync_multi.py --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A \
        --duration 180 --resample-hz 16 --resample-only --loop

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
    {base}_imu1_resampled{HZ}hz.csv/.mp4...      每个设备各自的降采样 CSV + 配对视频副本
                                                  （--resample-hz 指定目标频率，默认 25）
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
from imu_camera_sync import (
    _FfmpegVfrSink, _Cv2CfrSink, _measure_actual_fps, probe_camera, resample_raw_imu,
)

_MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$')

stop_event = threading.Event()
# 任意一个设备来了新样本就 set，用于事件驱动抓帧（--no-imu-sync 可关闭改回固定定时器）
_new_sample_event = threading.Event()

# 原始IMU流水csv的表头：timestamp 是格式化时间字符串（跟降采样输出的
# timestamp 列格式一致，%Y-%m-%d %H:%M:%S.fff），给人看/直接拖进 Label
# Studio 用。resample_raw_imu() 需要的 epoch 毫秒数改从这一列反解析
# （datetime.strptime），不再单独存一份 pc_ms 数值列（仍兼容旧版两列都有的
# raw.csv，见 imu_camera_sync.py 的 resample_raw_imu()）。
RAW_CSV_HEADER = ['timestamp', 'acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z']


def _fmt_pc_ms(pc_ms: float) -> str:
    return datetime.fromtimestamp(pc_ms / 1000.0).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


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
                    _fmt_pc_ms(row['pc_ms']),
                    f"{row['acc_x']:.6f}", f"{row['acc_y']:.6f}", f"{row['acc_z']:.6f}",
                    f"{row['gyro_x']:.6f}", f"{row['gyro_y']:.6f}", f"{row['gyro_z']:.6f}",
                ])
        _new_sample_event.set()

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
    """
    自动重连：BLE 信号太差（比如项圈被狗压在身下）会导致连接被判定为真正
    断开，而不只是丢几个包。断开后不退出协程，而是稍等一下重新扫描/连接，
    不断重试直到 stop_event 被设置（--duration 到时或用户手动停止），
    这样信号恢复后能自动续上，不会一直卡在"最后一次断开前"的状态。
    """
    from wit_parse import DEFAULT_NOTIFY_CANDIDATES, StreamingByteBuffer, parse_one_packet

    def on_data(_, data: bytearray, buf):
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

    first_attempt = True
    while not stop_event.is_set():
        is_mac = bool(_MAC_RE.match(device.ident))
        try:
            ble_device = await find_device(
                None if is_mac else device.ident,
                device.ident if is_mac else None,
                timeout=scan_timeout,
            )
        except Exception as e:
            # find_device() 本身也可能抛异常（尤其同时扫描/连接好几个设备时，
            # 蓝牙协议栈更容易出问题）——之前这里没兜住，会直接冒泡出这个协程，
            # 导致 asyncio.gather() 连带把所有设备的协程都判定失败，
            # ble_thread_main() 的 finally 又会把 stop_event 设上，等于一个
            # 设备扫描出错就把整个录制程序都干掉了。现在跟连接阶段一样重试。
            if stop_event.is_set():
                break
            print(f'[{device.label}] 扫描出错: {e}，2秒后重试...')
            await asyncio.sleep(2.0)
            continue
        if ble_device is None:
            action = '扫描' if first_attempt else '重连'
            print(f'[{device.label}] {action}未找到 WitMotion 设备: {device.ident}，2秒后重试...')
            first_attempt = False
            await asyncio.sleep(2.0)
            continue

        disconnected = asyncio.Event()

        def on_disconnect(_client):
            disconnected.set()

        buf = StreamingByteBuffer()
        try:
            async with BleakClient(ble_device, disconnected_callback=on_disconnect) as client:
                print(f'[{device.label}] WitMotion 已连接: {ble_device.name}  {ble_device.address}')
                device.mac = ble_device.address
                subscribed = None
                for uuid in DEFAULT_NOTIFY_CANDIDATES:
                    try:
                        await client.start_notify(uuid, lambda s, d: on_data(s, d, buf))
                        subscribed = uuid
                        break
                    except Exception:
                        continue
                if subscribed is None:
                    print(f'[{device.label}] 订阅 Notify 失败，2秒后重试...')
                    await asyncio.sleep(2.0)
                    continue
                print(f'[{device.label}] 已订阅: {subscribed}')
                while not stop_event.is_set() and not disconnected.is_set():
                    await asyncio.sleep(0.1)
                if disconnected.is_set():
                    print(f'[{device.label}] 连接断开（信号问题），尝试自动重连...')
                    continue
                try:
                    await client.stop_notify(subscribed)
                except Exception:
                    pass
        except Exception as e:
            if stop_event.is_set():
                break
            print(f'[{device.label}] 连接异常: {e}，2秒后重试...')
            await asyncio.sleep(2.0)
            continue
        break
    print(f'[{device.label}] WitMotion 已断开')


async def run_hicc_device(device: ImuDevice, scan_timeout: float):
    """自动重连，原因同 run_wit_device（信号差导致真实断连，不重连就永远卡在断开状态）。"""
    from hicc_parse import (
        FrameBuffer, parse_dp_sequence, find_tx_uuid, find_rx_uuid, send_timesync,
        DP_ACC_X, DP_ACC_Y, DP_ACC_Z, DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z, CMD_REPORT,
    )

    if not _MAC_RE.match(device.ident):
        print(f'[{device.label}] HICC 设备必须用 MAC 地址指定，收到: {device.ident}')
        return

    address = device.ident
    device.mac = address

    def on_data(_, data: bytearray, fb):
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

    while not stop_event.is_set():
        print(f'[{device.label}] 连接 HICC: {address}')
        disconnected = asyncio.Event()

        def on_disconnect(_client):
            disconnected.set()

        fb = FrameBuffer()
        try:
            async with BleakClient(address, disconnected_callback=on_disconnect) as client:
                tx_uuid = await find_tx_uuid(client)
                rx_uuid = await find_rx_uuid(client)
                if tx_uuid is None:
                    print(f'[{device.label}] 找不到 HICC TX 特征值，2秒后重试...')
                    await asyncio.sleep(2.0)
                    continue
                if rx_uuid:
                    await send_timesync(client, rx_uuid)
                await client.start_notify(tx_uuid, lambda s, d: on_data(s, d, fb))
                print(f'[{device.label}] 已订阅: {tx_uuid}')
                while not stop_event.is_set() and not disconnected.is_set():
                    await asyncio.sleep(0.1)
                if disconnected.is_set():
                    print(f'[{device.label}] 连接断开（信号问题），尝试自动重连...')
                    continue
                try:
                    await client.stop_notify(tx_uuid)
                except Exception:
                    pass
        except Exception as e:
            if stop_event.is_set():
                break
            print(f'[{device.label}] 连接异常: {e}，2秒后重试...')
            await asyncio.sleep(2.0)
            continue
        break
    print(f'[{device.label}] HICC 已断开')


def ble_thread_main(devices: list[ImuDevice], scan_timeout: float):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_all():
        labeled_tasks = []
        for d in devices:
            if d.dev_type == 'wit':
                labeled_tasks.append((d.label, run_wit_device(d, scan_timeout)))
            else:
                labeled_tasks.append((d.label, run_hicc_device(d, scan_timeout)))
        # return_exceptions=True：某一个设备的协程如果出了没兜住的异常
        # （不管是这次修的 find_device() 那种，还是以后没想到的其它情况），
        # 只让那一个设备停止工作（后续一直显示 MISSING），不会连累其它设备/
        # 把整个录制程序都跟着终止——之前默认行为是只要有一个任务抛异常，
        # gather() 就直接把异常冒泡出去，run_wit_device 的 find_device() 那个
        # bug 就是这么把整个程序干掉的。
        results = await asyncio.gather(*(t for _, t in labeled_tasks), return_exceptions=True)
        for (label, _), result in zip(labeled_tasks, results):
            if isinstance(result, Exception):
                print(f'[{label}] 协程异常退出（该设备后续会一直显示MISSING，其它设备不受影响）: {result}')

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

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:23]
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
    loop_mode = args.loop and record_mode
    if args.loop and not record_mode:
        print('警告: --loop 需要配合 --duration 使用，已忽略 --loop。')

    if record_mode and args.warmup_sec > 0:
        print(f'预热 {args.warmup_sec:.1f}s...')
        until = time.time() + args.warmup_sec
        while time.time() < until and not stop_event.is_set():
            cap.read()
            time.sleep(1.0 / target_fps)
        print(f'预热结束: CAM {_measure_actual_fps(cap, warmup=0, sample=10):.1f}fps  '
              + '  '.join(f'{d.label}={d.current_hz():.1f}Hz' for d in devices))

    try:
        segment_no = 0
        while True:
            segment_no += 1
            if loop_mode:
                print(f'\n════ 第 {segment_no} 段录制开始 ════')
            should_stop = _run_one_segment(args, devices, cap, actual_w, actual_h, target_fps, record_mode)
            if not loop_mode or should_stop or stop_event.is_set():
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        cap.release()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass


def _run_one_segment(args, devices: list[ImuDevice], cap, actual_w, actual_h, target_fps, record_mode) -> bool:
    """录制一段，返回是否应该整体停止（True=用户退出/出错，False=正常到时结束）。"""
    should_stop = [False]
    frame_interval = 1.0 / target_fps
    save_overlay = not args.no_save_overlay
    imu_sync = not args.no_imu_sync

    # 精确到毫秒，避免 --loop 循环录制时文件名撞车互相覆盖。
    ts_tag = datetime.now().strftime('%Y%m%d_%H%M%S%f')[:-3]
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
            raw_writer.writerow(RAW_CSV_HEADER)
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
    first_cam_ts_ms = None
    last_cam_ts_ms = None
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
            if imu_sync:
                # 事件驱动：等任意一个设备来了新样本再抓帧，但等待时间不超过
                # 到下一个预定tick还剩多少（而不是固定的frame_interval*3）——
                # 否则IMU一直没有新样本送达时（比如断联、还没连上），每次都会
                # 傻等满这个固定超时，把实际fps拖到远低于目标fps。
                remaining = next_tick - time.time()
                if remaining > 0:
                    _new_sample_event.wait(timeout=remaining)
                _new_sample_event.clear()
                next_tick += frame_interval
            else:
                now = time.time()
                sleep_s = next_tick - now
                if sleep_s > 0:
                    time.sleep(sleep_s)
                next_tick += frame_interval

            ret, frame = cap.read()
            if not ret:
                print('摄像头读取失败，退出。')
                should_stop[0] = True
                break

            cam_ts = time.time()
            cam_ts_ms = cam_ts * 1000.0
            frame_idx += 1
            elapsed = cam_ts - start_time
            if cam_ts < start_time:
                continue

            if first_cam_ts_ms is None:
                first_cam_ts_ms = cam_ts_ms
            last_cam_ts_ms = cam_ts_ms

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
                video_writer.write(display if save_overlay else frame)

            try:
                cv2.imshow('IMU(multi) + Camera Sync', display)
            except cv2.error:
                if not record_mode:
                    print('cv2.imshow 不支持（可能是 headless 版本）。')
                    should_stop[0] = True
                    break

            if record_mode and elapsed >= args.duration:
                print(f'\n已达到录制时长 {args.duration}s，停止。')
                break

            try:
                key = cv2.waitKey(1) & 0xFF
            except cv2.error:
                key = 0xFF
            if key in (ord('q'), ord('Q'), 27):
                should_stop[0] = True
                break

    except KeyboardInterrupt:
        should_stop[0] = True
    finally:
        if stop_event.is_set():
            should_stop[0] = True
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
        print(f'\n共采集 {frame_idx} 帧视频  {elapsed:.1f}s')
        if record_mode:
            print(f'已保存: {base}.mp4  {base}.csv  {base}_meta.csv')
            for d in devices:
                print(f'       {base}_{d.label}_raw.csv')

            resampled_bases = {}
            for d in devices:
                resampled_base = f'{base}_{d.label}_resampled{args.resample_hz:g}hz'
                resample_raw_imu(
                    f'{base}_{d.label}_raw.csv', f'{resampled_base}.csv', args.resample_hz,
                    t_start_ms=first_cam_ts_ms, t_end_ms=last_cam_ts_ms,
                )
                print(f'       {resampled_base}.csv（{d.label} 降采样，起止时间已对齐视频）')
                try:
                    shutil.copyfile(f'{base}.mp4', f'{resampled_base}.mp4')
                    print(f'       {resampled_base}.mp4（复制，供 Label Studio 与 {d.label} resampled CSV 配对）')
                except OSError as e:
                    print(f'复制 {d.label} 配对视频失败: {e}')
                resampled_bases[d.label] = resampled_base

            print()
            print('── 自动对齐校验（组合CSV，按帧对齐）──')
            try:
                import check_alignment
                check_alignment.run_check(base)
            except Exception as e:
                print(f'对齐校验运行失败: {e}（可手动运行 python check_alignment.py {base}）')

            for label, resampled_base in resampled_bases.items():
                print()
                print(f'── 自动对齐校验（{label} 降采样版）──')
                try:
                    import check_alignment
                    check_alignment.run_check(resampled_base, meta_base=base, strict_frame_match=False)
                except Exception as e:
                    print(f'对齐校验运行失败: {e}（可手动运行 python check_alignment.py {resampled_base}）')

            if args.resample_only:
                for p in [f'{base}.mp4', f'{base}.csv', f'{base}_meta.csv']:
                    try:
                        os.remove(p)
                    except OSError as e:
                        print(f'删除 {p} 失败: {e}')
                for d in devices:
                    try:
                        os.remove(f'{base}_{d.label}_raw.csv')
                    except OSError as e:
                        print(f'删除 {base}_{d.label}_raw.csv 失败: {e}')
                print(f'\n--resample-only: 已删除原始文件，只保留各设备的 resampled mp4/csv')

    return should_stop[0]


def run_probe(args, devices: list[ImuDevice], probe_seconds: float = 5.0):
    """探测摄像头能力 + 短暂连接所有 IMU 设备测量各自实际输出频率，不录制。"""
    probe_camera(args.camera)

    print(f'── IMU 设备能力探测（连接 {probe_seconds:.0f} 秒测量各设备实际频率）──')
    t = threading.Thread(target=ble_thread_main, args=(devices, args.scan_timeout), daemon=True)
    t.start()
    time.sleep(2.0 + probe_seconds)
    stop_event.set()
    t.join(timeout=3.0)

    for d in devices:
        print(f'  [{d.label}] ({d.dev_type}={d.ident})  实际输出频率: 约 {d.current_hz():.1f} Hz')
    print('  （这是设备当前配置的频率，不是"最大支持频率"；WitMotion 具体可选档位需要在'
          '官方上位机软件里查看/修改。）')


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
    ap.add_argument('--no-save-overlay', action='store_true',
                    help='保存干净视频（不含叠加信息）；默认保存带叠加信息的视频')
    ap.add_argument('--no-imu-sync', action='store_true',
                    help='关闭事件驱动同步，改用固定定时器抓帧')
    ap.add_argument('--resample-hz', type=float, default=25.0,
                    help='录制结束后把每个设备的原始IMU流水降采样到该频率，默认25Hz')
    ap.add_argument('--resample-only', action='store_true',
                    help='只保留各设备的降采样版文件，删除原始的 {base}.mp4/.csv/_meta.csv/_raw.csv')
    ap.add_argument('--loop', action='store_true',
                    help='循环录制模式：每段 --duration 秒，录完自动开始下一段，直到按 Q/ESC 或 Ctrl+C 才停止')
    ap.add_argument('--probe', action='store_true',
                    help='只探测硬件能力（摄像头 + 各IMU设备当前实际输出频率），不录制，探测完直接退出')
    args = ap.parse_args()

    devices = []
    for i, spec in enumerate(args.imu, start=1):
        try:
            dev_type, ident = parse_imu_spec(spec)
        except ValueError as e:
            print(e)
            sys.exit(1)
        devices.append(ImuDevice(dev_type, ident, label=f'imu{i}'))

    if args.probe:
        run_probe(args, devices)
        return

    t = threading.Thread(target=ble_thread_main, args=(devices, args.scan_timeout), daemon=True)
    t.start()

    print('等待 BLE 连接中...')
    time.sleep(2.0)

    run_camera(args, devices)

    stop_event.set()
    t.join(timeout=3.0)


if __name__ == '__main__':
    main()
