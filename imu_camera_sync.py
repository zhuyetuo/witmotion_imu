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

所有 IMU 时间戳均使用 PC 系统时间（time.time()），与摄像头帧时间戳对齐。

依赖:
    pip install bleak opencv-python

用法:
    # WitMotion，按名称查找，录 10 秒
    python imu_camera_sync.py --device wit --name WTSDCL --duration 10 -o rec

    # HICC，按 MAC 地址，录 10 秒
    python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --duration 10 -o rec

    # 实时显示（不保存文件）
    python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B

    # 指定摄像头编号（默认 0）
    python imu_camera_sync.py --device wit --name WTSDCL --camera 1
"""

import argparse
import asyncio
import csv
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import cv2
except ImportError:
    print('缺少 opencv-python，请先安装: pip install opencv-python')
    sys.exit(1)

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print('缺少 bleak，请先安装: pip install bleak')
    sys.exit(1)

# ── 共享状态 ────────────────────────────────────────────────────────────────

imu_queue: queue.Queue = queue.Queue(maxsize=500)  # IMU帧缓冲，主线程消费
stop_event = threading.Event()   # 通知所有线程退出


# ── WitMotion 采集 ──────────────────────────────────────────────────────────

def _setup_wit():
    try:
        from wit_ble_live import DEFAULT_NOTIFY_CANDIDATES, find_device, StreamingByteBuffer
        from wit_ble_live import parse_one_packet
    except ImportError as e:
        print(f'导入 wit_ble_live 失败: {e}')
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

    candidates = [args.notify_uuid] if args.notify_uuid else DEFAULT_NOTIFY_CANDIDATES
    buf = StreamingByteBuffer()

    def on_data(_, data: bytearray):
        pc_ms = time.time() * 1000.0
        packets = buf.feed(bytes(data))
        for pkt in packets:
            p = parse_one_packet(pkt)
            if p is None:
                continue
            row = {
                'pc_ms':  pc_ms,
                'acc_x':  p['acc_x'],
                'acc_y':  p['acc_y'],
                'acc_z':  p['acc_z'],
                'gyro_x': p['gyro_x'],
                'gyro_y': p['gyro_y'],
                'gyro_z': p['gyro_z'],
            }
            try:
                imu_queue.put_nowait(row)
            except queue.Full:
                pass

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

        await client.stop_notify(subscribed)

    print('WitMotion BLE 已断开。')


# ── HICC 采集 ───────────────────────────────────────────────────────────────

def _setup_hicc():
    try:
        from hicc_ble_debug import (
            FrameBuffer, parse_dp_sequence,
            find_tx_uuid, find_rx_uuid, send_timesync,
            DP_ACC_X, DP_ACC_Y, DP_ACC_Z,
            DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z,
            CMD_REPORT,
        )
    except ImportError as e:
        print(f'导入 hicc_ble_debug 失败: {e}')
        sys.exit(1)
    return (FrameBuffer, parse_dp_sequence, find_tx_uuid, find_rx_uuid,
            send_timesync, DP_ACC_X, DP_ACC_Y, DP_ACC_Z,
            DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z, CMD_REPORT)


async def _run_hicc(args):
    (FrameBuffer, parse_dp_sequence, find_tx_uuid, find_rx_uuid,
     send_timesync, DP_ACC_X, DP_ACC_Y, DP_ACC_Z,
     DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z, CMD_REPORT) = _setup_hicc()

    if not args.address:
        print('HICC 设备需要指定 --address')
        stop_event.set()
        return

    print(f'连接 HICC 设备: {args.address}')
    fb = FrameBuffer()

    def on_data(_, data: bytearray):
        pc_ms = time.time() * 1000.0
        frames = fb.feed(bytes(data))
        for frame in frames:
            cmd = frame[3]
            if cmd != CMD_REPORT:
                continue
            payload = frame[6:-1]
            dps = parse_dp_sequence(payload)
            if DP_ACC_X not in dps or DP_GYRO_X not in dps:
                continue
            row = {
                'pc_ms':  pc_ms,
                'acc_x':  dps[DP_ACC_X]  / 1_000_000.0,
                'acc_y':  dps[DP_ACC_Y]  / 1_000_000.0,
                'acc_z':  dps[DP_ACC_Z]  / 1_000_000.0,
                'gyro_x': dps[DP_GYRO_X] / 1_000_000.0,
                'gyro_y': dps[DP_GYRO_Y] / 1_000_000.0,
                'gyro_z': dps[DP_GYRO_Z] / 1_000_000.0,
            }
            try:
                imu_queue.put_nowait(row)
            except queue.Full:
                pass

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
        stop_event.set()
    finally:
        loop.close()


# ── 主循环（摄像头 + 显示 + 录制） ─────────────────────────────────────────

def draw_imu_overlay(frame, imu: dict | None, frame_idx: int, elapsed: float,
                     recording: bool, cam_fps: float, imu_fps: float, target_fps: int):
    h, w = frame.shape[:2]
    overlay = frame.copy()

    cv2.rectangle(overlay, (0, 0), (w, 185), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.45, frame, 0.55, 0, frame)

    def put(text, row, color=(200, 255, 200)):
        cv2.putText(frame, text, (12, 28 + row * 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 1, cv2.LINE_AA)

    ts = datetime.now().strftime('%H:%M:%S.%f')[:12]
    rec_tag = '  [REC]' if recording else ''
    put(f'{ts}  t={elapsed:.1f}s{rec_tag}', 0, (255, 255, 100))

    # 颜色：实际与目标相差 >20% 时变红提示
    def rate_color(actual, target):
        return (100, 100, 255) if abs(actual - target) / max(target, 1) > 0.2 else (255, 200, 100)

    put(f'CAM {cam_fps:5.1f} fps  (target {target_fps} fps)', 1, rate_color(cam_fps, target_fps))
    put(f'IMU {imu_fps:5.1f} Hz   (target {target_fps} Hz)', 2, rate_color(imu_fps, target_fps))

    if imu:
        put(f"Acc  X={imu['acc_x']:+7.3f}  Y={imu['acc_y']:+7.3f}  Z={imu['acc_z']:+7.3f}  m/s2", 3)
        put(f"Gyro X={imu['gyro_x']:+7.4f}  Y={imu['gyro_y']:+7.4f}  Z={imu['gyro_z']:+7.4f}  rad/s", 4)
    else:
        put('Waiting for IMU...', 3, (100, 100, 255))

    return frame


def run_camera(args):
    target_fps = args.fps
    frame_interval = 1.0 / target_fps  # 目标帧间隔（秒）

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
    out_prefix  = args.output or f'rec_{datetime.now().strftime("%Y%m%d_%H%M%S")}'

    video_writer = None
    imu_csv_file = None
    imu_csv_writer = None

    if record_mode and args.output:
        video_path = f'{out_prefix}_video.mp4'
        imu_path   = f'{out_prefix}_imu.csv'
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        video_writer = cv2.VideoWriter(video_path, fourcc, float(target_fps), (actual_w, actual_h))
        imu_csv_file   = open(imu_path, 'w', newline='', encoding='utf-8-sig')
        imu_csv_writer = csv.writer(imu_csv_file)
        imu_csv_writer.writerow(['pc_timestamp', 'acc_x_ms2', 'acc_y_ms2', 'acc_z_ms2',
                                  'gyro_x_rads', 'gyro_y_rads', 'gyro_z_rads'])
        print(f'录制模式: {args.duration}s  视频→{video_path}  IMU→{imu_path}')
    elif record_mode:
        print('录制模式（无 -o 参数，不保存文件）。')
    else:
        print('实时模式（按 Q 或 Ctrl+C 退出）。')

    start_time   = time.time()
    next_tick    = start_time      # 下一帧的目标时刻（用于精确限速）
    frame_idx    = 0
    last_imu: dict | None = None
    elapsed      = 0.0

    # FPS 统计：滑动 1 秒窗口
    cam_ts_window: list[float] = []
    imu_ts_window: list[float] = []

    def fps_from_window(ts_list: list[float], now: float) -> float:
        cutoff = now - 1.0
        while ts_list and ts_list[0] < cutoff:
            ts_list.pop(0)
        return float(len(ts_list))

    # IMU 按目标频率降采样：每 1/target_fps 秒最多输出一帧
    imu_next_emit = start_time

    try:
        while not stop_event.is_set():
            # ── 精确限速：等到 next_tick 再读帧 ──
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
            frame_idx += 1
            elapsed = cam_ts - start_time
            cam_ts_window.append(cam_ts)

            # 排空 IMU 队列，记录所有帧时间戳（用于实际 Hz 统计）
            # CSV/显示只取最新一帧（与当前视频帧对齐）
            latest_imu: dict | None = None
            while True:
                try:
                    r = imu_queue.get_nowait()
                    imu_ts_window.append(r['pc_ms'] / 1000.0)
                    latest_imu = r
                except queue.Empty:
                    break
            if latest_imu is not None:
                last_imu = latest_imu

            # CSV：每帧写一条 IMU（与视频帧 1:1），目标频率控制
            if imu_csv_writer and last_imu and cam_ts >= imu_next_emit:
                r = last_imu
                ts_str = datetime.fromtimestamp(r['pc_ms'] / 1000.0).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                imu_csv_writer.writerow([
                    ts_str,
                    f"{r['acc_x']:.6f}", f"{r['acc_y']:.6f}", f"{r['acc_z']:.6f}",
                    f"{r['gyro_x']:.6f}", f"{r['gyro_y']:.6f}", f"{r['gyro_z']:.6f}",
                ])
                imu_next_emit += frame_interval

            cam_fps = fps_from_window(cam_ts_window, cam_ts)
            imu_fps = fps_from_window(imu_ts_window, cam_ts)

            frame = draw_imu_overlay(frame, last_imu, frame_idx, elapsed,
                                     recording=(record_mode and args.output is not None),
                                     cam_fps=cam_fps, imu_fps=imu_fps,
                                     target_fps=target_fps)

            if video_writer:
                video_writer.write(frame)

            cv2.imshow('IMU + Camera Sync', frame)

            if record_mode and elapsed >= args.duration:
                print(f'\n已达到录制时长 {args.duration}s，停止。')
                break

            key = cv2.waitKey(1) & 0xFF
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
        cv2.destroyAllWindows()
        print(f'\n共采集 {frame_idx} 帧视频  {elapsed:.1f}s  目标 {target_fps} fps')
        if args.output and record_mode:
            print(f'已保存: {out_prefix}_video.mp4  {out_prefix}_imu.csv')


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='IMU + 摄像头同步采集')
    ap.add_argument('--device', choices=['wit', 'hicc'], required=True,
                    help='IMU 设备类型: wit=WitMotion(20Hz)  hicc=HICC_PetCollar(25Hz)')
    ap.add_argument('--name',    help='BLE 设备名称关键字（WitMotion 用）')
    ap.add_argument('--address', help='BLE MAC 地址（HICC 必须，WitMotion 可选）')
    ap.add_argument('--notify-uuid', dest='notify_uuid', default=None,
                    help='手动指定 WitMotion Notify UUID（找不到数据时用）')
    ap.add_argument('--camera', type=int, default=0,
                    help='摄像头编号，默认 0')
    ap.add_argument('--fps', type=int, default=20,
                    choices=range(1, 31), metavar='N',
                    help='目标帧率/采样率（1-30，默认 20），视频和 IMU 输出同步到此频率')
    ap.add_argument('--duration', type=float, default=0,
                    help='录制时长（秒），0 或不填=实时模式，不自动停止')
    ap.add_argument('-o', '--output', default=None,
                    help='输出文件前缀（录制模式下生成 <prefix>_video.mp4 和 <prefix>_imu.csv）')
    args = ap.parse_args()

    if args.device == 'wit' and not args.name and not args.address:
        ap.error('WitMotion 设备请指定 --name 或 --address')

    # BLE 在后台线程跑 asyncio，摄像头在主线程（OpenCV 要求）
    t = threading.Thread(target=ble_thread_main, args=(args,), daemon=True)
    t.start()

    print('等待 BLE 连接中...')
    time.sleep(2.0)  # 给 BLE 一点启动时间再开摄像头

    run_camera(args)

    stop_event.set()
    t.join(timeout=3.0)


if __name__ == '__main__':
    main()
