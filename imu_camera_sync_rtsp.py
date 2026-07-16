# -*- coding: utf-8 -*-
"""
IMU + RTSP摄像头（go2rtc/micam_dev）同步采集脚本
=================================================

跟 imu_camera_sync.py 功能完全一样（IMU+摄像头同步录制、事件驱动对齐、
VFR视频写入、降采样、断连缺口留空等），唯一区别是摄像头来源不是本地
USB摄像头（cv2.VideoCapture(索引)），而是 micam_dev/go2rtc 提供的
RTSP 流（cv2.VideoCapture("rtsp://host:port/stream")），用来接小米摄像头
转出来的流。IMU 采集、BLE 重连、CSV/降采样/对齐校验等逻辑全部直接复用
imu_camera_sync.py，不重复实现。

关于延迟:
    RTSP over TCP + FFmpeg 默认会做内部缓冲，实测常见 1~2秒 延迟（分辨率越
    高越明显）。本脚本参考 micam_dev/scripts/capture_frame.py 的做法:
        1. 用 OPENCV_FFMPEG_CAPTURE_OPTIONS 环境变量告诉 FFmpeg 后端关闭
           内部缓冲（fflags=nobuffer, flags=low_delay），这是延迟的大头。
        2. 用后台线程持续 cap.read()，主循环永远拿"最新一帧"而不是排队
           按到达顺序处理堆积的旧帧（LatestFrameReader，见 capture_frame.py
           里的同名类，这里做了同样实现）。
    即便如此，RTSP 链路（摄像头编码 → 网络 → go2rtc转发 → FFmpeg解码）本身
    还是会比本地USB摄像头多几十到几百毫秒延迟，这是链路结构决定的，不是
    脚本能完全消除的；--no-imu-sync 关闭事件驱动、improve不了这个延迟，
    真正需要更低延迟建议在go2rtc端确认用的是较低分辨率/码率的subtype。

    这个延迟对标注是有影响的：同步逻辑用"收到这一帧的PC时刻"去找最近的
    IMU样本，本地摄像头延迟接近0没问题，但RTSP画面内容实际上是"之前"发生
    的（IMU是BLE直连、近似实时），如果不处理，配对的IMU数值会比画面里的
    动作提前一个"RTSP延迟"的量，是系统性偏差。用 --auto-calibrate-latency
    可以自动测出这个延迟并补偿，预热结束后分三步、全程有明确提示：
        1. 静置（默认5秒）：保持设备/画面不动，先稳定下来。
        2. 倒计时（默认3秒，"3、2、1"）：提前预告，准备好动作。
        3. 动手窗口（默认3秒）："现在！"之后这几秒内随便什么时候、多大力
           晃/敲一下都行，不用掐时机——脚本只在这个窗口内找"最大的尖峰"。
    脚本会在视频画面（连续帧灰度差）和IMU加速度（模长偏离1g）两条独立时间
    线上分别检测这次动作的尖峰，两个尖峰的时间差就是延迟，之后查找IMU样本
    时自动把视频帧时间戳往回拨这个量。测出的结果会自动缓存（按host:port/
    stream区分），下次录同一个流不用再重新校准。也可以用 --video-latency-ms
    手动指定一个已知的延迟值，跳过自动校准。

用法:
    # 先探测 RTSP 流实际能拿到的分辨率/帧率
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --probe --device wit --name WTSDCL

    # 录制60秒（WitMotion）
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL --duration 60

    # 指定降采样到16Hz，循环录制，只保留降采样版
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL \\
        --duration 60 --resample-hz 16 --loop --resample-only --out-dir data/rtsp

    # 下游画面统一缩放到指定尺寸（RTSP流本身只有几档固定质量，不支持任意分辨率，
    # 想要精确到某个尺寸用这个参数，效果类似 capture_frame.py 的 --resize）
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL --resize 1280x720

    # 自动校准RTSP视频延迟后再录制（预热结束会提示晃动设备，几秒内完成即可）
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL \\
        --duration 60 --auto-calibrate-latency

    # 已知延迟大概多少，直接手动指定，跳过自动校准环节
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL \\
        --duration 60 --video-latency-ms 180
"""

import argparse
import os

# 必须在 import cv2 之前设置：告诉 FFmpeg 后端关闭内部缓冲，这是 RTSP
# 1~2秒延迟的大头（数值越高分辨率越明显）。抄自 micam_dev/scripts/capture_frame.py。
os.environ.setdefault(
    'OPENCV_FFMPEG_CAPTURE_OPTIONS',
    'rtsp_transport;tcp|fflags;nobuffer|flags;low_delay',
)

import csv
import json
import shutil
import statistics
import sys
import threading
import time
from datetime import datetime

try:
    import cv2
except ImportError:
    print('缺少 opencv-python，请先安装: pip install opencv-python')
    sys.exit(1)

# 直接复用 imu_camera_sync.py 里所有跟摄像头来源无关的逻辑（BLE采集、
# CSV/降采样、对齐校验、画面叠加、VFR视频写入等），避免另外维护一份。
import imu_camera_sync as ics


class LatestFrameReader:
    """
    后台线程连续 cap.read()，主线程 read() 永远拿最新一帧，不会因为
    主循环处理慢/等IMU事件而攒下一堆旧帧、导致画面和时间戳越来越滞后。
    跟 micam_dev/scripts/capture_frame.py 里的同名类实现一致。
    """

    def __init__(self, cap: 'cv2.VideoCapture') -> None:
        self._cap = cap
        self._frame = None
        self._ok = False
        self._lock = threading.Lock()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stopped:
            ok, frame = self._cap.read()
            with self._lock:
                self._ok, self._frame = ok, frame

    def read(self):
        with self._lock:
            return self._ok, self._frame

    def isOpened(self):
        return self._cap.isOpened()

    def release(self) -> None:
        self._stopped = True
        self._thread.join(timeout=1)
        self._cap.release()


def build_rtsp_url(host: str, port: int, stream: str) -> str:
    return f'rtsp://{host}:{port}/{stream}'


# ── 延迟校准结果缓存 ─────────────────────────────────────────────────────────
# RTSP 链路延迟基本是 go2rtc/摄像头/网络这条链路本身的固有属性，跟每次录制无关，
# 校准一次之后没必要每次开录都重新让人晃一下设备——按 host:port/stream 存到本地
# 缓存文件里，下次同一个流直接复用，除非显式加 --auto-calibrate-latency 要求重测。

def _cache_key(host: str, port: int, stream: str) -> str:
    return f'{host}:{port}/{stream}'


def load_cached_latency(cache_file: str, host: str, port: int, stream: str):
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    entry = data.get(_cache_key(host, port, stream))
    if not entry:
        return None
    return entry.get('latency_ms')


def save_cached_latency(cache_file: str, host: str, port: int, stream: str, latency_ms: float):
    data = {}
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data[_cache_key(host, port, stream)] = {
        'latency_ms': latency_ms,
        'calibrated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    try:
        os.makedirs(os.path.dirname(cache_file) or '.', exist_ok=True)
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f'  已把这次测得的延迟（{latency_ms:+.0f}ms）保存到 {cache_file}，'
              f'以后录这个流（{_cache_key(host, port, stream)}）会自动复用，不用再手动加 --auto-calibrate-latency。')
    except OSError as e:
        print(f'  警告: 延迟缓存写入失败: {e}（这次测出的值仍会用于本次录制，只是不会持久化）')


# 视频内容判定为"发生在多久之前"的固定偏移（RTSP链路自身的编码+网络+解码延迟）。
# 通过 auto_calibrate_latency() 自动测出来，用于把视频帧的时间戳往回拨这个量，
# 再去 IMU 缓冲区里找对应的真实样本，而不是直接拿"收到这一帧的PC时刻"去找。
_MOTION_SPIKE_THRESHOLD = 15.0   # 灰度帧间均值绝对差，明显晃动/敲击通常远超这个值（静止时通常<3）
_ACCEL_SPIKE_THRESHOLD_G = 0.3   # 加速度模长偏离1g的量，明显晃动/敲击通常远超这个值（静止时通常<0.05g）


def _drain_video_imu(cap, resize_to, duration: float, collect: bool):
    """辅助函数：跑 duration 秒，读摄像头帧+收集新到的IMU样本。
    collect=False 时只是丢弃（用于"静置"阶段稳定画面/避免尖峰检测用到旧帧的残留差值），
    collect=True 时返回 (video_samples, imu_samples)，时间戳都是各自到达时的PC毫秒。"""
    prev_gray = None
    video_samples = []
    imu_samples = []
    seen_imu_seq = set()
    t_start_ms = time.time() * 1000.0
    deadline = time.time() + duration
    while time.time() < deadline:
        ok, frame = cap.read()
        if ok and frame is not None:
            if resize_to:
                frame = cv2.resize(frame, resize_to)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            ts_ms = time.time() * 1000.0
            if collect and prev_gray is not None and prev_gray.shape == gray.shape:
                score = float(cv2.absdiff(gray, prev_gray).mean())
                video_samples.append((ts_ms, score))
            prev_gray = gray

        if collect:
            with ics._imu_lock:
                recent = [r for r in ics._imu_buffer if r['pc_ms'] >= t_start_ms]
            for r in recent:
                if r['seq'] in seen_imu_seq:
                    continue
                seen_imu_seq.add(r['seq'])
                mag = (r['acc_x'] ** 2 + r['acc_y'] ** 2 + r['acc_z'] ** 2) ** 0.5
                imu_samples.append((r['pc_ms'], abs(mag - 1.0)))

        time.sleep(0.005)
    return video_samples, imu_samples


def auto_calibrate_latency(cap, resize_to, still_sec: float = 5.0,
                            countdown_sec: int = 3, action_sec: float = 3.0):
    """
    自动测量 RTSP 链路的视频延迟（画面内容比真实发生时刻晚多少毫秒）。

    分三个阶段，全程有明确提示，跟着提示做就行:
        1. 静置 still_sec 秒：保持摄像头画面里的设备/项圈不动，脚本借这段
           时间让画面和数据流都稳定下来。
        2. 倒计时 countdown_sec 秒（3、2、1）：提前预告，准备好动作。
        3. "动手" 窗口 action_sec 秒：这段时间内随便多用力晃/敲一下都行——
           不需要掐时机、不需要精确对准某一刻，只要在这个窗口内做出一个
           明显的大动作，脚本只会在这个窗口的数据里找"最大的那个尖峰"。

    原理：这次动作会同时在两条独立的时间线上留下一个"尖峰"：
        1. 视频画面：连续帧灰度图的逐帧差异（mean abs diff）会突然变大。
        2. IMU 加速度：|acc| 偏离静止时的 1g 会突然变大。
    IMU 是 BLE 直连，延迟很小（近似"实时"）；摄像头是 RTSP，链路上有真实的
    编码+网络+解码延迟。所以视频里检测到尖峰的时刻，会比 IMU 里检测到尖峰的
    时刻晚一个"RTSP延迟"，两者之差就是需要补偿的量。

    返回: 测得的延迟（毫秒，视频比IMU晚多少），测不出明显尖峰则返回 None
    （调用方应提示用户加大动作幅度重试，或改用 --video-latency-ms 手动指定）。
    """
    print(f'── 延迟自动校准 ──')
    print(f'  第1步/静置：接下来 {still_sec:.0f} 秒请保持项圈/IMU设备和摄像头画面都不动...')
    _drain_video_imu(cap, resize_to, still_sec, collect=False)

    print('  第2步/准备：倒计时结束后有一个动作窗口，到时候用力晃一下或敲一下项圈/设备就行，')
    print('  不用掐准时机、不用小心翼翼——窗口内随便什么时候动、动作多大力都可以。')
    for n in range(countdown_sec, 0, -1):
        print(f'    {n}...')
        time.sleep(1.0)

    print(f'  第3步/动手：现在！接下来 {action_sec:.0f} 秒内用力晃一下/敲一下项圈或设备 👋')
    video_samples, imu_samples = _drain_video_imu(cap, resize_to, action_sec, collect=True)
    print('  动作窗口结束，正在计算...')

    if len(video_samples) < 3 or len(imu_samples) < 3:
        print('  校准失败：这个窗口内采集到的视频/IMU样本太少（摄像头或IMU可能还没稳定），跳过自动补偿。')
        return None

    video_peak_ts, video_peak_val = max(video_samples, key=lambda x: x[1])
    imu_peak_ts, imu_peak_val = max(imu_samples, key=lambda x: x[1])

    if video_peak_val < _MOTION_SPIKE_THRESHOLD or imu_peak_val < _ACCEL_SPIKE_THRESHOLD_G:
        print(f'  校准失败：这个窗口内没检测到明显的晃动尖峰（视频峰值={video_peak_val:.1f}，'
              f'IMU峰值={imu_peak_val:.2f}g），可能动作幅度不够大，或者没在动作窗口内做动作。'
              '可以重新跑一次 --auto-calibrate-latency，或者用 --video-latency-ms 手动指定。')
        return None

    latency_ms = video_peak_ts - imu_peak_ts
    print(f'  视频尖峰: {video_peak_val:.1f}（{time.strftime("%H:%M:%S", time.localtime(video_peak_ts/1000.0))}）  '
          f'IMU尖峰: {imu_peak_val:.2f}g（{time.strftime("%H:%M:%S", time.localtime(imu_peak_ts/1000.0))}）')
    print(f'  测得视频延迟: {latency_ms:+.0f} ms（正值=视频画面比IMU晚这么多，后续会自动补偿）')
    if not (-500.0 <= latency_ms <= 5000.0):
        print(f'  警告: 测得的延迟数值看起来不太合理（{latency_ms:.0f}ms），可能是碰巧同时有别的干扰动作，'
              '建议重新校准一次或改用 --video-latency-ms 手动指定，本次暂不自动补偿。')
        return None
    return latency_ms


def probe_rtsp(url: str, resize_to=None, sample_seconds: float = 5.0):
    """探测 RTSP 流实际分辨率 + 实测帧率（不像本地摄像头那样能请求任意分辨率/fps，
    go2rtc 提供的是固定的几档质量，这里只做只读探测，不下发任何 fps/分辨率请求）。"""
    print(f'── RTSP 流探测: {url} ──')
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print(f'无法打开 RTSP 流: {url}')
        return None
    reader = LatestFrameReader(cap)
    try:
        t0 = time.time()
        n = 0
        w = h = 0
        while time.time() - t0 < sample_seconds:
            ok, frame = reader.read()
            if ok and frame is not None:
                if resize_to:
                    frame = cv2.resize(frame, resize_to)
                h, w = frame.shape[:2]
                n += 1
            time.sleep(0.01)
        dt = time.time() - t0
        fps = n / dt if dt > 0 else 0.0
        print(f'  实测: {w}x{h} @ {fps:.1f} fps（{sample_seconds:.0f}秒采样，低延迟模式）')
        return (w, h, fps)
    finally:
        reader.release()


def run_probe(args):
    resize_to = None
    if args.resize:
        w, h = args.resize.lower().split('x')
        resize_to = (int(w), int(h))
    probe_rtsp(build_rtsp_url(args.host, args.port, args.stream), resize_to=resize_to)
    print()
    if args.name or args.address:
        ics.stop_event.clear()
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(ics.probe_imu(args))
        finally:
            loop.close()
    else:
        print('未指定 --name/--address，跳过 IMU 探测（只测了 RTSP 流）。')


def run_camera(args):
    """跟 imu_camera_sync.py 的 run_camera + _run_one_segment 逻辑基本一致，
    区别只在于摄像头来源换成 RTSP + LatestFrameReader 低延迟读取。"""
    target_fps = args.fps
    frame_interval = 1.0 / target_fps
    save_overlay = not args.no_save_overlay

    resize_to = None
    if args.resize:
        w, h = args.resize.lower().split('x')
        resize_to = (int(w), int(h))

    url = build_rtsp_url(args.host, args.port, args.stream)
    cap_raw = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap_raw.isOpened():
        print(f'无法打开 RTSP 流: {url}，请确认 go2rtc 服务/流名称/网络可达。')
        ics.stop_event.set()
        return
    cap = LatestFrameReader(cap_raw)

    # 摄像头分辨率由 RTSP 流本身决定（go2rtc 固定质量档位），读第一帧确认实际尺寸
    actual_w = actual_h = None
    wait_until = time.time() + 5.0
    while time.time() < wait_until:
        ok, frame = cap.read()
        if ok and frame is not None:
            if resize_to:
                frame = cv2.resize(frame, resize_to)
            actual_h, actual_w = frame.shape[:2]
            break
        time.sleep(0.05)
    if actual_w is None:
        print('等待 RTSP 首帧超时，请检查流是否正常。')
        cap.release()
        ics.stop_event.set()
        return
    print(f'RTSP 流: {url}  实际画面: {actual_w}x{actual_h}  目标帧率: {target_fps} fps'
          '（IMU 采样率由设备自身配置决定，与此参数无关；RTSP实际到达速率取决于摄像头/网络，'
          '不一定能精确达到目标帧率）')

    record_mode = args.duration and args.duration > 0
    loop_mode = args.loop and record_mode
    if args.loop and not record_mode:
        print('警告: --loop 需要配合 --duration（每段录制时长）使用，已忽略 --loop。')

    if record_mode and args.warmup_sec > 0:
        print(f'预热 {args.warmup_sec:.1f}s（RTSP流/IMU 稳定中，不写入数据）...')
        warmup_until = time.time() + args.warmup_sec
        while time.time() < warmup_until and not ics.stop_event.is_set():
            cap.read()
            time.sleep(max(0.0, 1.0 / target_fps))
        print(f'预热结束，IMU {ics._current_imu_hz():.1f}Hz')

    # 视频延迟补偿量（毫秒）：--video-latency-ms 手动指定的值作为默认/兜底，
    # --auto-calibrate-latency 测出来的结果会覆盖它（测不出来则保留手动值，
    # 手动值默认 0，即不补偿）。
    # 延迟补偿优先级: 显式 --video-latency-ms > 本次 --auto-calibrate-latency 现测 >
    # 缓存里之前测过的值（同一个 host:port/stream 复用）> 0（不补偿）。
    latency_ms = args.video_latency_ms
    if latency_ms is not None:
        print(f'使用手动指定的视频延迟补偿: {latency_ms:+.0f}ms（--video-latency-ms）')
    elif args.auto_calibrate_latency and record_mode and not ics.stop_event.is_set():
        measured = auto_calibrate_latency(
            cap, resize_to,
            still_sec=args.calibrate_still_sec,
            countdown_sec=args.calibrate_countdown_sec,
            action_sec=args.calibrate_action_sec,
        )
        if measured is not None:
            latency_ms = measured
            if not args.no_latency_cache:
                save_cached_latency(args.latency_cache_file, args.host, args.port, args.stream, latency_ms)
        else:
            cached = None if args.no_latency_cache else load_cached_latency(
                args.latency_cache_file, args.host, args.port, args.stream)
            latency_ms = cached if cached is not None else 0.0
            print(f'  自动校准未成功，本次录制使用: {latency_ms:+.0f}ms'
                  + ('（沿用之前缓存的校准结果）' if cached is not None else '（无缓存值，不补偿）'))
        print()
    else:
        cached = None if args.no_latency_cache else load_cached_latency(
            args.latency_cache_file, args.host, args.port, args.stream)
        if cached is not None:
            latency_ms = cached
            print(f'使用之前校准并缓存的视频延迟补偿: {latency_ms:+.0f}ms'
                  f'（{_cache_key(args.host, args.port, args.stream)}，如需重新测量加 --auto-calibrate-latency）')
        else:
            latency_ms = 0.0
            if record_mode:
                print('未指定/未缓存视频延迟补偿，本次不做补偿（首次建议加 --auto-calibrate-latency 校准一次）。')

    try:
        segment_no = 0
        while True:
            segment_no += 1
            if loop_mode:
                print(f'\n════ 第 {segment_no} 段录制开始 ════')
            should_stop = _run_one_segment(
                args, cap, actual_w, actual_h, target_fps, frame_interval,
                record_mode, save_overlay, resize_to, latency_ms,
            )
            if not loop_mode or should_stop or ics.stop_event.is_set():
                break
    except KeyboardInterrupt:
        pass
    finally:
        ics.stop_event.set()
        cap.release()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass


def _run_one_segment(args, cap, actual_w, actual_h, target_fps, frame_interval,
                      record_mode, save_overlay, resize_to, latency_ms: float = 0.0) -> bool:
    should_stop = [False]

    ts_tag = datetime.now().strftime('%Y%m%d_%H%M%S')
    dev_tag = args.device
    mac_tag = ics.ble_mac[0].replace(':', '').lower()
    os.makedirs(args.out_dir, exist_ok=True)
    # 注意：文件名不能出现 "_camN"（下划线+cam+数字）这种子串——label_infra 的
    # 上传/导入脚本（upload_server.py）用正则 `_cam\d+` 判断"是不是多摄像头会话"，
    # 命中就会把 task data 的 key 从单视角的 "video" 改成多视角的 "video1"/"video2"...
    # 如果 go2rtc 的流名称正好叫 cam0/cam1 这种，之前拼出来的文件名会变成
    # "..._rtsp_cam0_..."，被误判成多摄像头，导致导入单视角项目时报
    # "'video' key is expected in task data" 400错误。这里用连字符而不是
    # 下划线拼流名称，规避这个正则，不管 go2rtc 流叫什么名字都不会误触发。
    base = os.path.join(args.out_dir, f'{dev_tag}_{mac_tag}_rtspstream-{args.stream}_{ts_tag}')

    if latency_ms:
        print(f'本段录制使用视频延迟补偿: {latency_ms:+.0f}ms（查找IMU样本时，视频帧时间戳会先减去这个量）')

    video_writer = None
    imu_csv_file = None
    imu_csv_writer = None
    meta_csv_file = None
    meta_csv_writer = None
    raw_csv_file = None

    if record_mode:
        video_path = f'{base}.mp4'
        imu_path = f'{base}.csv'
        meta_path = f'{base}_meta.csv'
        raw_path = f'{base}_raw.csv'
        use_ffmpeg = shutil.which('ffmpeg') is not None
        if use_ffmpeg:
            video_writer = ics._FfmpegVfrSink(video_path, actual_w, actual_h, crf=args.video_crf)
        else:
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            video_writer = ics._Cv2CfrSink(video_path, fourcc, float(target_fps), actual_w, actual_h)
            print('警告: 未找到 ffmpeg，退化为固定 fps 写入视频，播放时长与真实录制时长可能有误差。')
        imu_csv_file = open(imu_path, 'w', newline='', encoding='utf-8-sig')
        imu_csv_writer = csv.writer(imu_csv_file)
        imu_csv_writer.writerow(ics.CSV_HEADER)
        meta_csv_file = open(meta_path, 'w', newline='', encoding='utf-8-sig')
        meta_csv_writer = csv.writer(meta_csv_file)
        meta_csv_writer.writerow(ics.META_HEADER)

        raw_csv_file = open(raw_path, 'w', newline='', encoding='utf-8-sig')
        raw_writer = csv.writer(raw_csv_file)
        raw_writer.writerow(ics.RAW_CSV_HEADER)
        ics._set_raw_csv_writer(raw_writer)

        overlay_note = '（含叠加信息）' if save_overlay else '（干净画面）'
        print(f'录制模式: {args.duration}s  视频{overlay_note}→{video_path}')
        print(f'  IMU(Label Studio)→{imu_path}  对齐信息→{meta_path}  原始IMU流水→{raw_path}')
    else:
        print('实时模式（按 Q 或 Ctrl+C 退出）。')

    start_time = time.time()
    next_tick = start_time
    frame_idx = 0
    elapsed = 0.0
    first_cam_ts_ms = None
    last_cam_ts_ms = None

    cam_ts_window: list = []

    def cam_fps_tick(now: float) -> float:
        cutoff = now - 1.0
        while cam_ts_window and cam_ts_window[0] < cutoff:
            cam_ts_window.pop(0)
        cam_ts_window.append(now)
        return float(len(cam_ts_window))

    max_lag_ms = 3 * (1000.0 / target_fps)
    imu_sync = not args.no_imu_sync

    try:
        while not ics.stop_event.is_set():
            if imu_sync:
                ics._imu_new_event.wait(timeout=frame_interval * 3)
                ics._imu_new_event.clear()
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

            ret, frame = cap.read()
            if not ret or frame is None:
                # RTSP 流偶尔瞬时抖动拿不到帧很常见，不像本地摄像头那样直接判定
                # 硬件故障退出；短暂重试，长时间拿不到再放弃。
                time.sleep(0.01)
                continue

            if resize_to:
                frame = cv2.resize(frame, resize_to)

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
            imu_hz = ics._current_imu_hz()

            # 视频帧内容比它自己的到达时间戳"更早"（RTSP链路延迟），查IMU时要
            # 用这一帧画面里动作真实发生的时刻，而不是收到这一帧的PC时刻。
            imu_row, lag_ms, missing = ics._find_nearest_imu(cam_ts_ms - latency_ms, max_lag_ms)

            cam_ts_str = datetime.fromtimestamp(cam_ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            if missing or imu_row is None:
                acc = ['', '', '']
                gyro = ['', '', '']
                lag_str = f'{lag_ms:.1f}' if lag_ms != float('inf') else ''
                imu_ts_str = ''
                missing_flag = 1
            else:
                acc = [f"{imu_row['acc_x']:.6f}", f"{imu_row['acc_y']:.6f}", f"{imu_row['acc_z']:.6f}"]
                gyro = [f"{imu_row['gyro_x']:.6f}", f"{imu_row['gyro_y']:.6f}", f"{imu_row['gyro_z']:.6f}"]
                lag_str = f'{lag_ms:.1f}'
                imu_ts_str = datetime.fromtimestamp(imu_row['pc_ms'] / 1000.0).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                missing_flag = 0

            if imu_csv_writer:
                imu_csv_writer.writerow([cam_ts_str] + acc + gyro)

            if meta_csv_writer:
                meta_csv_writer.writerow([
                    frame_idx, cam_ts_str, imu_ts_str,
                    lag_str, missing_flag,
                    *acc, *gyro,
                    f'{cam_fps:.1f}', f'{imu_hz:.1f}',
                ])

            display = ics.draw_imu_overlay(
                frame.copy(), imu_row, lag_ms, missing, frame_idx, elapsed,
                recording=record_mode, cam_fps=cam_fps, imu_hz=imu_hz, target_fps=target_fps,
            )

            if video_writer:
                video_writer.write(display if save_overlay else frame)

            try:
                cv2.imshow('IMU + RTSP Camera Sync', display)
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
        if ics.stop_event.is_set():
            should_stop[0] = True
        if video_writer:
            video_writer.close()
        if imu_csv_file:
            imu_csv_file.close()
        if meta_csv_file:
            meta_csv_file.close()
        if raw_csv_file:
            ics._set_raw_csv_writer(None)
            raw_csv_file.close()
        print(f'\n共采集 {frame_idx} 帧视频  {elapsed:.1f}s  目标 {target_fps} fps')
        if record_mode:
            print(f'已保存: {base}.mp4')
            print(f'       {base}.csv（Label Studio）')
            print(f'       {base}_meta.csv（全量信息）')
            print(f'       {base}_raw.csv（原始IMU全量流水）')
            resampled_base = f'{base}_resampled{args.resample_hz:g}hz'
            ics.resample_raw_imu(
                f'{base}_raw.csv', f'{resampled_base}.csv', args.resample_hz,
                t_start_ms=first_cam_ts_ms, t_end_ms=last_cam_ts_ms,
            )
            print(f'       {resampled_base}.csv（降采样，起止时间已对齐视频）')
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
                for p in (f'{base}.mp4', f'{base}.csv', f'{base}_meta.csv', f'{base}_raw.csv'):
                    try:
                        os.remove(p)
                    except OSError as e:
                        print(f'删除 {p} 失败: {e}')
                print(f'\n--resample-only: 已删除原始文件，只保留 {resampled_base}.mp4 / .csv')

    return should_stop[0]


def main():
    ap = argparse.ArgumentParser(description='IMU + RTSP摄像头（go2rtc/micam_dev）同步采集')
    ap.add_argument('--device', choices=['wit', 'hicc'], required=True,
                    help='IMU 设备类型: wit=WitMotion  hicc=HICC_PetCollar')
    ap.add_argument('--name', help='BLE 设备名称关键字（WitMotion 用）')
    ap.add_argument('--address', help='BLE MAC 地址（HICC 必须，WitMotion 可选）')
    ap.add_argument('--notify-uuid', dest='notify_uuid', default=None,
                    help='手动指定 WitMotion Notify UUID')

    ap.add_argument('--host', required=True, help='go2rtc 服务地址，如 192.168.2.140')
    ap.add_argument('--port', type=int, default=8554, help='go2rtc RTSP 端口，默认 8554')
    ap.add_argument('--stream', required=True, help='go2rtc 流名称，如 cam0')
    ap.add_argument('--resize', metavar='WxH', default=None,
                    help='把每帧缩放到指定尺寸，如 1280x720（RTSP流本身只有几档固定质量，'
                         '不支持任意分辨率，想要精确尺寸用这个）')

    ap.add_argument('--cam-fps', '--fps', dest='fps', type=int, default=20,
                    choices=range(1, 61), metavar='N',
                    help='目标帧率（1-60，默认 20），用于事件驱动同步节流和叠加显示，'
                         'RTSP实际到达速率取决于摄像头/网络，不一定能精确达到')
    ap.add_argument('--duration', type=float, default=0,
                    help='录制时长（秒），0=实时模式不保存')
    ap.add_argument('--no-save-overlay', action='store_true',
                    help='保存干净视频（不含叠加信息）；默认保存带叠加信息的视频')
    ap.add_argument('--no-imu-sync', action='store_true',
                    help='关闭事件驱动同步，改用固定定时器抓帧')
    ap.add_argument('--probe', action='store_true',
                    help='只探测 RTSP 流实际分辨率/帧率 + IMU 当前实际输出频率，不录制')
    ap.add_argument('--resample-hz', type=float, default=25.0,
                    help='录制结束后降采样到该频率，默认 25Hz')
    ap.add_argument('--warmup-sec', type=float, default=5.0,
                    help='正式录制前的预热时长（秒），默认 5s，设 0 关闭')
    ap.add_argument('--video-crf', type=int, default=28, choices=range(0, 52), metavar='N',
                    help='H.264 压缩质量参数 CRF（0-51），默认 28')
    ap.add_argument('--loop', action='store_true',
                    help='循环录制模式：每段 --duration 秒，录完自动开始下一段，需配合 --duration')
    ap.add_argument('--out-dir', default='data',
                    help='录制输出目录，默认 data/')
    ap.add_argument('--resample-only', action='store_true',
                    help='只保留降采样版文件，删除按帧对齐版')
    ap.add_argument('--auto-calibrate-latency', action='store_true',
                    help='预热结束后自动测量RTSP视频延迟并补偿：会提示你对着摄像头把设备猛地晃一下/'
                         '敲一下，脚本据此自动检测视频画面和IMU加速度里同时出现的"尖峰"，'
                         '两者时间差就是需要补偿的视频延迟，全程不需要人工掐表。'
                         '测出的结果会自动存到 --latency-cache-file，同一个 host:port/stream '
                         '以后录制会自动复用，不用每次都重新晃动设备；只有想重新测量时才需要再加这个参数。')
    ap.add_argument('--video-latency-ms', type=float, default=None,
                    help='手动指定视频延迟补偿量（毫秒，视频比IMU晚多少），查找IMU样本时会用'
                         '"视频帧时间戳 - 这个值"去匹配，优先级最高（会跳过缓存和自动校准）。'
                         '不指定的话，按顺序：本次 --auto-calibrate-latency 现测的结果 > 之前'
                         '缓存过的同一个流的校准结果 > 0（不补偿）。')
    ap.add_argument('--calibrate-still-sec', type=float, default=5.0,
                    help='--auto-calibrate-latency 第1步"静置"时长（秒），默认5秒，'
                         '这段时间保持设备/画面不动即可')
    ap.add_argument('--calibrate-countdown-sec', type=int, default=3,
                    help='--auto-calibrate-latency 第2步倒计时秒数，默认3秒（3、2、1）')
    ap.add_argument('--calibrate-action-sec', type=float, default=3.0,
                    help='--auto-calibrate-latency 第3步"动手"窗口时长（秒），默认3秒，'
                         '倒计时结束后这段时间内随时用力晃/敲一下都可以，不用掐时机')
    ap.add_argument('--latency-cache-file', default=os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), '.rtsp_latency_cache.json'),
                    help='延迟校准结果的缓存文件路径，按 host:port/stream 分别存一份，'
                         '默认存在脚本同目录下的 .rtsp_latency_cache.json')
    ap.add_argument('--no-latency-cache', action='store_true',
                    help='不读取也不写入延迟缓存文件（每次都要么不补偿、要么用 --video-latency-ms/'
                         '--auto-calibrate-latency 现场指定/现测）')
    args = ap.parse_args()

    if args.probe:
        run_probe(args)
        return

    if args.device == 'wit' and not args.name and not args.address:
        ap.error('WitMotion 设备请指定 --name 或 --address')

    t = threading.Thread(target=ics.ble_thread_main, args=(args,), daemon=True)
    t.start()

    print('等待 BLE 连接中...')
    time.sleep(2.0)

    run_camera(args)

    ics.stop_event.set()
    t.join(timeout=3.0)


if __name__ == '__main__':
    main()
