# -*- coding: utf-8 -*-
"""
IMU + 摄像头同步采集脚本
=========================

支持设备:
    --device wit   WitMotion WT901SDCL-BT50（20Hz，BLE）
    --device hicc  HICC_PetCollar 自制设备（25Hz，BLE）

两种模式:
    录制模式（--duration N）  采集 N 秒后自动停止，保存视频 + IMU CSV
    实时模式（不加 --duration 或 --duration 0）  显示实时画面+IMU数值，Ctrl+C 停止

输出两个 CSV（每视频帧一行，与视频严格1:1对齐）:

{base}.csv（Label Studio 兼容格式）:
    timestamp      视频帧采集时刻（%Y-%m-%d %H:%M:%S.%L）
    acc_x/y/z      加速度（缺失帧写空）
    gyro_x/y/z     角速度（缺失帧写空）

{base}_meta.csv（对齐质量/调试信息）:
    frame_idx      视频帧序号（从1开始）
    cam_timestamp  视频帧采集时刻
    imu_timestamp  匹配到的 IMU 样本的芯片时间
    imu_lag_ms     IMU样本与视频帧的时间差（ms），越小对齐越好
    imu_missing    1=此帧未找到有效 IMU 数据
    cam_fps        此时刻摄像头帧率（滑动1秒窗口）
    imu_hz         此时刻 IMU 采样率（滑动1秒窗口）

依赖:
    pip install bleak opencv-python

用法:
    # HICC，录 60 秒（默认保存带叠加信息的视频）
    python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --duration 60

    # WitMotion，实时显示，不保存
    python imu_camera_sync.py --device wit --name WTSDCL

    # 保存干净视频（不含叠加信息）
    python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --duration 60 --no-save-overlay
"""

import argparse
import asyncio
import csv
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

# ── 共享状态 ────────────────────────────────────────────────────────────────

# IMU 环形缓冲，保留最近 10 秒的样本（25Hz × 10s = 250 条）
# 每个元素: {'pc_ms': float, 'imu_ts': str, 'acc_x': ..., ...}
_imu_buffer: deque = deque(maxlen=500)
_imu_lock   = threading.Lock()

stop_event = threading.Event()
ble_mac: list[str] = ['unknown']

# IMU Hz 统计（BLE线程侧）
_imu_ts_window: list[float] = []
_imu_hz_lock   = threading.Lock()


def _push_imu(row: dict):
    """BLE 线程调用：把一条 IMU 数据推入缓冲，同时更新 Hz 窗口。"""
    now = time.time()
    with _imu_lock:
        _imu_buffer.append(row)
    with _imu_hz_lock:
        _imu_ts_window.append(now)


def _current_imu_hz() -> float:
    """主线程调用：读取当前 IMU 实际采样率（滑动1秒窗口）。"""
    now = time.time()
    cutoff = now - 1.0
    with _imu_hz_lock:
        while _imu_ts_window and _imu_ts_window[0] < cutoff:
            _imu_ts_window.pop(0)
        return float(len(_imu_ts_window))


def _find_nearest_imu(cam_ts_ms: float, max_lag_ms: float = 300.0):
    """
    在缓冲中找与 cam_ts_ms 时间最近的 IMU 样本。
    返回 (row, lag_ms, is_missing)。
    is_missing=True 表示最近样本时间差超过 max_lag_ms。
    """
    with _imu_lock:
        if not _imu_buffer:
            return None, float('inf'), True
        best = min(_imu_buffer, key=lambda r: abs(r['pc_ms'] - cam_ts_ms))
    lag = abs(best['pc_ms'] - cam_ts_ms)
    return best, lag, lag > max_lag_ms


# ── WitMotion 采集 ──────────────────────────────────────────────────────────

def _setup_wit():
    try:
        from wit_parse import DEFAULT_NOTIFY_CANDIDATES, StreamingByteBuffer, parse_one_packet
        from ble_utils import find_device
    except ImportError as e:
        print(f'导入 wit_parse / ble_utils 失败: {e}')
        sys.exit(1)
    return DEFAULT_NOTIFY_CANDIDATES, find_device, StreamingByteBuffer, parse_one_packet


async def _run_wit(args):
    DEFAULT_NOTIFY_CANDIDATES, find_device, StreamingByteBuffer, parse_one_packet = _setup_wit()

    device = await find_device(args.name, args.address)
    if device is None:
        print('找不到 WitMotion 设备，请检查名称/地址或确认设备已开机且未被其他程序占用。')
        stop_event.set()
        return

    print(f'WitMotion 已连接: {device.name}  {device.address}')
    ble_mac[0] = device.address

    candidates = [args.notify_uuid] if args.notify_uuid else DEFAULT_NOTIFY_CANDIDATES
    buf = StreamingByteBuffer()

    def on_data(_, data: bytearray):
        pc_ms = time.time() * 1000.0
        packets = buf.feed(bytes(data))
        for pkt in packets:
            p = parse_one_packet(pkt)
            if p is None or p['chip_time'] is None:
                continue
            ct = p['chip_time']
            imu_ts = ct.strftime('%Y-%m-%d %H:%M:%S.') + f'{ct.microsecond // 1000:03d}'
            _push_imu({
                'pc_ms':  pc_ms,
                'imu_ts': imu_ts,
                'acc_x':  p['acc'][0],
                'acc_y':  p['acc'][1],
                'acc_z':  p['acc'][2],
                'gyro_x': p['gyro'][0],
                'gyro_y': p['gyro'][1],
                'gyro_z': p['gyro'][2],
            })

    async with BleakClient(device) as client:
        subscribed = None
        for uuid in candidates:
            try:
                await client.start_notify(uuid, on_data)
                subscribed = uuid
                print(f'已订阅 WitMotion Notify: {uuid}')
                break
            except Exception:
                continue
        if subscribed is None:
            print('订阅 WitMotion Notify 失败，请用 --notify-uuid 手动指定 UUID。')
            stop_event.set()
            return

        while not stop_event.is_set():
            await asyncio.sleep(0.1)

        try:
            await client.stop_notify(subscribed)
        except Exception:
            pass

    print('WitMotion BLE 已断开。')


# ── HICC 采集 ───────────────────────────────────────────────────────────────

def _setup_hicc():
    try:
        from hicc_parse import (
            FrameBuffer, parse_dp_sequence,
            find_tx_uuid, find_rx_uuid, send_timesync,
            DP_ACC_X, DP_ACC_Y, DP_ACC_Z,
            DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z,
            DP_TIMESTAMP, CMD_REPORT,
        )
    except ImportError as e:
        print(f'导入 hicc_parse 失败: {e}')
        sys.exit(1)
    return (FrameBuffer, parse_dp_sequence, find_tx_uuid, find_rx_uuid,
            send_timesync, DP_ACC_X, DP_ACC_Y, DP_ACC_Z,
            DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z, DP_TIMESTAMP, CMD_REPORT)


async def _run_hicc(args):
    (FrameBuffer, parse_dp_sequence, find_tx_uuid, find_rx_uuid,
     send_timesync, DP_ACC_X, DP_ACC_Y, DP_ACC_Z,
     DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z, DP_TIMESTAMP, CMD_REPORT) = _setup_hicc()

    if not args.address:
        print('HICC 设备需要指定 --address')
        stop_event.set()
        return

    print(f'连接 HICC 设备: {args.address}')
    ble_mac[0] = args.address
    fb = FrameBuffer()

    def on_data(_, data: bytearray):
        pc_ms = time.time() * 1000.0
        frames = fb.feed(bytes(data))
        for frame in frames:
            if frame[3] != CMD_REPORT:
                continue
            dps = parse_dp_sequence(frame[6:-1])
            if DP_ACC_X not in dps or DP_GYRO_X not in dps:
                continue
            chip_ms = dps.get(DP_TIMESTAMP, 0)
            try:
                imu_ts = (datetime.utcfromtimestamp(chip_ms / 1000.0)
                          .strftime('%Y-%m-%d %H:%M:%S.') + f'{chip_ms % 1000:03d}')
            except (OSError, OverflowError, ValueError):
                imu_ts = ''
            _push_imu({
                'pc_ms':  pc_ms,
                'imu_ts': imu_ts,
                'acc_x':  dps[DP_ACC_X]  / 1_000_000.0,
                'acc_y':  dps[DP_ACC_Y]  / 1_000_000.0,
                'acc_z':  dps[DP_ACC_Z]  / 1_000_000.0,
                'gyro_x': dps[DP_GYRO_X] / 1_000_000.0,
                'gyro_y': dps[DP_GYRO_Y] / 1_000_000.0,
                'gyro_z': dps[DP_GYRO_Z] / 1_000_000.0,
            })

    async with BleakClient(args.address) as client:
        tx_uuid = await find_tx_uuid(client)
        rx_uuid = await find_rx_uuid(client)
        if tx_uuid is None:
            print('找不到 HICC TX 特征值，请确认设备和 UUID。')
            stop_event.set()
            return
        if rx_uuid:
            await send_timesync(client, rx_uuid)
        await client.start_notify(tx_uuid, on_data)
        print(f'已订阅 HICC TX: {tx_uuid}')
        while not stop_event.is_set():
            await asyncio.sleep(0.1)
        await client.stop_notify(tx_uuid)

    print('HICC BLE 已断开。')


# ── BLE 线程入口 ────────────────────────────────────────────────────────────

def ble_thread_main(args):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        if args.device == 'wit':
            loop.run_until_complete(_run_wit(args))
        else:
            loop.run_until_complete(_run_hicc(args))
    except Exception as e:
        print(f'BLE 线程异常: {e}')
    finally:
        stop_event.set()
        loop.close()


# ── 画面叠加信息 ─────────────────────────────────────────────────────────────

# Label Studio 兼容格式（主 CSV）
CSV_HEADER = ['timestamp', 'acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z']

# 对齐质量 / 调试信息（副 CSV，包含全部字段）
META_HEADER = [
    'frame_idx', 'cam_timestamp', 'imu_timestamp',
    'imu_lag_ms', 'imu_missing',
    'acc_x', 'acc_y', 'acc_z',
    'gyro_x', 'gyro_y', 'gyro_z',
    'cam_fps', 'imu_hz',
]


def draw_imu_overlay(frame, imu: dict | None, imu_lag_ms: float, imu_missing: bool,
                     frame_idx: int, elapsed: float, recording: bool,
                     cam_fps: float, imu_hz: float, target_fps: int):
    h, w = frame.shape[:2]
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (w, 210), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.45, frame, 0.55, 0, frame)

    def put(text, row, color=(200, 255, 200)):
        cv2.putText(frame, text, (12, 28 + row * 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 1, cv2.LINE_AA)

    # 行0：时间 + 帧号 + 录制标记
    ts = datetime.now().strftime('%H:%M:%S.%f')[:12]
    rec_tag = '  [REC]' if recording else ''
    put(f'{ts}  #{frame_idx}  t={elapsed:.1f}s{rec_tag}', 0, (255, 255, 100))

    # 颜色：实际与目标相差 >20% 变红
    def rate_color(actual, target):
        return (80, 80, 255) if abs(actual - target) / max(target, 1) > 0.2 else (255, 200, 100)

    # 行1：摄像头帧率
    put(f'CAM {cam_fps:5.1f} fps  (target {target_fps} fps)', 1, rate_color(cam_fps, target_fps))

    # 行2：IMU 采样率
    put(f'IMU {imu_hz:5.1f} Hz   (target {target_fps} Hz)', 2, rate_color(imu_hz, target_fps))

    # 行3：IMU 对齐延迟（颜色：<50ms 绿，50~150ms 黄，>150ms 红）
    if imu_missing:
        lag_color = (80, 80, 255)
        lag_str = 'IMU MISSING'
    elif imu_lag_ms < 50:
        lag_color = (100, 255, 100)
        lag_str = f'IMU lag {imu_lag_ms:.0f} ms'
    elif imu_lag_ms < 150:
        lag_color = (50, 200, 255)
        lag_str = f'IMU lag {imu_lag_ms:.0f} ms'
    else:
        lag_color = (80, 80, 255)
        lag_str = f'IMU lag {imu_lag_ms:.0f} ms  !'
    put(lag_str, 3, lag_color)

    # 行4/5：IMU 数值
    if imu and not imu_missing:
        put(f"Acc  X={imu['acc_x']:+7.3f}  Y={imu['acc_y']:+7.3f}  Z={imu['acc_z']:+7.3f}", 4)
        put(f"Gyro X={imu['gyro_x']:+7.4f}  Y={imu['gyro_y']:+7.4f}  Z={imu['gyro_z']:+7.4f}", 5)
    else:
        put('Waiting for IMU...', 4, (80, 80, 255))

    return frame


# ── 主循环（摄像头 + 显示 + 录制） ─────────────────────────────────────────

def run_camera(args):
    target_fps    = args.fps
    frame_interval = 1.0 / target_fps
    save_overlay  = not args.no_save_overlay

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f'无法打开摄像头 {args.camera}，请检查 --camera 参数。')
        stop_event.set()
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    cap.set(cv2.CAP_PROP_FPS, target_fps)
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f'摄像头分辨率: {actual_w}x{actual_h}  目标帧率: {target_fps} fps')

    record_mode = args.duration and args.duration > 0
    ts_tag  = datetime.now().strftime('%Y%m%d_%H%M%S')
    dev_tag = args.device
    mac_tag = ble_mac[0].replace(':', '').lower()
    base    = f'data/{dev_tag}_{mac_tag}_{ts_tag}'

    video_writer    = None
    imu_csv_file    = None
    imu_csv_writer  = None
    meta_csv_file   = None
    meta_csv_writer = None

    if record_mode:
        video_path = f'{base}.mp4'
        imu_path   = f'{base}.csv'
        meta_path  = f'{base}_meta.csv'
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        video_writer   = cv2.VideoWriter(video_path, fourcc, float(target_fps), (actual_w, actual_h))
        imu_csv_file   = open(imu_path,  'w', newline='', encoding='utf-8-sig')
        imu_csv_writer = csv.writer(imu_csv_file)
        imu_csv_writer.writerow(CSV_HEADER)
        meta_csv_file   = open(meta_path, 'w', newline='', encoding='utf-8-sig')
        meta_csv_writer = csv.writer(meta_csv_file)
        meta_csv_writer.writerow(META_HEADER)
        overlay_note = '（含叠加信息）' if save_overlay else '（干净画面）'
        print(f'录制模式: {args.duration}s  视频{overlay_note}→{video_path}')
        print(f'  IMU(Label Studio)→{imu_path}  对齐信息→{meta_path}')
    else:
        print('实时模式（按 Q 或 Ctrl+C 退出）。')

    start_time = time.time()
    next_tick  = start_time
    frame_idx  = 0
    elapsed    = 0.0

    # 摄像头 fps 滑动窗口
    cam_ts_window: list[float] = []

    def cam_fps_tick(now: float) -> float:
        cutoff = now - 1.0
        while cam_ts_window and cam_ts_window[0] < cutoff:
            cam_ts_window.pop(0)
        cam_ts_window.append(now)
        return float(len(cam_ts_window))

    # IMU 最大允许延迟：3个 IMU 周期（BLE 偶尔批量推送，给一点宽容）
    max_lag_ms = 3 * (1000.0 / target_fps)

    try:
        while not stop_event.is_set():
            # 精确限速
            now = time.time()
            sleep_s = next_tick - now
            if sleep_s > 0:
                time.sleep(sleep_s)
            next_tick += frame_interval

            ret, frame = cap.read()
            if not ret:
                print('摄像头读取失败，退出。')
                break

            cam_ts    = time.time()
            cam_ts_ms = cam_ts * 1000.0
            frame_idx += 1
            elapsed   = cam_ts - start_time

            # 跳过录制开始前的帧（BLE 启动期间积压）
            if cam_ts < start_time:
                continue

            cam_fps = cam_fps_tick(cam_ts)
            imu_hz  = _current_imu_hz()

            # 找时间戳最近的 IMU 样本
            imu_row, lag_ms, missing = _find_nearest_imu(cam_ts_ms, max_lag_ms)

            cam_ts_str = datetime.fromtimestamp(cam_ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            # CSV：每视频帧写一行，与视频严格1:1
            if missing or imu_row is None:
                acc  = ['', '', '']
                gyro = ['', '', '']
                lag_str = f'{lag_ms:.1f}' if lag_ms != float('inf') else ''
                imu_ts_str = ''
                missing_flag = 1
            else:
                acc  = [f"{imu_row['acc_x']:.6f}",  f"{imu_row['acc_y']:.6f}",  f"{imu_row['acc_z']:.6f}"]
                gyro = [f"{imu_row['gyro_x']:.6f}", f"{imu_row['gyro_y']:.6f}", f"{imu_row['gyro_z']:.6f}"]
                lag_str = f'{lag_ms:.1f}'
                imu_ts_str = imu_row.get('imu_ts', '')
                missing_flag = 0

            if imu_csv_writer:
                # Label Studio 格式：timestamp + acc/gyro
                imu_csv_writer.writerow([cam_ts_str] + acc + gyro)

            if meta_csv_writer:
                # 全量信息：对齐质量 + acc/gyro
                meta_csv_writer.writerow([
                    frame_idx, cam_ts_str, imu_ts_str,
                    lag_str, missing_flag,
                    *acc, *gyro,
                    f'{cam_fps:.1f}', f'{imu_hz:.1f}',
                ])

            # 生成叠加画面
            display = draw_imu_overlay(
                frame.copy(), imu_row, lag_ms if not missing else lag_ms,
                missing, frame_idx, elapsed, recording=record_mode,
                cam_fps=cam_fps, imu_hz=imu_hz, target_fps=target_fps,
            )

            # 保存视频（可选带叠加信息）
            if video_writer:
                video_writer.write(display if save_overlay else frame)

            try:
                cv2.imshow('IMU + Camera Sync', display)
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
            video_writer.release()
        if imu_csv_file:
            imu_csv_file.close()
        if meta_csv_file:
            meta_csv_file.close()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass
        print(f'\n共采集 {frame_idx} 帧视频  {elapsed:.1f}s  目标 {target_fps} fps')
        if record_mode:
            print(f'已保存: {base}.mp4')
            print(f'       {base}.csv（Label Studio）')
            print(f'       {base}_meta.csv（全量信息）')


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='IMU + 摄像头同步采集')
    ap.add_argument('--device', choices=['wit', 'hicc'], required=True,
                    help='IMU 设备类型: wit=WitMotion  hicc=HICC_PetCollar')
    ap.add_argument('--name',    help='BLE 设备名称关键字（WitMotion 用）')
    ap.add_argument('--address', help='BLE MAC 地址（HICC 必须，WitMotion 可选）')
    ap.add_argument('--notify-uuid', dest='notify_uuid', default=None,
                    help='手动指定 WitMotion Notify UUID')
    ap.add_argument('--camera', type=int, default=0,
                    help='摄像头编号，默认 0')
    ap.add_argument('--fps', type=int, default=20,
                    choices=range(1, 31), metavar='N',
                    help='目标帧率（1-30，默认 20）')
    ap.add_argument('--duration', type=float, default=0,
                    help='录制时长（秒），0=实时模式不保存')
    ap.add_argument('--no-save-overlay', action='store_true',
                    help='保存干净视频（不含叠加信息）；默认保存带叠加信息的视频，便于标注时参考')
    args = ap.parse_args()

    if args.device == 'wit' and not args.name and not args.address:
        ap.error('WitMotion 设备请指定 --name 或 --address')

    t = threading.Thread(target=ble_thread_main, args=(args,), daemon=True)
    t.start()

    print('等待 BLE 连接中...')
    time.sleep(2.0)

    run_camera(args)

    stop_event.set()
    t.join(timeout=3.0)


if __name__ == '__main__':
    main()
