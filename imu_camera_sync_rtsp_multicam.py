# -*- coding: utf-8 -*-
"""
多路 RTSP 摄像头（go2rtc/micam_dev） + 多个 IMU 设备 同步采集脚本
====================================================================

在 imu_camera_sync_multicam.py（多本地摄像头+多IMU）的基础上，把摄像头来源
换成 micam_dev/go2rtc 提供的多路 RTSP 流（每路流用 --stream 重复指定，共用
同一个 --host/--port）。IMU 采集、CSV/降采样/断连缺口留空等逻辑直接复用
imu_camera_sync.py / imu_camera_sync_multi.py，RTSP 读取（低延迟选项 +
LatestFrameReader）复用 imu_camera_sync_rtsp.py，都不重复实现。

关于延迟补偿:
    每一路 RTSP 流的链路延迟可能不一样（不同摄像头/不同网络路径），所以
    延迟补偿是按每一路流单独配置的，取值方式跟 imu_camera_sync_rtsp.py
    完全一样：从 JSON 配置文件里按 host:port/stream 读取，比如:
        {
          "192.168.2.140:8554/cam0": {"latency_ms": 700},
          "192.168.2.140:8554/cam1": {"latency_ms": 900}
        }
    每次运行自动读取，不用在命令行传参数；也可以用 --video-latency-ms
    给所有流指定同一个值（命令行优先级更高，会覆盖配置文件里每一路的值）。

    补偿只应用在每路摄像头 x 每个设备各自的降采样配对文件（{base}_camN_imuM_
    resampled{HZ}hz.mp4/.csv，这是实际拖进 Label Studio 标注用的文件）上，
    因为不同摄像头延迟不同，没法在"所有摄像头共用一份"的逐帧组合CSV
    （{base}.csv）里同时精确补偿多路不同的延迟，所以那份文件里的对齐仍然是
    未做延迟补偿的原始"抓帧时刻"匹配，仅供调试/参考，不建议直接拿它标注；
    真正标注请用每路摄像头自己的降采样配对文件。

用法:
    # 2路RTSP流（cam0/cam1） + 1个IMU设备，配置文件里已经配好各自的延迟值
    python imu_camera_sync_rtsp_multicam.py --host 192.168.2.140 --stream cam0 --stream cam1 \\
        --imu wit=WT901BLE68 --duration 60

    # 降采样到16Hz，循环录制，只保留降采样版
    python imu_camera_sync_rtsp_multicam.py --host 192.168.2.140 --stream cam0 --stream cam1 \\
        --imu wit=WT901BLE68 --duration 60 --resample-hz 16 --loop --resample-only \\
        --out-dir data/rtsp_multicam --warmup-sec 10

    # 探测每路流的分辨率/帧率 + IMU 实际输出频率
    python imu_camera_sync_rtsp_multicam.py --host 192.168.2.140 --stream cam0 --stream cam1 \\
        --imu wit=WT901BLE68 --probe

--stream 可重复传，第一个对应 cam1，第二个对应 cam2，以此类推（跟本地摄像头版本
的 --camera 编号规则一致，方便文件命名/延迟配置对应）。--imu 用法跟
imu_camera_sync_multi.py 完全一样。

输出:
    {base}_cam1.mp4, {base}_cam2.mp4...          每路流各自的视频（VFR，含叠加信息）
    {base}.csv                                    每个"tick"一行的组合CSV（未做延迟补偿，仅供调试）
    {base}_meta.csv                                每行的对齐信息
    {base}_imu1_raw.csv...                         各 IMU 设备的原始全量流水
    {base}_cam1_imu1_resampled{HZ}hz.mp4/.csv...   每路流 x 每个设备的降采样配对文件
                                                    （已按该路流的延迟值补偿）
"""

import argparse
import csv
import os
import shutil
import sys
import threading
import time
from datetime import datetime

# 必须在 import cv2 之前设置（RTSP 低延迟选项）；imu_camera_sync_rtsp 自己
# 也会设置一次（setdefault 幂等），这里先设是为了保证我们自己后面的 import cv2
# 之前一定已经生效，不依赖模块导入顺序。
os.environ.setdefault(
    'OPENCV_FFMPEG_CAPTURE_OPTIONS',
    'rtsp_transport;tcp|fflags;nobuffer|flags;low_delay',
)

try:
    import cv2
except ImportError:
    print('缺少 opencv-python，请先安装: pip install opencv-python')
    sys.exit(1)

from imu_camera_sync import _FfmpegVfrSink, _Cv2CfrSink, resample_raw_imu
from imu_camera_sync_multi import (
    ImuDevice, ble_thread_main, parse_imu_spec, stop_event, _new_sample_event,
)
from imu_camera_sync_rtsp import LatestFrameReader, build_rtsp_url, load_latency_config, _cache_key


class RtspCameraStream:
    """一路 RTSP 流的独立状态：VideoCapture(RTSP) + LatestFrameReader、视频写入、fps统计、
    这一路自己的延迟补偿值（不同流的RTSP链路延迟可能不一样）。"""

    def __init__(self, host: str, port: int, stream: str, label: str,
                 resize_to, latency_ms: float):
        self.host = host
        self.port = port
        self.stream = stream
        self.label = label
        self.resize_to = resize_to
        self.latency_ms = latency_ms
        self.url = build_rtsp_url(host, port, stream)

        cap_raw = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        if not cap_raw.isOpened():
            raise RuntimeError(f'无法打开 RTSP 流: {self.url}（{label}）')
        self.cap = LatestFrameReader(cap_raw)

        self.actual_w = self.actual_h = None
        wait_until = time.time() + 5.0
        while time.time() < wait_until:
            ok, frame = self.cap.read()
            if ok and frame is not None:
                if resize_to:
                    frame = cv2.resize(frame, resize_to)
                self.actual_h, self.actual_w = frame.shape[:2]
                break
            time.sleep(0.05)
        if self.actual_w is None:
            self.cap.release()
            raise RuntimeError(f'等待 RTSP 首帧超时: {self.url}（{label}）')

        self.video_writer = None
        self.ts_window: list[float] = []

    def read(self):
        ok, frame = self.cap.read()
        if ok and frame is not None and self.resize_to:
            frame = cv2.resize(frame, self.resize_to)
        return ok, frame

    def fps_tick(self, now: float) -> float:
        cutoff = now - 1.0
        while self.ts_window and self.ts_window[0] < cutoff:
            self.ts_window.pop(0)
        self.ts_window.append(now)
        return float(len(self.ts_window))

    def close_writer(self):
        if self.video_writer:
            self.video_writer.close()
            self.video_writer = None

    def release(self):
        self.cap.release()
        self.close_writer()


def draw_overlay(frame, cam_label, cam_fps, target_fps, imu_info, elapsed, frame_idx, latency_ms):
    # 不再画半透明底框——纯文字叠加，字体带黑色描边保证在任意背景色的画面
    # 上都看得清，不遮挡画面内容。
    def put(text, row, color=(200, 255, 200)):
        pos = (12, 28 + row * 26)
        cv2.putText(frame, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA)

    ts = datetime.now().strftime('%H:%M:%S.%f')[:12]
    put(f'{ts}  [{cam_label}]  #{frame_idx}  t={elapsed:.1f}s  {cam_fps:.1f}/{target_fps}fps'
        f'  lat={latency_ms:+.0f}ms', 0, (255, 255, 100))
    row = 1
    for device, hz, lag_ms, missing, imu_row in imu_info:
        if missing or imu_row is None:
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
        put(text, row, color)
        row += 1
        # 6轴实时数值：方便肉眼判断设备是不是静置在桌上没戴（加速度接近
        # (0,0,1g)、角速度接近0）还是真的戴在狗身上有动作。
        if not missing and imu_row is not None:
            acc_text = (f'  Acc  X={imu_row["acc_x"]:+.3f} Y={imu_row["acc_y"]:+.3f} '
                        f'Z={imu_row["acc_z"]:+.3f} g')
            gyro_text = (f'  Gyro X={imu_row["gyro_x"]:+7.2f} Y={imu_row["gyro_y"]:+7.2f} '
                         f'Z={imu_row["gyro_z"]:+7.2f} °/s')
            put(acc_text, row, (200, 200, 200))
            row += 1
            put(gyro_text, row, (200, 200, 200))
            row += 1
    return frame


def run_cameras(args, cameras: list, devices: list):
    target_fps = args.cam_fps

    for cam in cameras:
        print(f'{cam.label} ({cam.url}): {cam.actual_w}x{cam.actual_h}  目标帧率: {target_fps}fps  '
              f'延迟补偿: {cam.latency_ms:+.0f}ms')

    record_mode = args.duration and args.duration > 0
    loop_mode = args.loop and record_mode
    if args.loop and not record_mode:
        print('警告: --loop 需要配合 --duration 使用，已忽略 --loop。')

    if record_mode and args.warmup_sec > 0:
        print(f'预热 {args.warmup_sec:.1f}s...')
        until = time.time() + args.warmup_sec
        while time.time() < until and not stop_event.is_set():
            for cam in cameras:
                cam.read()
            time.sleep(1.0 / target_fps)
        imu_report = '  '.join(f'{d.label}={d.current_hz():.1f}Hz' for d in devices)
        print(f'预热结束: {imu_report}')

    try:
        segment_no = 0
        while True:
            segment_no += 1
            if loop_mode:
                print(f'\n════ 第 {segment_no} 段录制开始 ════')
            should_stop = _run_one_segment(args, cameras, devices, target_fps, record_mode)
            if not loop_mode or should_stop or stop_event.is_set():
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for cam in cameras:
            cam.release()
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass


def _run_one_segment(args, cameras: list, devices: list, target_fps: int, record_mode: bool) -> bool:
    should_stop = [False]
    frame_interval = 1.0 / target_fps
    save_overlay = not args.no_save_overlay
    imu_sync = not args.no_imu_sync

    # 精确到毫秒，避免 --loop 循环录制时文件名撞车互相覆盖。
    ts_tag = datetime.now().strftime('%Y%m%d_%H%M%S%f')[:-3]
    os.makedirs(args.out_dir, exist_ok=True)
    base = os.path.join(args.out_dir, f'rtspmulticam_{ts_tag}')

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
            print(f'  {cam.label} 视频→{video_path}（延迟补偿 {cam.latency_ms:+.0f}ms，'
                  f'仅应用于降采样配对文件，本视频本身不受影响）')

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

        print(f'录制模式: {args.duration}s  组合CSV→{base}.csv（未做延迟补偿，仅供调试对齐参考）'
              f'  对齐信息→{base}_meta.csv')
        for d in devices:
            print(f'  {d.label} 原始流水→{base}_{d.label}_raw.csv')
    else:
        print('实时模式（按 Q 或 Ctrl+C 退出）。')

    start_time = time.time()
    next_tick = start_time
    frame_idx = 0
    elapsed = 0.0
    first_tick_ts_ms = None
    last_tick_ts_ms = None
    max_lag_ms = 3 * (1000.0 / target_fps)

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

            tick_ts = time.time()
            tick_ts_ms = tick_ts * 1000.0
            frame_idx += 1
            elapsed = tick_ts - start_time
            if tick_ts < start_time:
                continue

            if first_tick_ts_ms is None:
                first_tick_ts_ms = tick_ts_ms
            last_tick_ts_ms = tick_ts_ms

            frames = []
            read_failed = False
            for cam in cameras:
                ret, frame = cam.read()
                if not ret or frame is None:
                    # RTSP 流偶尔瞬时抖动拿不到帧很常见，不像本地摄像头那样直接判定
                    # 硬件故障退出；这里跳过这个tick的整体处理，下个tick重试。
                    read_failed = True
                    break
                frames.append(frame)
            if read_failed:
                time.sleep(0.01)
                continue

            tick_ts_str = datetime.fromtimestamp(tick_ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            cam_fps_list = [cam.fps_tick(tick_ts) for cam in cameras]

            csv_row = [tick_ts_str]
            meta_row = [frame_idx, tick_ts_str] + [f'{fps:.1f}' for fps in cam_fps_list]
            imu_info = []

            # 注意：这里逐帧组合CSV用的是未做延迟补偿的 tick_ts_ms 做匹配——多路
            # 摄像头各自延迟不同，没法在"一份共用CSV"里同时精确补偿每一路，这份
            # 文件仅供调试/参考。真正标注用的降采样配对文件会在录制结束后按
            # 每一路自己的 latency_ms 分别正确计算（见下面 resample 部分）。
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
                imu_info.append((d, hz, lag_ms, missing, imu_row))

            if csv_writer:
                csv_writer.writerow(csv_row)
            if meta_writer:
                meta_writer.writerow(meta_row)

            for cam, frame, cam_fps in zip(cameras, frames, cam_fps_list):
                display = draw_overlay(frame.copy(), cam.label, cam_fps, target_fps, imu_info,
                                        elapsed, frame_idx, cam.latency_ms)
                if cam.video_writer:
                    cam.video_writer.write(display if save_overlay else frame)
                try:
                    cv2.imshow(f'IMU(RTSP multicam) {cam.label}', display)
                except cv2.error:
                    if not record_mode:
                        print('cv2.imshow 不支持（可能是 headless 版本）。')
                        read_failed = True
                        should_stop[0] = True

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
                should_stop[0] = True
                break

    except KeyboardInterrupt:
        should_stop[0] = True
    finally:
        if stop_event.is_set():
            should_stop[0] = True
        for cam in cameras:
            cam.close_writer()
        if csv_file:
            csv_file.close()
        if meta_file:
            meta_file.close()
        for d in devices:
            d.set_raw_writer(None)
            if hasattr(d, '_raw_file'):
                d._raw_file.close()
        print(f'\n共采集 {frame_idx} 个同步tick  {elapsed:.1f}s')
        if record_mode:
            print(f'已保存: {base}.csv  {base}_meta.csv')
            for cam in cameras:
                print(f'       {base}_{cam.label}.mp4')
            for d in devices:
                print(f'       {base}_{d.label}_raw.csv')
            print()
            print('── 自动对齐校验（各摄像头帧数 vs 组合CSV行数）──')
            for cam in cameras:
                video_path = f'{base}_{cam.label}.mp4'
                cap_check = cv2.VideoCapture(video_path)
                actual_frames = int(cap_check.get(cv2.CAP_PROP_FRAME_COUNT))
                cap_check.release()
                if actual_frames == frame_idx:
                    print(f'  [{cam.label}] ✔ 帧数与组合CSV行数一致: {actual_frames}')
                else:
                    print(f'  [{cam.label}] ✘ 帧数不一致: 视频 {actual_frames} 帧, CSV {frame_idx} 行')

            print()
            print('── 降采样（每路摄像头 x 每个设备各生成一对同名 mp4/csv，按各自的延迟补偿值计算）──')
            resampled_pairs = []  # (cam_label, device_label, resampled_base)
            for d in devices:
                for cam in cameras:
                    pair_base = f'{base}_{cam.label}_{d.label}_resampled{args.resample_hz:g}hz'
                    # 注意：跟本地摄像头版本的 multicam 脚本不同，这里不能"算一次、
                    # 复制给其它摄像头"——因为每一路RTSP流的延迟补偿值可能不一样，
                    # 每一对都要用这一路自己的 latency_ms 重新计算一次。
                    resample_raw_imu(
                        f'{base}_{d.label}_raw.csv', f'{pair_base}.csv', args.resample_hz,
                        t_start_ms=first_tick_ts_ms, t_end_ms=last_tick_ts_ms,
                        latency_ms=cam.latency_ms,
                    )
                    try:
                        shutil.copyfile(f'{base}_{cam.label}.mp4', f'{pair_base}.mp4')
                        print(f'  {pair_base}.mp4 / .csv（{cam.label} 视频 + {d.label} 降采样数据，'
                              f'延迟补偿 {cam.latency_ms:+.0f}ms，文件名一致可直接拖拽配对）')
                        resampled_pairs.append((cam.label, d.label, pair_base))
                    except OSError as e:
                        print(f'生成 {pair_base} 配对文件失败: {e}')

            if args.resample_only:
                for cam in cameras:
                    try:
                        os.remove(f'{base}_{cam.label}.mp4')
                    except OSError as e:
                        print(f'删除 {base}_{cam.label}.mp4 失败: {e}')
                for p in (f'{base}.csv', f'{base}_meta.csv'):
                    try:
                        os.remove(p)
                    except OSError as e:
                        print(f'删除 {p} 失败: {e}')
                for d in devices:
                    try:
                        os.remove(f'{base}_{d.label}_raw.csv')
                    except OSError as e:
                        print(f'删除 {base}_{d.label}_raw.csv 失败: {e}')
                print(f'\n--resample-only: 已删除原始文件，只保留各摄像头x设备的 resampled mp4/csv')

    return should_stop[0]


def run_probe(args, devices: list, probe_seconds: float = 5.0):
    """探测每路RTSP流的分辨率/帧率 + 短暂连接所有 IMU 设备测量各自实际输出频率，不录制。"""
    resize_to = None
    if args.resize:
        w, h = args.resize.lower().split('x')
        resize_to = (int(w), int(h))

    from imu_camera_sync_rtsp import probe_rtsp
    for i, stream in enumerate(args.stream, start=1):
        url = build_rtsp_url(args.host, args.port, stream)
        latency_ms = load_latency_config(args.latency_config_file, args.host, args.port, stream) or 0.0
        print(f'── cam{i} ({stream}) ── 延迟补偿配置: {latency_ms:+.0f}ms')
        probe_rtsp(url, resize_to=resize_to)

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


def main():
    ap = argparse.ArgumentParser(description='多路 RTSP 摄像头（go2rtc/micam_dev）+ 多个 IMU 设备同步采集')
    ap.add_argument('--host', required=True, help='go2rtc 服务地址，如 192.168.2.140')
    ap.add_argument('--port', type=int, default=8554, help='go2rtc RTSP 端口，默认 8554')
    ap.add_argument('--stream', action='append', required=True,
                    help='go2rtc 流名称，可重复传多个，例如 --stream cam0 --stream cam1'
                         '（所有流共用同一个 --host/--port）')
    ap.add_argument('--imu', action='append', required=True,
                    help='IMU 设备，格式 类型=标识，可重复传多个。见 imu_camera_sync_multi.py 说明。')
    ap.add_argument('--resize', metavar='WxH', default=None,
                    help='把每路流的每帧都缩放到指定尺寸，如 1280x720')
    ap.add_argument('--cam-fps', type=int, default=20, help='目标帧率，默认 20（RTSP实际到达速率取决于网络）')
    ap.add_argument('--duration', type=float, default=0, help='录制时长（秒），0=实时模式不保存')
    ap.add_argument('--warmup-sec', type=float, default=5.0, help='预热时长（秒），默认 5，设 0 关闭')
    ap.add_argument('--video-crf', type=int, default=28, help='H.264 CRF，默认 28')
    ap.add_argument('--out-dir', default='data', help='输出目录，默认 data/')
    ap.add_argument('--scan-timeout', type=float, default=8.0, help='BLE 扫描超时（秒），默认 8')
    ap.add_argument('--no-save-overlay', action='store_true', help='保存干净视频（不含叠加信息）')
    ap.add_argument('--no-imu-sync', action='store_true', help='关闭事件驱动同步，改用固定定时器抓帧')
    ap.add_argument('--resample-hz', type=float, default=25.0,
                    help='录制结束后把每个设备的原始IMU流水降采样到该频率，默认25Hz')
    ap.add_argument('--resample-only', action='store_true',
                    help='只保留各摄像头x设备的降采样版文件，删除原始的 {base}_camN.mp4/.csv/_meta.csv/_raw.csv')
    ap.add_argument('--loop', action='store_true',
                    help='循环录制模式：每段 --duration 秒，录完自动开始下一段，直到按 Q/ESC 或 Ctrl+C 才停止')
    ap.add_argument('--probe', action='store_true',
                    help='只探测每路流的分辨率/帧率 + 各IMU设备当前实际输出频率，不录制')
    ap.add_argument('--video-latency-ms', type=float, default=None,
                    help='给所有流统一指定同一个延迟补偿值（毫秒），优先级高于配置文件，'
                         '会覆盖每一路流各自在配置文件里的值。不指定的话每一路流分别按'
                         '--latency-config-file 里各自的 host:port/stream 读取。')
    ap.add_argument('--latency-config-file', default=os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), '.rtsp_latency_cache.json'),
                    help='延迟补偿配置文件路径，JSON格式，按 host:port/stream 分别配置每一路流，比如：'
                         '{"192.168.2.140:8554/cam0": {"latency_ms": 700}, '
                         '"192.168.2.140:8554/cam1": {"latency_ms": 900}}。'
                         '默认读脚本同目录下的 .rtsp_latency_cache.json（跟 imu_camera_sync_rtsp.py 共用同一份配置）。')
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

    resize_to = None
    if args.resize:
        w, h = args.resize.lower().split('x')
        resize_to = (int(w), int(h))

    cameras = []
    for i, stream in enumerate(args.stream, start=1):
        if args.video_latency_ms is not None:
            latency_ms = args.video_latency_ms
        else:
            latency_ms = load_latency_config(args.latency_config_file, args.host, args.port, stream) or 0.0
        try:
            cameras.append(RtspCameraStream(args.host, args.port, stream, f'cam{i}', resize_to, latency_ms))
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
