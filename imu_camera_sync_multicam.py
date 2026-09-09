# -*- coding: utf-8 -*-
"""
多个摄像头 + 多个 IMU 设备 同步采集脚本
==========================================

在 imu_camera_sync_multi.py（一个摄像头+多IMU）的基础上再扩展一维：支持
同时开多路摄像头，每路摄像头独立写视频，所有摄像头 + 所有 IMU 设备共用
同一份"每帧一行"的组合 CSV（同一时刻抓取所有摄像头的画面 + 匹配所有 IMU
设备最近的样本）。

功能已跟 imu_camera_sync.py / imu_camera_sync_multi.py 对齐：--loop /
--resample-hz / --probe / --resample-only 全部支持。

用法:
    # 2个摄像头 + 2个IMU设备
    python imu_camera_sync_multicam.py --camera 0 --camera 1 \\
        --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --duration 60

    # 探测硬件能力：每路摄像头 + 每个 IMU 设备实际输出频率
    python imu_camera_sync_multicam.py --camera 0 --camera 1 \\
        --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --probe

    # 每个设备降采样到16Hz，只保留降采样版文件，循环录制每段3分钟
    python imu_camera_sync_multicam.py --camera 0 --camera 1 \\
        --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A \\
        --duration 180 --resample-hz 16 --resample-only --loop

--camera 可重复传，第一个对应 cam1，第二个对应 cam2，以此类推。
--imu 用法跟 imu_camera_sync_multi.py 完全一样（type=标识，可重复传）。

输出:
    {base}_cam1_raw.mp4, {base}_cam2_raw.mp4... 每路摄像头各自的原始视频（VFR，含叠加信息，
                                                跟 {base}_imu1_raw.csv 等同名后缀"_raw"风格一致）
    {base}.csv                                 每个"tick"一行：timestamp, imu1_acc_x...
                                                （所有 IMU 设备的 acc/gyro，按 --imu 顺序）
    {base}_meta.csv                            每行的对齐信息：各摄像头的 fps，各IMU设备的
                                                lag_ms/missing/hz
    {base}_imu1_raw.csv, {base}_imu2_raw.csv... 各 IMU 设备的原始全量流水
    {base}_cam1_imu1_resampled{HZ}hz.mp4/.csv...  每路摄像头 x 每个设备的降采样配对文件
                                                （--resample-hz 指定目标频率，默认25）
"""

import log_setup
import argparse
import csv
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta

try:
    import cv2
except ImportError:
    print('缺少 opencv-python，请先安装: pip install opencv-python')
    sys.exit(1)

from imu_camera_sync import (
    _FfmpegVfrSink, _Cv2CfrSink, _measure_actual_fps, probe_camera, resample_raw_imu, open_camera,
    write_anchored_raw_csv,
)
from imu_camera_sync_multi import (
    ImuDevice, ble_thread_main, parse_imu_spec, precheck_devices, stop_event, _new_sample_event, RAW_CSV_HEADER,
)


def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f'{secs}s'
    if secs < 3600:
        return f'{secs // 60}m{secs % 60:02d}s'
    return f'{secs // 3600}h{(secs % 3600) // 60:02d}m'


class CameraStream:
    """一路摄像头的独立状态：VideoCapture、视频写入、fps 统计。"""

    def __init__(self, index: int, label: str, width: int, height: int, target_fps: int,
                 backend: str = 'auto', fourcc: str = 'MJPG', autofocus=None, auto_wb=None,
                 capture_width: int = 0, capture_height: int = 0, show_settings_dialog: bool = False):
        self.index = index
        self.label = label
        # 采集分辨率（比如广角摄像头的原生2K）跟最终输出分辨率分开：直接向驱动
        # 请求较低分辨率时，很多广角摄像头给的是传感器中间裁切出来的一小块画面
        # （视野变窄），不是完整画幅等比缩小；按原生高分辨率采集、软件缩放到
        # 输出分辨率，才能保住完整广角视野。不指定 capture_width/height 时
        # 两者相同，行为和之前完全一样。
        self.actual_w, self.actual_h = width, height  # 最终输出/写入视频的分辨率
        # 重连时要用同一套参数重新 open_camera，全部记下来
        self._open_kwargs = dict(index=index, width=capture_width or width, height=capture_height or height,
                                 fps=target_fps, backend=backend, fourcc=fourcc, autofocus=autofocus,
                                 auto_wb=auto_wb, show_settings_dialog=False)
        self.cap = open_camera(**{**self._open_kwargs, 'show_settings_dialog': show_settings_dialog})
        if not self.cap.isOpened():
            raise RuntimeError(f'无法打开摄像头 {index}（{label}），可以试试 --backend dshow/msmf')
        self._after_open()
        self.video_writer = None
        self.ts_window: list[float] = []
        # 断联/重连状态：某一路摄像头中途掉了（USB 松了、供电不稳、驱动挂了）不能
        # 把整个采集停掉——其它摄像头和 IMU 还好好的。掉了的这一路写占位黑帧
        # 顶住（保证视频帧数 == 组合CSV行数，对齐关系不乱），后台按间隔尝试重新
        # 打开，连回来了就无缝接着录。连续读失败 FAIL_STREAK_TO_DOWN 次才算掉线，
        # 偶尔一帧读不到不算。
        self.down = False
        self.fail_streak = 0
        self.down_since: float | None = None
        self.next_retry = 0.0
        # 重连间隔指数退避 5s→10s→…封顶 RETRY_MAX_INTERVAL：真松了要人去插回来的话
        # 可能一断就是一天，每 5s 反复 open_camera 一整天既白耗 CPU，Windows 上还
        # 可能把摄像头驱动栈折腾出问题；连上一次就重置回 5s
        self.retry_interval = self.RETRY_MIN_INTERVAL
        self.last_frame = None
        self.dropped_ticks = 0  # 这一段里写了多少个占位帧，结束时汇报
        self.last_down_report = 0.0

    FAIL_STREAK_TO_DOWN = 3
    RETRY_MIN_INTERVAL = 5.0
    RETRY_MAX_INTERVAL = 60.0
    DOWN_REPORT_INTERVAL = 60.0  # 断联期间每隔这么久在终端提醒一次

    def down_seconds(self, now: float) -> float:
        return now - self.down_since if self.down and self.down_since is not None else 0.0

    def _after_open(self):
        driver_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        driver_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.need_resize = (driver_w, driver_h) != (self.actual_w, self.actual_h)
        if self.need_resize:
            print(f'{self.label}: 采集 {driver_w}x{driver_h} → 缩放输出 {self.actual_w}x{self.actual_h}')

    def read(self):
        ret, frame = self.cap.read()
        if ret and self.need_resize:
            frame = cv2.resize(frame, (self.actual_w, self.actual_h))
        return ret, frame

    def _placeholder(self, now: float):
        """掉线期间的占位帧：黑底 + 提示文字，写进视频保持帧数对齐，画面上也一眼能看出这路断了。"""
        import numpy as np
        frame = np.zeros((self.actual_h, self.actual_w, 3), dtype=np.uint8)
        for i, text in enumerate((f'{self.label} DISCONNECTED', f'reconnecting... {_fmt_duration(self.down_seconds(now))}')):
            pos = (20, self.actual_h // 2 - 20 + i * 40)
            cv2.putText(frame, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(frame, text, pos, cv2.FONT_HERSHEY_SIMPLEX, 1.0, (80, 80, 255), 2, cv2.LINE_AA)
        return frame

    def _try_reconnect(self, now: float) -> bool:
        if now - self.last_down_report >= self.DOWN_REPORT_INTERVAL:
            self.last_down_report = now
            print(f'!! {self.label} 已断联 {_fmt_duration(self.down_seconds(now))}，仍在尝试重连'
                  f'（间隔 {self.retry_interval:.0f}s）——如果是线松了需要人去插回去')
        if now < self.next_retry:
            return False
        self.next_retry = now + self.retry_interval
        self.retry_interval = min(self.retry_interval * 2, self.RETRY_MAX_INTERVAL)
        try:
            cap = open_camera(**self._open_kwargs)
            ok = cap.isOpened()
            if ok:
                ret, _ = cap.read()
                ok = bool(ret)
            if not ok:
                cap.release()
                return False
        except Exception as e:  # noqa: BLE001 驱动层什么异常都可能抛，重连失败就下次再试
            print(f'{self.label} 重连异常: {e}')
            return False
        self.cap = cap
        self._after_open()
        print(f'{self.label} 已重新连上（断了 {_fmt_duration(self.down_seconds(now))}）')
        self.down = False
        self.fail_streak = 0
        self.down_since = None
        self.retry_interval = self.RETRY_MIN_INTERVAL
        return True

    def read_resilient(self, now: float):
        """主循环用这个：永远返回一帧（真实帧或占位帧）+ 这帧是不是真的。
        掉线了就走重连逻辑，不抛异常、不让调用方停。"""
        if self.down:
            if self._try_reconnect(now):
                ret, frame = self.read()
                if ret:
                    self.last_frame = frame
                    return frame, True
                self.down = True
            self.dropped_ticks += 1
            return self._placeholder(now), False

        ret, frame = self.read()
        if ret:
            self.fail_streak = 0
            self.last_frame = frame
            return frame, True

        self.fail_streak += 1
        if self.fail_streak >= self.FAIL_STREAK_TO_DOWN:
            self.down = True
            self.down_since = now
            self.retry_interval = self.RETRY_MIN_INTERVAL
            self.next_retry = now + self.retry_interval
            self.last_down_report = now
            print(f'{self.label} 连续 {self.fail_streak} 次读取失败，判定断联，其它摄像头/IMU 继续录，'
                  f'这一路写占位帧并尝试重连（{self.RETRY_MIN_INTERVAL:.0f}s 起指数退避到 {self.RETRY_MAX_INTERVAL:.0f}s）')
            try:
                self.cap.release()
            except Exception:  # noqa: BLE001
                pass
        self.dropped_ticks += 1
        # 还没判定掉线的那一两帧，用上一帧顶一下比黑帧自然
        if self.last_frame is not None:
            return self.last_frame, False
        return self._placeholder(now), False

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
        try:
            self.cap.release()
        except Exception:  # noqa: BLE001 掉线时 cap 可能已经 release 过
            pass
        self.close_writer()


def draw_overlay(frame, cam_label, cam_fps, target_fps, imu_info, elapsed, frame_idx,
                  show_imu_values: bool = False, show_frame_info: bool = False,
                  down_cams: list[str] | None = None, alpha: float = 0.45):
    """
    画面左上角的叠加信息。

    分两档画，规则是「常态信息做水印，异常信息不打折」：

    - 常态（时间、机位、fps、每只狗的名字/编号/采样率）半透明，alpha 默认 0.45。
      这些字是录一整天都在的，画满不透明等于在每一帧上永久糊掉左上角，
      而那块位置照样是画面，狗真会走到那儿去。
    - 异常（某个设备 MISSING、某路摄像头掉线）不透明。它们是要人立刻去处理的，
      淡化了就等于藏起来。

    半透明只混合文字覆盖的那一小块，不动整帧。
    第一版是整帧 copy() + addWeighted，在 6 路 25fps 下就是每秒 150 次全帧
    （1280x720x3）的拷贝加混合，纯属白烧 CPU——文字实际只占左上角一小块。
    先用 getTextSize 量出真正要盖多大，只在那块 ROI 上混。
    """
    font, scale, thick = cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1

    def pos(row):
        return (12, 28 + row * 26)

    def _put(canvas, text, row, color):
        p = pos(row)
        # 先画黑色粗一点当描边，再画正常颜色：不加描边的话，字压到浅色背景
        # （白墙、地板反光）上就完全看不见了，何况还要再淡一层
        cv2.putText(canvas, text, p, font, scale, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(canvas, text, p, font, scale, color, thick, cv2.LINE_AA)

    marks = []   # 常态：(文字, 行号, 颜色)，混合后变淡
    alarms = []  # 异常：同上，但不打折

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:23]
    # #frame_idx（同步tick序号，核对丢帧用）/ t=elapsed（这一段已经录了多久）
    # 平时看画面用不上，默认不显示，排查对齐/丢帧问题时加 --show-frame-info 打开。
    if show_frame_info:
        marks.append((f'{ts}  [{cam_label}]  #{frame_idx}  t={elapsed:.1f}s  {cam_fps:.1f}/{target_fps}fps', 0, (255, 255, 100)))
    else:
        marks.append((f'{ts}  [{cam_label}]  {cam_fps:.1f}/{target_fps}fps', 0, (255, 255, 100)))

    row = 1
    for device, hz, lag_ms, missing, imu_row in imu_info:
        # 名字后面跟上 imu 编号：文件名和 CSV 列名用的都是 imu1/imu2，画面上只有
        # 狗名的话，回头对着录像核"这条曲线是谁"还得再去翻当时的启动参数
        who = f'{device.display_name}/{device.label}' if device.display_name != device.label else device.label
        if missing or imu_row is None:
            alarms.append((f'[{who}] MISSING', row, (80, 80, 255)))
        else:
            # lag 的数值不再显示——它每帧都在跳，盯着也没有可操作性，真掉线了看
            # MISSING 就够。但还是拿它决定颜色：绿=跟得上，黄=有点滞后，红=明显
            # 滞后，扫一眼就知道健康不健康，不占任何字宽。
            color = (100, 255, 100) if lag_ms < 50 else (50, 200, 255) if lag_ms < 150 else (80, 80, 255)
            marks.append((f'[{who}] {hz:.1f}Hz', row, color))
        row += 1
        # 6轴实时数值：方便肉眼判断设备是不是静置在桌上没戴（加速度接近
        # (0,0,1g)、角速度接近0）还是真的戴在狗身上有动作。默认不显示（太占画面），
        # 需要看的时候加 --show-imu-values 打开。
        if show_imu_values and not missing and imu_row is not None:
            marks.append((f'  Acc  X={imu_row["acc_x"]:+.3f} Y={imu_row["acc_y"]:+.3f} '
                          f'Z={imu_row["acc_z"]:+.3f} g', row, (200, 200, 200)))
            row += 1
            marks.append((f'  Gyro X={imu_row["gyro_x"]:+7.2f} Y={imu_row["gyro_y"]:+7.2f} '
                          f'Z={imu_row["gyro_z"]:+7.2f} °/s', row, (200, 200, 200)))
            row += 1

    # 别的摄像头断了，在每一路画面上都挂一条红字——断掉那路的窗口是黑的没人看，
    # 得让盯着任何一个窗口的人都能看见"有一路掉了，去插线"
    if down_cams:
        alarms.append(('!! ' + '  '.join(down_cams) + '  <- check cable', row, (60, 60, 255)))

    if marks:
        h, w = frame.shape[:2]
        # 量出常态文字真正占多大，只在这块上混合。+16 给描边和抗锯齿留边，
        # 少了会把最右边一列像素切掉，看着像字被啃了一口
        x1 = min(w, max(cv2.getTextSize(t, font, scale, 3)[0][0] for t, _, _ in marks) + 12 + 16)
        y1 = min(h, pos(max(r for _, r, _ in marks))[1] + 12)
        roi = frame[0:y1, 0:x1]
        wm = roi.copy()
        for text, r, color in marks:
            _put(wm, text, r, color)
        cv2.addWeighted(wm, alpha, roi, 1.0 - alpha, 0, roi)

    for text, r, color in alarms:
        _put(frame, text, r, color)
    return frame


def _csv_has_data_rows(path: str) -> bool:
    """CSV 除了表头之外还有没有真实数据行。"""
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            f.readline()  # 表头
            for line in f:
                if line.strip():
                    return True
    except OSError:
        return False
    return False


def _seconds_to_next_hour(now: datetime) -> float:
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return (next_hour - now).total_seconds()


def _pairs_for(cameras, device, pair_filter):
    """这个设备要跟哪几路摄像头配对。pair_filter 为空 = 全排列（老行为）。

    狗场那种「一间一狗一摄像头」的场地必须限制：cam_i 和 imu_i 严格一一对应，
    全排列出来 36 份里 30 份是「A 房间的画面配 B 房间的狗」，纯废文件——而且每份
    都是一小时的 720p 视频拷贝，磁盘是成倍烧的。
    影棚那种一个大空间多只狗的场地相反：哪路摄像头拍到哪只狗事先不知道，
    全排列是有意义的，所以默认不限制。
    """
    if not pair_filter:
        return list(cameras)
    return [c for c in cameras if (c.label, device.label) in pair_filter]


def run_cameras(args, cameras: list[CameraStream], devices: list[ImuDevice], pair_filter=None):
    target_fps = args.cam_fps

    for cam in cameras:
        print(f'{cam.label} ({cam.index}): {cam.actual_w}x{cam.actual_h}  目标帧率: {target_fps}fps')

    record_mode = (args.duration and args.duration > 0) or args.align_hourly
    loop_mode = args.loop and record_mode
    if args.loop and not record_mode:
        print('警告: --loop 需要配合 --duration 或 --align-hourly 使用，已忽略 --loop。')

    if record_mode and args.warmup_sec > 0:
        print(f'预热 {args.warmup_sec:.1f}s...')
        until = time.time() + args.warmup_sec
        while time.time() < until and not stop_event.is_set():
            for cam in cameras:
                cam.read_resilient(time.time())
            time.sleep(1.0 / target_fps)
        cam_report = '  '.join(
            f'{c.label}={_measure_actual_fps(c.cap, warmup=0, sample=10):.1f}fps' if not c.down else f'{c.label}=断联'
            for c in cameras)
        imu_report = '  '.join(f'{d.label}={d.current_hz():.1f}Hz' for d in devices)
        print(f'预热结束: {cam_report}  {imu_report}')

    try:
        segment_no = 0
        while True:
            segment_no += 1
            if args.align_hourly:
                duration_seconds = _seconds_to_next_hour(datetime.now())
            else:
                duration_seconds = args.duration
            if loop_mode:
                if args.align_hourly:
                    end_at = (datetime.now() + timedelta(seconds=duration_seconds)).strftime('%H:%M:%S')
                    print(f'\n════ 第 {segment_no} 段录制开始（本段到 {end_at} 整点结束）════')
                else:
                    print(f'\n════ 第 {segment_no} 段录制开始 ════')
            should_stop = _run_one_segment(args, cameras, devices, target_fps, record_mode,
                                            duration_seconds, pair_filter)
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


def _run_one_segment(args, cameras: list[CameraStream], devices: list[ImuDevice],
                      target_fps: int, record_mode: bool, duration_seconds: float = 0,
                      pair_filter=None) -> bool:
    """录制一段，返回是否应该整体停止（True=用户退出/出错，False=正常到时结束）。
    duration_seconds: 这一段实际要录多久——普通模式下就是 --duration；
    --align-hourly 模式下是"到下一个整点还剩多少秒"（每段都重新算一次，
    所以第一段可能不足一小时，后面每段都是整整一小时）。"""
    should_stop = [False]
    frame_interval = 1.0 / target_fps
    save_overlay = not args.no_save_overlay
    # 事件驱动同步依赖IMU来新样本时唤醒 _new_sample_event；没有任何IMU设备时
    # （纯摄像头预览模式）这个事件永远不会被触发，每个tick都会傻等满
    # frame_interval*3 的超时才继续，把实际fps拖到远低于 --cam-fps 的水平
    # ——这种情况下没有IMU可同步，直接退化成固定定时器抓帧。
    imu_sync = not args.no_imu_sync and bool(devices)

    # 精确到毫秒，避免 --loop 循环录制时文件名撞车互相覆盖。
    now_dt = datetime.now()
    ts_tag = now_dt.strftime('%Y%m%d_%H%M%S%f')[:-3]
    # 按录制开始那天新建一个日期子文件夹（2026_7_18 这种格式，不补零），
    # 方便按天整理/归档，不用每天手动建目录或者在一堆文件里翻日期。
    day_dir = f'{now_dt.year}_{now_dt.month}_{now_dt.day}'
    out_dir = os.path.join(args.out_dir, day_dir)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f'multicam_{ts_tag}')

    csv_file = meta_file = None
    csv_writer = meta_writer = None

    csv_header = ['timestamp']
    meta_header = ['frame_idx', 'timestamp']
    for cam in cameras:
        meta_header.append(f'{cam.label}_fps')
    for cam in cameras:
        meta_header.append(f'{cam.label}_missing')  # 1 = 这一 tick 这路摄像头没拿到真实帧（占位帧）
    for d in devices:
        csv_header += [f'{d.label}_acc_x', f'{d.label}_acc_y', f'{d.label}_acc_z',
                        f'{d.label}_gyro_x', f'{d.label}_gyro_y', f'{d.label}_gyro_z']
        meta_header += [f'{d.label}_imu_timestamp', f'{d.label}_lag_ms', f'{d.label}_missing',
                         f'{d.label}_hz', f'{d.label}_acc_x', f'{d.label}_acc_y', f'{d.label}_acc_z',
                         f'{d.label}_gyro_x', f'{d.label}_gyro_y', f'{d.label}_gyro_z']

    if record_mode:
        use_ffmpeg = shutil.which('ffmpeg') is not None
        for cam in cameras:
            video_path = f'{base}_{cam.label}_raw.mp4'
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
            raw_writer.writerow(RAW_CSV_HEADER)
            d.set_raw_writer(raw_writer)
            d._raw_file = raw_file

        print(f'录制模式: {duration_seconds:.0f}s  组合CSV→{base}.csv  对齐信息→{base}_meta.csv')
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
                # 傻等满这个固定超时，把实际fps拖到远低于目标fps，即使非
                # --imu-sync模式本可以跑到目标fps。
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

            # 某路摄像头读失败不再整体退出：掉线的那一路写占位帧、后台重连，
            # 其它摄像头和 IMU 照常录（见 CameraStream.read_resilient）
            frames = []
            cam_missing = []
            read_failed = False
            for cam in cameras:
                frame, ok = cam.read_resilient(tick_ts)
                frames.append(frame)
                cam_missing.append(0 if ok else 1)

            tick_ts_str = datetime.fromtimestamp(tick_ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            cam_fps_list = [cam.fps_tick(tick_ts) if ok == 0 else 0.0 for cam, ok in zip(cameras, cam_missing)]

            csv_row = [tick_ts_str]
            meta_row = [frame_idx, tick_ts_str] + [f'{fps:.1f}' for fps in cam_fps_list] + cam_missing
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
                imu_info.append((d, hz, lag_ms, missing, imu_row))

            if csv_writer:
                csv_writer.writerow(csv_row)
            if meta_writer:
                meta_writer.writerow(meta_row)

            down_cams = [f'{c.label} DOWN {_fmt_duration(c.down_seconds(tick_ts))}' for c in cameras if c.down]
            for cam, frame, cam_fps in zip(cameras, frames, cam_fps_list):
                display = draw_overlay(frame.copy(), cam.label, cam_fps, target_fps, imu_info, elapsed, frame_idx,
                                        show_imu_values=args.show_imu_values,
                                        show_frame_info=args.show_frame_info,
                                        down_cams=down_cams if not cam.down else None,
                                        alpha=args.overlay_alpha)
                if cam.video_writer:
                    cam.video_writer.write(display if save_overlay else frame)
                try:
                    cv2.imshow(f'IMU(multicam) {cam.label}', display)
                except cv2.error:
                    if not record_mode:
                        print('cv2.imshow 不支持（可能是 headless 版本）。')
                        read_failed = True
                        should_stop[0] = True

            if read_failed:
                break

            if record_mode and elapsed >= duration_seconds:
                print(f'\n已达到录制时长 {duration_seconds:.0f}s，停止。')
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
        for cam in cameras:
            if cam.dropped_ticks:
                state = '仍未连上' if cam.down else '已恢复'
                print(f'  [{cam.label}] 本段有 {cam.dropped_ticks} 个 tick 没拿到真实帧（占位帧顶替，{state}）')
            cam.dropped_ticks = 0
        # 设备从头到尾一条数据都没来（没连上/一开始就断了），它的 raw.csv 就只有
        # 一行表头。这种设备下面不要再生成配对文件——配对文件是最终要传上 NAS、
        # 进标注平台的东西，一个只有表头的 CSV 到了那边就是个打开就报错、算不出
        # 任何指标的空样本，还得有人回过头来一个个删。宁可这一路没有文件，
        # 也不要一个看起来正常、其实是空的文件。
        dead_devices = set()
        if record_mode:
            for d in devices:
                if not _csv_has_data_rows(f'{base}_{d.label}_raw.csv'):
                    dead_devices.add(d.label)
            if dead_devices:
                print()
                print(f'!! 警告: {"、".join(sorted(dead_devices))} 整段没有收到任何数据'
                      f'（raw.csv 只有表头）——不会为它生成配对文件。')
                print('!! 检查一下设备有没有连上、是不是没电了，这一段这几路的数据是丢了的。')

        if record_mode:
            print(f'已保存: {base}.csv  {base}_meta.csv')
            for cam in cameras:
                print(f'       {base}_{cam.label}_raw.mp4')
            for d in devices:
                print(f'       {base}_{d.label}_raw.csv')
            print()
            print('── 自动对齐校验（各摄像头帧数 vs 组合CSV行数）──')
            # 每个 tick 都同时读了所有摄像头一帧，所以每路摄像头的视频帧数理论上
            # 应该都严格等于 frame_idx（组合 CSV 的行数）；check_alignment.py 假设
            # 视频和 CSV 同名成对，这里视频是 {base}_{label}.mp4、CSV 是共享的
            # {base}.csv，不满足它的命名假设，所以直接读帧数自己比对，不复用它。
            for cam in cameras:
                video_path = f'{base}_{cam.label}_raw.mp4'
                cap_check = cv2.VideoCapture(video_path)
                actual_frames = int(cap_check.get(cv2.CAP_PROP_FRAME_COUNT))
                cap_check.release()
                if actual_frames == frame_idx:
                    print(f'  [{cam.label}] ✔ 帧数与组合CSV行数一致: {actual_frames}')
                else:
                    print(f'  [{cam.label}] ✘ 帧数不一致: 视频 {actual_frames} 帧, CSV {frame_idx} 行')

            print()
            resampled_pairs = []  # (cam_label, device_label, resampled_base)
            if args.no_resample:
                # 不降采样，但原始数据也按 cam x imu 两两配对复制一份（内容还是
                # 原始频率，不经过 resample_raw_imu），方便直接拖拽上传标注；
                # 配对之外，单独的 {base}_camN_raw.mp4/{base}_imuM_raw.csv 原始
                # 文件也保留，两种都留，不删。
                #
                # CSV这一份用 write_anchored_raw_csv() 而不是直接复制：对齐到
                # first_tick_ts_ms/last_tick_ts_ms（视频第一帧/最后一帧的真实
                # 时间戳），不改动/不插值任何真实数值。除了开头（IMU连接延迟
                # 导致第一条真实数据比视频晚几百毫秒到1秒）、结尾（裁掉视频
                # 结束之后的部分）这两处边界，中间只要检测到相邻两条真实样本
                # 间隔超过1秒（信号断联，比如设备被压住），也会在缺口两头各
                # 插一行数据。这些"没有真实信号"的行统一用6轴全0填充（而不是
                # 留空）：6轴同时全为0在真实IMU数据里不可能出现（重力会让加速
                # 度至少有读数），既能让Label Studio图表上显示为一段贴着0的
                # 平线、不会被误当成真实变化去标注，也方便下游训练代码用一条
                # "全0→判定缺失，跳过"的简单规则识别，无需处理空值/NaN。
                print('── --no-resample：不降采样，原始数据按 cam x imu 配对（原始文件也保留，时间轴已对齐视频起止）──')
                for d in devices:
                    if d.label in dead_devices:
                        print(f'  跳过 {d.label}：整段没有数据，不生成配对文件')
                        continue
                    for cam in _pairs_for(cameras, d, pair_filter):
                        pair_base = f'{base}_{cam.label}_{d.label}_raw'
                        try:
                            shutil.copyfile(f'{base}_{cam.label}_raw.mp4', f'{pair_base}.mp4')
                            write_anchored_raw_csv(
                                f'{base}_{d.label}_raw.csv', f'{pair_base}.csv',
                                t_start_ms=first_tick_ts_ms, t_end_ms=last_tick_ts_ms,
                            )
                            print(f'  {pair_base}.mp4 / .csv（{cam.label} 原始视频 + {d.label} 原始数据，'
                                  f'文件名一致可直接拖拽配对）')
                        except OSError as e:
                            print(f'生成 {pair_base} 配对文件失败: {e}')
            else:
                print('── 降采样（每路摄像头 x 每个设备各生成一对同名 mp4/csv）──')
                for d in devices:
                    if not cameras:
                        continue
                    if d.label in dead_devices:
                        print(f'  跳过 {d.label}：整段没有数据，不生成配对文件')
                        continue
                    # 每个设备只需要算一次降采样，但要让每一对 mp4/csv 文件名（去掉
                    # 扩展名）完全一致才能直接拖进 Label Studio 配对，所以第一路摄像头
                    # 直接把降采样结果写到配对文件名下，其余摄像头再从这份结果复制过去
                    # （内容完全相同，只是复制成不同文件名，方便按文件名对拖拽上传）。
                    want = _pairs_for(cameras, d, pair_filter)
                    if not want:
                        continue
                    first_pair_base = f'{base}_{want[0].label}_{d.label}_resampled{args.resample_hz:g}hz'
                    resample_raw_imu(
                        f'{base}_{d.label}_raw.csv', f'{first_pair_base}.csv', args.resample_hz,
                        t_start_ms=first_tick_ts_ms, t_end_ms=last_tick_ts_ms,
                    )
                    for cam in want:
                        pair_base = f'{base}_{cam.label}_{d.label}_resampled{args.resample_hz:g}hz'
                        try:
                            shutil.copyfile(f'{base}_{cam.label}_raw.mp4', f'{pair_base}.mp4')
                            if cam is not want[0]:
                                shutil.copyfile(f'{first_pair_base}.csv', f'{pair_base}.csv')
                            print(f'  {pair_base}.mp4 / .csv（{cam.label} 视频 + {d.label} 降采样数据，'
                                  f'文件名一致可直接拖拽配对）')
                            resampled_pairs.append((cam.label, d.label, pair_base))
                        except OSError as e:
                            print(f'生成 {pair_base} 配对文件失败: {e}')

            if args.resample_only and not devices:
                # 没有配置任何IMU设备（纯视频录制模式）时不会生成任何
                # resampled配对文件——这里如果照常删除原始 {base}_camX.mp4/
                # .csv，就是把唯一的视频/数据删掉、什么都不剩，所以这种情况
                # 下 --resample-only 直接忽略，原始文件原样保留。
                print(f'\n--resample-only: 没有配置IMU设备，没有resampled文件可替代，'
                      f'已忽略 --resample-only，原始文件保留。')
            elif args.resample_only:
                for cam in cameras:
                    try:
                        os.remove(f'{base}_{cam.label}_raw.mp4')
                    except OSError as e:
                        print(f'删除 {base}_{cam.label}_raw.mp4 失败: {e}')
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


def run_probe(args, cam_indices: list[int], devices: list[ImuDevice], probe_seconds: float = 5.0):
    """探测每路摄像头能力 + 短暂连接所有 IMU 设备测量各自实际输出频率，不录制。"""
    for i, cam_idx in enumerate(cam_indices, start=1):
        print(f'── cam{i} (摄像头 {cam_idx}) ──')
        probe_camera(cam_idx, backend=args.backend, fourcc=args.fourcc)

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
    ap = argparse.ArgumentParser(description='多个摄像头 + 多个 IMU 设备同步采集')
    ap.add_argument('--camera', action='append', type=int, required=True,
                    help='摄像头编号，可重复传多个，例如 --camera 0 --camera 1')
    ap.add_argument('--imu', action='append', default=[],
                    help='IMU 设备，格式 类型=标识，可重复传多个。见 imu_camera_sync_multi.py 说明。'
                         '不传就是纯摄像头预览模式，不连IMU（组合CSV里就不会有对应的acc/gyro列）。')
    ap.add_argument('--dog-name', action='append', default=[],
                    help='画面叠加信息里显示的名字，按 --imu 出现的顺序一一对应（第几个 --dog-name '
                         '对应第几个 --imu），比如 --imu wit=WT1 --imu wit=WT4 --dog-name bibi '
                         '--dog-name titi 就是 WT1 显示成 bibi、WT4 显示成 titi。只影响画面上的'
                         '显示名字，不影响文件名/CSV列名（那些还是用 imu1/imu2 这种固定编号）。'
                         '不传就还是显示 imu1/imu2...；传的数量可以比 --imu 少，没对应到的设备'
                         '照常显示 imuN。')
    ap.add_argument('--width', type=int, default=1280, help='最终输出/写入视频的分辨率宽，默认 1280（720p）')
    ap.add_argument('--height', type=int, default=720, help='最终输出/写入视频的分辨率高，默认 720（720p）')
    ap.add_argument('--capture-width', type=int, default=0,
                    help='向摄像头请求的原生采集分辨率宽，默认0=跟--width一样。广角摄像头直接请求'
                         '较低分辨率时驱动常给裁切画面而非等比缩小，想保住完整广角视野就填原生高'
                         '分辨率（比如2K是2560），配合--capture-height，脚本采集后会用软件缩放到'
                         '--width/--height 输出。')
    ap.add_argument('--capture-height', type=int, default=0,
                    help='向摄像头请求的原生采集分辨率高，默认0=跟--height一样。见 --capture-width。')
    ap.add_argument('--backend', choices=['auto', 'dshow', 'msmf', 'any'], default='auto',
                    help='OpenCV 打开摄像头用的后端，默认 auto（Windows上自动用MSMF），所有摄像头统一'
                         '用这一个设置。MSMF 在实测中1080p/720p的真实fps比DSHOW准确得多（DSHOW有的'
                         '分辨率下请求的30/60fps实际只能跑5~10fps）；如果某台摄像头在MSMF下自动对焦/'
                         '白平衡等UVC控制属性不生效，改用 --backend dshow 单独调一次（--show-settings'
                         '-dialog 也只支持dshow），调好之后驱动通常会记住，换回默认MSMF往往还生效。')
    ap.add_argument('--fourcc', default='MJPG',
                    help='摄像头像素格式，默认 MJPG（高分辨率下大多数USB2.0摄像头只支持MJPG，'
                         '不显式指定可能导致OpenCV协商到裁切/不完整画面的格式）')
    ap.add_argument('--autofocus', choices=['on', 'off'], default='on', help='是否开启自动对焦（默认on）')
    ap.add_argument('--auto-wb', choices=['on', 'off'], default='on', help='是否开启自动白平衡（默认on）')
    ap.add_argument('--show-settings-dialog', action='store_true',
                    help='打开摄像头驱动原生的属性设置对话框（仅Windows+dshow有效，会给每一路摄像头'
                         '各弹一次，模态对话框会卡住直到关掉），效果等同于Windows相机App里调白平衡/'
                         '曝光/对焦——部分摄像头驱动不支持OpenCV转发这些控制属性时用这个手动调')
    ap.add_argument('--cam-fps', type=int, default=20, help='摄像头目标帧率，默认 20')
    ap.add_argument('--duration', type=float, default=0, help='录制时长（秒），0=实时模式不保存')
    ap.add_argument('--warmup-sec', type=float, default=5.0, help='预热时长（秒），默认 5，设 0 关闭')
    ap.add_argument('--video-crf', type=int, default=28, help='H.264 CRF，默认 28')
    ap.add_argument('--out-dir', default='data', help='输出目录，默认 data/')
    ap.add_argument('--scan-timeout', type=float, default=8.0, help='BLE 扫描超时（秒），默认 8')
    ap.add_argument('--reconnect-max-backoff', type=float, default=300.0,
                    help='设备一直连不上时，重连间隔按2→4→8→...指数退避的封顶秒数，默认300秒'
                         '（5分钟）。长时间信号差不会让重试间隔无限缩短，避免频繁反复扫描把'
                         'Windows蓝牙栈拖垮（症状：整个蓝牙适配器搜不到任何设备，得重启电脑才能'
                         '恢复）；长时间无人值守录制（比如整晚8小时以上）建议保持默认或调更高。')
    ap.add_argument('--no-save-overlay', action='store_true', help='保存干净视频（不含叠加信息）')
    ap.add_argument('--no-imu-sync', action='store_true', help='关闭事件驱动同步，改用固定定时器抓帧')
    ap.add_argument('--overlay-alpha', type=float, default=0.45,
                    help='画面上常态叠加信息（时间/机位/各设备Hz）的不透明度，0~1，默认 0.45。'
                         '设 1 就是完全不透明。MISSING 和摄像头掉线的告警不受这个影响，始终不透明')
    ap.add_argument('--show-imu-values', action='store_true',
                    help='画面上显示每个IMU设备的实时6轴数值（加速度+角速度），默认不显示（比较占'
                         '画面）；只在需要肉眼确认设备有没有戴好、是不是在动的时候临时打开。')
    ap.add_argument('--show-frame-info', action='store_true',
                    help='画面上显示同步tick序号(#frame_idx)和这一段已录时长(t=...s)，默认不显示'
                         '（平时看画面用不上）；排查丢帧/对齐问题时临时打开。')
    ap.add_argument('--resample-hz', type=float, default=25.0,
                    help='录制结束后把每个设备的原始IMU流水降采样到该频率，默认25Hz')
    ap.add_argument('--resample-only', action='store_true',
                    help='只保留各摄像头x设备的降采样版文件，删除原始的 {base}_camN.mp4/.csv/_meta.csv/_raw.csv')
    ap.add_argument('--no-resample', action='store_true',
                    help='跟 --resample-only 相反：完全跳过降采样这一步，不生成任何 '
                         '{base}_camX_imuY_resampled{HZ}hz.mp4/.csv 配对文件，只保留原始的 '
                         '{base}_camN.mp4/.csv/_meta.csv/_{imu}_raw.csv（想留原始IMU数据、不需要'
                         '降采样配对文件时用这个）。跟 --resample-only 互斥。')
    ap.add_argument('--loop', action='store_true',
                    help='循环录制模式：每段 --duration 秒，录完自动开始下一段，直到按 Q/ESC 或 Ctrl+C 才停止')
    ap.add_argument('--align-hourly', action='store_true',
                    help='按整点对齐分段，不用 --duration 固定秒数：不管什么时候开始录制，第一段只录到'
                         '下一个整点为止（比如11:23开始，第一段到12:00:00结束），之后每段整整一小时'
                         '（12:00→13:00→14:00...），配合 --loop 就能一直按小时切文件。跟 --duration 是'
                         '二选一：加了这个参数 --duration 会被忽略；不加这个参数，--duration 的固定秒数'
                         '用法完全不受影响。')
    ap.add_argument('--imu-label', action='append', default=[], metavar='imuN',
                    help='每个设备在文件名/列名里用的编号，顺序跟 --imu 一一对应，'
                         '例如 --imu-label imu9 --imu-label imu11。不传就按位置排 imu1、imu2…'
                         '\n'
                         '为什么需要：位置序号是每个场地各自从 1 开始的，两个场地的文件名会撞。'
                         '狗场的第一个设备叫 imu1，影棚的第一个也叫 imu1，而平台那边是靠文件名里的'
                         'imu 号去认是哪只狗的（狗档案登记的是全局唯一的 IMU1..IMU20）——撞了就会把'
                         '一个场地的数据算到另一个场地的狗身上，皮肤评估那张按（日期,imu,来源）'
                         '唯一的表还会直接撞行写不进去。')
    ap.add_argument('--pair', action='append', default=[], metavar='camN:imuM',
                    help='只生成这些 cam x imu 配对，可重复传，例如 --pair cam1:imu1 --pair cam2:imu2。'
                         '不传就按老规矩全排列。'
                         '狗场那种「一间一狗一摄像头」的场地必须用它：cam_i 和 imu_i 是严格一一对应，'
                         '全排列出来 30/36 都是「A 房间的画面配 B 房间的狗」，纯废文件，还成倍占磁盘。'
                         '影棚那种一个大空间多只狗的场地不要传：哪路摄像头拍到哪只狗事先不知道，'
                         '全排列是有意义的')
    ap.add_argument('--no-precheck', action='store_true',
                    help='跳过开录前的设备预检。预检是为了避免"参数写错→录一整天空 CSV"，'
                         '只有确认设备稍后才会上线之类的特殊情况才该关掉')
    ap.add_argument('--probe', action='store_true',
                    help='只探测硬件能力（每路摄像头 + 各IMU设备当前实际输出频率），不录制，探测完直接退出')
    args = ap.parse_args()

    # 日志留存：终端照常打印，同时逐行带时间戳写进 logs/。录制是连着好几天跑的，
    # BLE 断连这类问题必须能回头翻几小时前发生了什么（见 log_setup.py）
    log_path = log_setup.setup(prefix='record_multicam')
    print(f'[日志] 本次输出同时写入: {log_path}')


    if args.resample_only and args.no_resample:
        print('--resample-only 和 --no-resample 互斥（一个是"只留降采样版"，一个是"只留原始版"），只能选一个。')
        sys.exit(1)

    devices = []
    for i, spec in enumerate(args.imu, start=1):
        try:
            dev_type, ident = parse_imu_spec(spec)
        except ValueError as e:
            print(e)
            sys.exit(1)
        dog_name = args.dog_name[i - 1] if i - 1 < len(args.dog_name) else None
        label = args.imu_label[i - 1] if i - 1 < len(args.imu_label) else f'imu{i}'
        if not re.fullmatch(r'imu\d+', label):
            print(f'--imu-label 应该长这样：imu9，收到: {label!r}')
            sys.exit(1)
        devices.append(ImuDevice(dev_type, ident, label=label, display_name=dog_name))

    if args.imu_label and len(args.imu_label) != len(args.imu):
        # 只给一半更危险：没给的那些退回位置序号，一份录制里混着两套编号体系，
        # 事后根本看不出哪个 imu3 是哪个意思
        print(f'--imu-label 给了 {len(args.imu_label)} 个，但有 {len(args.imu)} 个设备——'
              f'要么全给，要么一个都不给')
        sys.exit(1)
    dup = [x for x in {d.label for d in devices} if [d.label for d in devices].count(x) > 1]
    if dup:
        print(f'设备编号重复: {", ".join(sorted(dup))}')
        sys.exit(1)

    if args.probe:
        run_probe(args, args.camera, devices)
        return

    # --pair 解析成 {(cam标签, imu标签)} 的集合；空集合 = 不限制、全排列
    pair_filter = set()
    for spec in args.pair:
        if ':' not in spec:
            print(f'--pair 格式应为 camN:imuM，收到: {spec!r}')
            sys.exit(1)
        c, i = spec.split(':', 1)
        pair_filter.add((c.strip().lower(), i.strip().lower()))
    if pair_filter:
        # 写错了要立刻报，别等录完一小时才发现一个配对文件都没生成
        cam_labels = {f'cam{n}' for n in range(1, len(args.camera) + 1)}
        imu_labels = {f'imu{n}' for n in range(1, len(args.imu) + 1)}
        bad = [f'{c}:{i}' for c, i in sorted(pair_filter)
               if c not in cam_labels or i not in imu_labels]
        if bad:
            print(f'--pair 里这些配对不存在: {", ".join(bad)}')
            print(f'  可用摄像头: {", ".join(sorted(cam_labels))}')
            print(f'  可用设备:   {", ".join(sorted(imu_labels))}')
            sys.exit(1)

    # 设备没到齐就别开录：录一整天出来 IMU 全是空 CSV，那一天补不回来。
    # 只在开录前拦一次，录起来之后掉线还是照常自动重连（狗跑远了要能恢复）。
    #
    # 放在打开摄像头**之前**：一是设备不齐就不用白白初始化三路 1080p；二是
    # OpenCV 打开摄像头会把主线程初始化成 Windows GUI(STA)，而 bleak 的 WinRT
    # 后端要求 MTA，之后再扫描会直接炸（Thread is configured for Windows GUI
    # but callbacks are not working）。precheck_devices 内部另起线程也躲开了
    # 这个问题，这里再顺手把顺序也摆对。
    if devices and not args.no_precheck:
        problems = precheck_devices(devices, scan_timeout=max(args.scan_timeout, 12.0))
        if problems:
            print('\n没有开始录制。改对 --imu 参数再来，或者加 --no-precheck 强行开录。')
            sys.exit(1)

    autofocus = {'on': True, 'off': False}.get(args.autofocus)
    auto_wb = {'on': True, 'off': False}.get(args.auto_wb)
    cameras = []
    for i, cam_idx in enumerate(args.camera, start=1):
        try:
            cameras.append(CameraStream(cam_idx, f'cam{i}', args.width, args.height, args.cam_fps,
                                         backend=args.backend, fourcc=args.fourcc,
                                         autofocus=autofocus, auto_wb=auto_wb,
                                         capture_width=args.capture_width, capture_height=args.capture_height,
                                         show_settings_dialog=args.show_settings_dialog))
        except RuntimeError as e:
            print(e)
            sys.exit(1)

    t = None
    if devices:
        # 没有 IMU 设备时（纯摄像头预览模式）不启动这个线程——它的
        # run_all() 在设备列表为空时会立刻 gather() 完，finally 里的
        # stop_event.set() 会跟着马上触发，导致摄像头主循环还没真正开始
        # 就被当成"该停止了"，一tick都抓不到就退出。
        t = threading.Thread(target=ble_thread_main,
                             args=(devices, args.scan_timeout, args.reconnect_max_backoff), daemon=True)
        t.start()
        print('等待 BLE 连接中...')
        time.sleep(2.0)

    run_cameras(args, cameras, devices, pair_filter)

    stop_event.set()
    if t is not None:
        t.join(timeout=3.0)


if __name__ == '__main__':
    main()
