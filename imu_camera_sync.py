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
    cam_timestamp  视频帧采集时刻（PC系统时间）
    imu_timestamp  匹配到的 IMU 样本到达时刻（PC系统时间，非芯片时间，芯片时间可能未校准）
    imu_lag_ms     IMU样本与视频帧的时间差（ms），越小对齐越好
    imu_missing    1=此帧未找到有效 IMU 数据
    cam_fps        此时刻摄像头帧率（滑动1秒窗口）
    imu_hz         此时刻 IMU 采样率（滑动1秒窗口）

{base}_raw.csv（原始 IMU 全量流水，不受摄像头帧率影响）:
    pc_ms, acc_x/y/z, gyro_x/y/z    每条真实到达的 IMU 样本原样记录

{base}_resampled{HZ}hz.csv（降采样结果，Label Studio 兼容格式）:
    录制结束后自动用 --resample-hz 指定的目标频率对 {base}_raw.csv 做低通+
    线性插值降采样生成，与视频帧率/对齐无关，方便按需生成 20/16/15Hz 等
    目标训练频率的数据，不用等到训练脚本里再处理。

同步模式:
    默认事件驱动（等待新 IMU 样本到达再抓帧），摄像头与 IMU 天然对齐，
    避免同一 IMU 样本被多帧复用；--no-imu-sync 可切回固定定时器模式。

预热:
    录制模式默认先预热 5 秒（--warmup-sec 调整，设 0 关闭），期间持续抓帧
    丢弃、不写入任何文件，等摄像头自动曝光/帧率、IMU 连接都稳定后再正式
    开始计时录制，避免刚开始那几秒帧率/数据不稳定混进正式数据里。

录制模式结束后会自动调用 check_alignment.py 打印视频/CSV 对齐校验结果。

视频写入:
    默认通过 ffmpeg 管道以可变帧率(VFR)写入，每帧 PTS 直接取写入时刻的真实
    系统时间（-use_wallclock_as_timestamps），因此视频时长天然等于真实录制
    时长，与 CSV 时间戳精确对应，无需（也无法）事后修正 fps。未安装 ffmpeg
    时退化为固定 fps 写入，时长可能有 0.1s 级别误差。

    编码优先用 H.264（libx264，压缩率远高于旧版默认的 mpeg4，同画质下体积
    通常小 5~10 倍），--video-crf 调整压缩质量（默认 28，数值越大文件越小）；
    没有 libx264 的 ffmpeg 构建才退化用 mpeg4。

依赖:
    pip install bleak opencv-python
    ffmpeg（用于精确对齐的视频写入，未安装会自动退化并提示）

用法:
    # 先探测硬件能力：摄像头最大分辨率/实测fps + IMU当前实际输出频率
    python imu_camera_sync.py --device wit --name WTSDCL --probe

    # HICC，录 60 秒（默认保存带叠加信息的视频）
    python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --duration 60

    # WitMotion，实时显示，不保存
    python imu_camera_sync.py --device wit --name WTSDCL

    # 保存干净视频（不含叠加信息）
    python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --duration 60 --no-save-overlay

    # 降采样目标频率改成 16Hz（默认 25Hz）
    python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --resample-hz 16

    # 预热时间改成 8 秒（默认 5 秒），关闭预热用 --warmup-sec 0
    python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --warmup-sec 8

    # 循环录制：每段60秒，录完自动开始下一段，直到按 Q/ESC 或 Ctrl+C 才停止
    python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --loop

    # 指定保存目录
    python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --out-dir data/session1

    # 只保留降采样版文件（resampled mp4/csv），其余中间文件自动删除
    python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --resample-hz 16 --resample-only
"""

import argparse
import asyncio
import csv
import os
import shutil
import subprocess
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
# 每个元素: {'pc_ms': float, 'seq': int, 'acc_x': ..., ...}
_imu_buffer: deque = deque(maxlen=500)
_imu_lock   = threading.Lock()

stop_event = threading.Event()
ble_mac: list[str] = ['unknown']

# IMU Hz 统计（BLE线程侧）
_imu_ts_window: list[float] = []
_imu_hz_lock   = threading.Lock()

# 新样本到达事件：用于摄像头“等待 IMU 新样本”事件驱动模式
_imu_new_event = threading.Event()
_imu_seq_counter = 0

# 原始 IMU 全量流水日志（不受摄像头帧率影响，用于事后独立降采样）
_raw_csv_writer = None
RAW_CSV_HEADER = ['pc_ms', 'acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z']


def _set_raw_csv_writer(writer):
    """线程安全地设置/清空原始 IMU 日志的 writer，避免文件已关闭后 BLE 线程仍尝试写入。"""
    global _raw_csv_writer
    with _imu_lock:
        _raw_csv_writer = writer


def _push_imu(row: dict):
    """BLE 线程调用：把一条 IMU 数据推入缓冲，同时更新 Hz 窗口并触发新样本事件。"""
    global _imu_seq_counter
    now = time.time()
    with _imu_lock:
        _imu_seq_counter += 1
        row['seq'] = _imu_seq_counter
        _imu_buffer.append(row)
        if _raw_csv_writer is not None:
            _raw_csv_writer.writerow([
                f"{row['pc_ms']:.3f}",
                f"{row['acc_x']:.6f}", f"{row['acc_y']:.6f}", f"{row['acc_z']:.6f}",
                f"{row['gyro_x']:.6f}", f"{row['gyro_y']:.6f}", f"{row['gyro_z']:.6f}",
            ])
    with _imu_hz_lock:
        _imu_ts_window.append(now)
    _imu_new_event.set()


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
            if p is None:
                continue
            # 只用 PC 时间（pc_ms）做时间戳，不依赖芯片时间：芯片时间需要设备
            # 用官方上位机校准过才有效，未校时的设备 chip_time 恒为 None，
            # 之前误把它当成过滤条件会导致这类设备的数据被整体丢弃。
            _push_imu({
                'pc_ms':  pc_ms,
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
            _push_imu({
                'pc_ms':  pc_ms,
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

_TS_FMT = '%Y-%m-%d %H:%M:%S.%f'


def resample_raw_imu(raw_path: str, out_path: str, target_hz: float,
                      t_start_ms: float = None, t_end_ms: float = None):
    """
    独立于摄像头帧率，把 {base}_raw.csv 里的完整原始 IMU 流降采样到 target_hz。
    降采样前先做一次简单的滑动平均低通滤波（窗口按原始/目标采样率之比估算），
    减少直接抽稀带来的走样（aliasing），再用线性插值取到目标频率的等间隔时刻点。

    t_start_ms/t_end_ms: 若提供，输出的时间范围裁到 [t_start_ms, t_end_ms]
    （通常传视频第一帧/最后一帧的真实 cam_timestamp），保证降采样结果与视频
    的起止时间、时长严格对齐；不提供则退化为用 raw.csv 自身的首尾时间
    （可能比视频略宽，因为 BLE 数据流启停时刻与视频帧采集不完全同步）。
    """
    try:
        import numpy as np
    except ImportError:
        print('缺少 numpy（opencv-python 一般会带），跳过降采样输出。')
        return

    rows = []
    with open(raw_path, newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            rows.append(r)
    if len(rows) < 2:
        print(f'原始 IMU 样本太少，跳过降采样: {raw_path}')
        return

    t = np.array([float(r['pc_ms']) for r in rows])
    cols = {name: np.array([float(r[name]) for r in rows])
            for name in ('acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z')}

    step_ms = 1000.0 / target_hz
    avg_dt_ms = float(np.mean(np.diff(t))) if len(t) > 1 else step_ms
    window = max(1, int(round(step_ms / avg_dt_ms))) if avg_dt_ms > 0 else 1
    if window > 1:
        kernel = np.ones(window) / window
        for name in cols:
            cols[name] = np.convolve(cols[name], kernel, mode='same')

    range_start = t_start_ms if t_start_ms is not None else t[0]
    range_end   = t_end_ms   if t_end_ms   is not None else t[-1]
    # 裁到 raw 数据实际覆盖的范围内，避免对视频起止范围之外的区间做外推
    range_start = max(range_start, t[0])
    range_end   = min(range_end, t[-1])

    new_t = np.arange(range_start, range_end, step_ms)
    with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for ts_ms in new_t:
            ts_str = datetime.fromtimestamp(ts_ms / 1000.0).strftime(_TS_FMT)[:-3]
            values = [f'{np.interp(ts_ms, t, cols[name]):.6f}'
                      for name in ('acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z')]
            writer.writerow([ts_str] + values)

    print(f'已生成降采样 IMU CSV: {out_path}（{target_hz:.1f}Hz，{len(new_t)} 行，低通窗口={window}）')


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
        # 只有低于目标才是问题（丢帧/采样跟不上）；高于目标是好事，不标红。
        return (80, 80, 255) if actual < target * 0.8 else (255, 200, 100)

    # 行1：摄像头帧率
    put(f'CAM {cam_fps:5.1f} fps  (target {target_fps} fps)', 1, rate_color(cam_fps, target_fps))

    # 行2：IMU 采样率（这里的 target_fps 只是"要跟上摄像头帧率"的参考阈值，
    # 不是设备最终部署要用的 IMU 频率；IMU 实际频率由设备自身配置决定，
    # 高于这个参考值完全没问题，不代表跟最终产品的采样率有关联）
    put(f'IMU {imu_hz:5.1f} Hz   (keep up >= {target_fps} Hz)', 2, rate_color(imu_hz, target_fps))

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


_ffmpeg_encoder_cache = {}


def _ffmpeg_has_encoder(name: str) -> bool:
    if name not in _ffmpeg_encoder_cache:
        try:
            result = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                                     capture_output=True, timeout=10)
            _ffmpeg_encoder_cache[name] = name.encode() in result.stdout
        except (OSError, subprocess.TimeoutExpired):
            _ffmpeg_encoder_cache[name] = False
    return _ffmpeg_encoder_cache[name]


class _FfmpegVfrSink:
    """
    通过管道把每一帧实时喂给 ffmpeg，用 -use_wallclock_as_timestamps 1 让
    ffmpeg 把每帧的 PTS 直接记录为写入那一刻的真实系统时间（可变帧率 VFR）。
    这样视频文件里第 N 帧的时刻天然等于抓帧那一刻的 cam_timestamp，不需要
    事后按固定 fps 猜测/修正时长——mp4 容器一旦写入 PTS，ffmpeg 之后不会再
    按外部指定的 fps 重新计算已有时间戳，所以必须从写入源头就用真实时间。
    """

    def __init__(self, path: str, width: int, height: int, crf: int = 28):
        self.path = path
        # 优先用 H.264（libx264），压缩率比 mpeg4 高很多（同画质下体积通常
        # 只有 mpeg4 的 1/5~1/10），标注用途不需要高码率；没有 libx264 的
        # ffmpeg 构建（少见）才退化用 mpeg4。
        # 注意：ffmpeg 的编码器名是 mpeg4（写出的 fourcc 才是 mp4v），
        # 不存在名为 "mp4v" 的编码器，写错会导致 ffmpeg 直接报错退出、
        # 输出文件为空/损坏，且之前 stderr=DEVNULL 会把报错吞掉不可见。
        if _ffmpeg_has_encoder('libx264'):
            codec_args = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(crf)]
        else:
            codec_args = ['-c:v', 'mpeg4', '-q:v', '3']
        self.proc = subprocess.Popen(
            [
                'ffmpeg', '-y', '-loglevel', 'error',
                '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{width}x{height}',
                '-use_wallclock_as_timestamps', '1',
                '-i', '-',
                *codec_args, '-pix_fmt', 'yuv420p', '-vsync', 'vfr',
                path,
            ],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

    def write(self, frame):
        try:
            self.proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, OSError):
            pass

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            _, stderr = self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, stderr = self.proc.communicate()
        if self.proc.returncode != 0:
            print(f'ffmpeg 写入视频失败 (exit {self.proc.returncode}): {stderr.decode(errors="replace").strip()}')


class _Cv2CfrSink:
    """ffmpeg 不可用时的兜底方案：固定帧率写入，时长精度较低（仅供保留兼容）。"""

    def __init__(self, path: str, fourcc, fps: float, width: int, height: int):
        self.writer = cv2.VideoWriter(path, fourcc, fps, (width, height))

    def write(self, frame):
        self.writer.write(frame)

    def close(self):
        self.writer.release()


# ── 硬件能力探测（--probe） ───────────────────────────────────────────────

def _measure_actual_fps(cap, warmup=5, sample=30) -> float:
    """连续读若干帧，用真实经过时间算出摄像头实际能跑多快（而不是驱动声称的 fps）。"""
    for _ in range(warmup):
        cap.read()
    t0 = time.time()
    got = 0
    for _ in range(sample):
        ret, _ = cap.read()
        if not ret:
            break
        got += 1
    dt = time.time() - t0
    return got / dt if dt > 0 else 0.0


def probe_camera(camera_idx: int):
    """探测摄像头支持的最大分辨率，以及在该分辨率下驱动声称的 fps 与实测能跑的真实 fps。"""
    print(f'── 摄像头 {camera_idx} 能力探测 ──')
    candidate_resolutions = [(3840, 2160), (1920, 1080), (1280, 720), (640, 480)]
    best_res = None
    for w, h in candidate_resolutions:
        cap = cv2.VideoCapture(camera_idx)
        if not cap.isOpened():
            print(f'无法打开摄像头 {camera_idx}')
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        print(f'  请求 {w}x{h}  →  实际 {actual_w}x{actual_h}')
        if best_res is None and actual_w > 0 and actual_h > 0:
            best_res = (actual_w, actual_h)

    if best_res is None:
        print('未能探测到有效分辨率。')
        return None

    print(f'  最大可用分辨率（约）: {best_res[0]}x{best_res[1]}')

    cap = cv2.VideoCapture(camera_idx)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  best_res[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, best_res[1])
    for target in [60, 30, 25, 20, 16, 15, 10]:
        cap.set(cv2.CAP_PROP_FPS, target)
        declared = cap.get(cv2.CAP_PROP_FPS)
        actual = _measure_actual_fps(cap)
        print(f'  请求 {target:3d}fps  →  驱动声称 {declared:5.1f}fps  实测真实 {actual:5.1f}fps')
    cap.release()
    print()
    return best_res


async def probe_imu(args, seconds: float = 5.0):
    """短暂连接 BLE 设备，测量当前设备实际配置的 IMU 输出频率（滑动1秒窗口平均）。"""
    print(f'── IMU 设备能力探测（连接 {seconds:.0f} 秒测量实际频率）──')
    if args.device == 'wit':
        task = asyncio.ensure_future(_run_wit(args))
    else:
        task = asyncio.ensure_future(_run_hicc(args))

    await asyncio.sleep(seconds)
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.TimeoutError:
        pass

    hz = _current_imu_hz()
    print(f'  当前设备实际输出频率: 约 {hz:.1f} Hz')
    print('  （这是设备当前配置的频率，不是"最大支持频率"；WitMotion 设备的具体可选档位'
          '需要在官方上位机软件里查看/修改，一般为 0.2/0.5/1/2/5/10/20/50/100/125/200Hz 等离散值，'
          '不一定支持任意频率如 16Hz。）')
    print()


def run_probe(args):
    probe_camera(args.camera)
    if args.name or args.address:
        stop_event.clear()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(probe_imu(args))
        finally:
            loop.close()
    else:
        print('未指定 --name/--address，跳过 IMU 探测（只测了摄像头）。')


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

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, target_fps)
    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if (actual_w, actual_h) != (args.width, args.height):
        print(f'警告: 请求分辨率 {args.width}x{args.height}，摄像头驱动实际给出 {actual_w}x{actual_h}'
              '（驱动不支持请求的分辨率，自动退化到最接近的档位）。')
    print(f'摄像头分辨率: {actual_w}x{actual_h}  摄像头目标帧率: {target_fps} fps（不影响 IMU 采样率，IMU 由设备自身配置）')

    record_mode = args.duration and args.duration > 0
    loop_mode = args.loop and record_mode
    if args.loop and not record_mode:
        print('警告: --loop 需要配合 --duration（每段录制时长）使用，已忽略 --loop。')

    # 预热：摄像头刚打开时自动曝光/白平衡还没收敛，帧率往往不稳定；IMU 刚连接
    # 也可能有积压/抖动。预热期间持续抓帧丢弃、不写入任何文件，等稳定后再
    # 正式开始计时录制，文件名时间戳也用预热结束后的真实时刻。只在第一段前
    # 预热一次，循环模式下后续片段不用重新预热（摄像头/IMU 已经稳定）。
    if record_mode and args.warmup_sec > 0:
        print(f'预热 {args.warmup_sec:.1f}s（摄像头/IMU 稳定中，不写入数据）...')
        warmup_until = time.time() + args.warmup_sec
        while time.time() < warmup_until and not stop_event.is_set():
            cap.read()
            time.sleep(max(0.0, 1.0 / target_fps))
        print(f'预热结束: CAM {_measure_actual_fps(cap, warmup=0, sample=10):.1f}fps  IMU {_current_imu_hz():.1f}Hz')

    try:
        segment_no = 0
        while True:
            segment_no += 1
            if loop_mode:
                print(f'\n════ 第 {segment_no} 段录制开始 ════')
            should_stop = _run_one_segment(
                args, cap, actual_w, actual_h, target_fps, frame_interval,
                record_mode, save_overlay,
            )
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


def _run_one_segment(args, cap, actual_w, actual_h, target_fps, frame_interval,
                      record_mode, save_overlay) -> bool:
    """
    录制一段（--duration 秒），返回是否应该整体停止（True=用户按 q/ESC 或
    出错/BLE断开，False=正常到时结束，--loop 时会继续录下一段）。
    """
    should_stop = [False]

    ts_tag  = datetime.now().strftime('%Y%m%d_%H%M%S')
    dev_tag = args.device
    mac_tag = ble_mac[0].replace(':', '').lower()
    os.makedirs(args.out_dir, exist_ok=True)
    base    = os.path.join(args.out_dir, f'{dev_tag}_{mac_tag}_{ts_tag}')

    video_writer    = None
    imu_csv_file    = None
    imu_csv_writer  = None
    meta_csv_file   = None
    meta_csv_writer = None
    raw_csv_file    = None

    if record_mode:
        video_path = f'{base}.mp4'
        imu_path   = f'{base}.csv'
        meta_path  = f'{base}_meta.csv'
        raw_path   = f'{base}_raw.csv'
        use_ffmpeg = shutil.which('ffmpeg') is not None
        if use_ffmpeg:
            video_writer = _FfmpegVfrSink(video_path, actual_w, actual_h, crf=args.video_crf)
        else:
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            video_writer = _Cv2CfrSink(video_path, fourcc, float(target_fps), actual_w, actual_h)
            print('警告: 未找到 ffmpeg，退化为固定 fps 写入视频，播放时长与真实录制时长可能有 0.1s 级别误差。'
                  '建议安装 ffmpeg 以获得精确到毫秒的视频/IMU 对齐。')
        imu_csv_file   = open(imu_path,  'w', newline='', encoding='utf-8-sig')
        imu_csv_writer = csv.writer(imu_csv_file)
        imu_csv_writer.writerow(CSV_HEADER)
        meta_csv_file   = open(meta_path, 'w', newline='', encoding='utf-8-sig')
        meta_csv_writer = csv.writer(meta_csv_file)
        meta_csv_writer.writerow(META_HEADER)

        # 原始 IMU 全量流水日志：每条真实到达的样本都记录，不受摄像头帧率影响，
        # 用于录制结束后独立降采样到 --resample-hz（与视频帧对齐的 {base}.csv 互不影响）。
        raw_csv_file = open(raw_path, 'w', newline='', encoding='utf-8-sig')
        raw_writer = csv.writer(raw_csv_file)
        raw_writer.writerow(RAW_CSV_HEADER)
        _set_raw_csv_writer(raw_writer)

        overlay_note = '（含叠加信息）' if save_overlay else '（干净画面）'
        print(f'录制模式: {args.duration}s  视频{overlay_note}→{video_path}')
        print(f'  IMU(Label Studio)→{imu_path}  对齐信息→{meta_path}  原始IMU流水→{raw_path}')
    else:
        print('实时模式（按 Q 或 Ctrl+C 退出）。')

    start_time = time.time()
    next_tick  = start_time
    frame_idx  = 0
    elapsed    = 0.0
    first_cam_ts_ms = None
    last_cam_ts_ms  = None

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

    imu_sync = not args.no_imu_sync

    try:
        while not stop_event.is_set():
            if imu_sync:
                # 事件驱动：等待新 IMU 样本到达再抓帧，让摄像头与 IMU 天然对齐，
                # 而不是各走各的固定定时器（消除同一 IMU 样本被多帧复用的问题）。
                # 超时兜底：等不到新样本时仍按 frame_interval 抓帧并标记 imu_missing。
                _imu_new_event.wait(timeout=frame_interval * 3)
                _imu_new_event.clear()
                now = time.time()
                if now < next_tick:
                    time.sleep(next_tick - now)
                next_tick = time.time() + frame_interval
            else:
                # 旧的固定定时器限速模式
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

            cam_ts    = time.time()
            cam_ts_ms = cam_ts * 1000.0
            frame_idx += 1
            elapsed   = cam_ts - start_time

            # 跳过录制开始前的帧（BLE 启动期间积压）
            if cam_ts < start_time:
                continue

            if first_cam_ts_ms is None:
                first_cam_ts_ms = cam_ts_ms
            last_cam_ts_ms = cam_ts_ms

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
                imu_ts_str = datetime.fromtimestamp(imu_row['pc_ms'] / 1000.0).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
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
        if imu_csv_file:
            imu_csv_file.close()
        if meta_csv_file:
            meta_csv_file.close()
        if raw_csv_file:
            _set_raw_csv_writer(None)  # 先切断 BLE 线程的写入引用，再关闭文件，避免竞态
            raw_csv_file.close()
        print(f'\n共采集 {frame_idx} 帧视频  {elapsed:.1f}s  目标 {target_fps} fps')
        if record_mode:
            print(f'已保存: {base}.mp4')
            print(f'       {base}.csv（Label Studio）')
            print(f'       {base}_meta.csv（全量信息）')
            print(f'       {base}_raw.csv（原始IMU全量流水）')
            resampled_base = f'{base}_resampled{args.resample_hz:g}hz'
            resample_raw_imu(
                f'{base}_raw.csv', f'{resampled_base}.csv', args.resample_hz,
                t_start_ms=first_cam_ts_ms, t_end_ms=last_cam_ts_ms,
            )
            print(f'       {resampled_base}.csv（降采样，起止时间已对齐视频）')
            # Label Studio 靠"文件名（去掉扩展名）一致"配对视频和时间序列 CSV，
            # 复制一份同名视频，方便直接把这一对文件传上去标注。
            try:
                shutil.copyfile(f'{base}.mp4', f'{resampled_base}.mp4')
                print(f'       {resampled_base}.mp4（复制，供 Label Studio 与 resampled CSV 配对）')
            except OSError as e:
                print(f'复制配对视频失败: {e}')
            print()
            print('── 自动对齐校验（按帧对齐版）──')
            try:
                import check_alignment
                check_alignment.run_check(base)
            except Exception as e:
                print(f'对齐校验运行失败: {e}（可手动运行 python check_alignment.py {base}）')
            print()
            print('── 自动对齐校验（降采样版）──')
            try:
                import check_alignment
                check_alignment.run_check(resampled_base, meta_base=base, strict_frame_match=False)
            except Exception as e:
                print(f'对齐校验运行失败: {e}（可手动运行 python check_alignment.py {resampled_base}）')

            if args.resample_only:
                # 对齐校验已经用到 base.mp4/csv/meta/raw，必须等上面全部跑完才能删，
                # 只保留降采样版（resampled_base.mp4/.csv）。
                for p in (f'{base}.mp4', f'{base}.csv', f'{base}_meta.csv', f'{base}_raw.csv'):
                    try:
                        os.remove(p)
                    except OSError as e:
                        print(f'删除 {p} 失败: {e}')
                print(f'\n--resample-only: 已删除原始文件，只保留 {resampled_base}.mp4 / .csv')

    return should_stop[0]


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
    ap.add_argument('--width', type=int, default=1280, help='摄像头请求分辨率宽，默认 1280（720p）')
    ap.add_argument('--height', type=int, default=720, help='摄像头请求分辨率高，默认 720（720p）')
    ap.add_argument('--cam-fps', '--fps', dest='fps', type=int, default=20,
                    choices=range(1, 61), metavar='N',
                    help='摄像头目标帧率（1-60，默认 20）。IMU 采样率由设备自身配置决定，与此参数无关。'
                         '（--fps 为旧名，保留兼容，等价于 --cam-fps）')
    ap.add_argument('--duration', type=float, default=0,
                    help='录制时长（秒），0=实时模式不保存')
    ap.add_argument('--no-save-overlay', action='store_true',
                    help='保存干净视频（不含叠加信息）；默认保存带叠加信息的视频，便于标注时参考')
    ap.add_argument('--no-imu-sync', action='store_true',
                    help='关闭事件驱动同步，改用固定定时器抓帧（不等待新 IMU 样本，可能出现重复复用同一 IMU 样本）')
    ap.add_argument('--probe', action='store_true',
                    help='只探测硬件能力（摄像头最大分辨率/实测fps，IMU当前实际输出频率），不录制，探测完直接退出')
    ap.add_argument('--resample-hz', type=float, default=25.0,
                    help='录制结束后把原始 IMU 全量流水（{base}_raw.csv）降采样到该频率，'
                         '生成 {base}_resampled{HZ}hz.csv（Label Studio 格式），默认 25Hz。'
                         '与摄像头帧率、按帧对齐的 {base}.csv 无关，方便按需调整成 20/16/15Hz 等目标频率。')
    ap.add_argument('--warmup-sec', type=float, default=5.0,
                    help='正式录制前的预热时长（秒），默认 5s。预热期间持续抓帧丢弃、不写入任何'
                         '文件，等摄像头自动曝光/帧率、IMU 连接都稳定后再正式开始计时录制'
                         '（文件名时间戳也是预热结束后的真实时刻）。设为 0 关闭预热。')
    ap.add_argument('--video-crf', type=int, default=28, choices=range(0, 52), metavar='N',
                    help='H.264 压缩质量参数 CRF（0-51），默认 28。数值越小画质越好、文件越大；'
                         '数值越大文件越小、画质越差。标注用途 23~30 都够用，18 以下接近无损但'
                         '体积明显变大。仅在系统 ffmpeg 支持 libx264 时生效，否则退化用 mpeg4。')
    ap.add_argument('--loop', action='store_true',
                    help='循环录制模式：每段 --duration 秒，录完自动开始下一段（各段独立生成一套'
                         '文件），直到按 Q/ESC 或 Ctrl+C 才停止。摄像头/BLE 连接全程保持不重连，'
                         '只在片段边界切换文件。需要配合 --duration 使用。')
    ap.add_argument('--out-dir', default='data',
                    help='录制输出目录，默认 data/（目录不存在会自动创建）')
    ap.add_argument('--resample-only', action='store_true',
                    help='只保留降采样版文件（{resampled_base}.mp4 / .csv），'
                         '删除按帧对齐版 {base}.mp4/.csv/_meta.csv/_raw.csv。'
                         '对齐校验仍会先跑完再删除，不影响校验结果。')
    args = ap.parse_args()

    if args.probe:
        run_probe(args)
        return

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
