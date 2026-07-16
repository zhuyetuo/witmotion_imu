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
    动作提前一个"RTSP延迟"的量，是系统性偏差。

    延迟补偿值从一个 JSON 配置文件里读（默认脚本同目录下的
    .rtsp_latency_cache.json），按 host:port/stream 分别配置，比如：
        {
          "192.168.2.140:8554/cam0": {"latency_ms": 700}
        }
    这个值需要自己测出来再填进去（比如对着摄像头把设备晃一下，人工看视频
    里动作出现的时刻和IMU数据里加速度突变的时刻差多少毫秒），脚本每次运行
    会自动读取这个文件、按 host:port/stream 匹配对应的延迟值并应用，不用
    每次都在命令行传参数。也可以用 --video-latency-ms 命令行直接指定一个
    值，优先级比配置文件高。

用法:
    # 先探测 RTSP 流实际能拿到的分辨率/帧率
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --probe --device wit --name WTSDCL

    # 录制60秒（WitMotion）；延迟补偿会自动从 .rtsp_latency_cache.json 里读
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL --duration 60

    # 指定降采样到16Hz，循环录制，只保留降采样版
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL \\
        --duration 60 --resample-hz 16 --loop --resample-only --out-dir data/rtsp

    # 下游画面统一缩放到指定尺寸（RTSP流本身只有几档固定质量，不支持任意分辨率，
    # 想要精确到某个尺寸用这个参数，效果类似 capture_frame.py 的 --resize）
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL --resize 1280x720

    # 命令行直接指定延迟补偿值（毫秒），跳过配置文件
    python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL \\
        --duration 60 --video-latency-ms 700
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


# ── 延迟补偿配置文件 ─────────────────────────────────────────────────────────
# RTSP 链路延迟基本是 go2rtc/摄像头/网络这条链路本身的固有属性，跟每次录制无关。
# 延迟值由使用者自己测出来后手动写进这个 JSON 文件（按 host:port/stream 分别
# 配置），脚本每次运行会自动读取并应用对应的值，不用每次都在命令行传参数。
# 文件格式:
#   {
#     "192.168.2.140:8554/cam0": {"latency_ms": 700}
#   }

def _cache_key(host: str, port: int, stream: str) -> str:
    return f'{host}:{port}/{stream}'


def load_latency_config(config_file: str, host: str, port: int, stream: str):
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    entry = data.get(_cache_key(host, port, stream))
    if not entry:
        return None
    return entry.get('latency_ms')


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

    # 视频延迟补偿量（毫秒）：优先级 显式 --video-latency-ms > 配置文件里
    # 这个 host:port/stream 对应的值 > 0（不补偿）。
    latency_ms = args.video_latency_ms
    if latency_ms is not None:
        print(f'使用命令行指定的视频延迟补偿: {latency_ms:+.0f}ms（--video-latency-ms）')
    else:
        configured = load_latency_config(args.latency_config_file, args.host, args.port, args.stream)
        if configured is not None:
            latency_ms = configured
            print(f'使用配置文件里的视频延迟补偿: {latency_ms:+.0f}ms'
                  f'（{args.latency_config_file}，{_cache_key(args.host, args.port, args.stream)}）')
        else:
            latency_ms = 0.0
            if record_mode:
                print(f'未在 {args.latency_config_file} 里找到 {_cache_key(args.host, args.port, args.stream)} '
                      '对应的延迟补偿值，本次不做补偿。')

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
                latency_ms=latency_ms,
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
    ap.add_argument('--video-latency-ms', type=float, default=None,
                    help='手动指定视频延迟补偿量（毫秒，视频比IMU晚多少），查找IMU样本时会用'
                         '"视频帧时间戳 - 这个值"去匹配，优先级高于配置文件。'
                         '不指定的话，从 --latency-config-file 里按 host:port/stream 读取；'
                         '都没有就是 0（不补偿）。')
    ap.add_argument('--latency-config-file', default=os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), '.rtsp_latency_cache.json'),
                    help='延迟补偿配置文件路径，JSON格式，按 host:port/stream 分别配置，比如：'
                         '{"192.168.2.140:8554/cam0": {"latency_ms": 700}}。'
                         '延迟值需要自己测出来后手动写进去；默认读脚本同目录下的 .rtsp_latency_cache.json。')
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
