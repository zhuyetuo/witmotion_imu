# -*- coding: utf-8 -*-
"""
WT901SDCL-BT50 BLE 实时IMU数据接收脚本
=========================================

功能:
    通过 BLE (Bluetooth Low Energy) 直接扫描并连接维特智能(WitMotion)
    WT901SDCL-BT50 传感器，订阅其数据特征值(Notify)，实时接收 0x55 0x61
    数据包（加速度+角速度+角度+芯片时间戳），边接收边实时写入
    Label Studio 可识别的 CSV 文件：
        timestamp, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z

    使用 BLE GATT 协议直连，不需要先在 Windows "蓝牙和其他设备" 设置里
    手动配对——Windows 10/11 自带的 BLE 协议栈(WinRT)允许程序直接扫描、
    连接、订阅特征值，跳过传统经典蓝牙(SPP)那种必须先配对生成COM口的流程。

依赖:
    pip install bleak --break-system-packages
    (bleak 是跨平台 BLE 库，Windows 下基于系统自带的 WinRT Bluetooth API，
     无需额外安装驱动；只要电脑本身支持 BLE 5.0 或插了 BLE 适配器即可)

数据包格式、acc/gyro/angle 换算公式、芯片时间戳解析、Label Studio 时间
格式要求等，均与同目录下的 parse_wit.py（离线文件解析脚本）完全一致，
本脚本直接 import 复用其中的解析与格式化函数，避免两份代码各写一套、
互相不一致。

WitMotion BLE 模组的 GATT UUID（经典款 WT901BLECL 已验证，WT901SDCL-BT50
作为同系列新款大概率沿用相同协议；如果连接后发现下面给的默认UUID订阅不到
数据，脚本会先打印出设备实际暴露的所有服务/特征值，照着实际输出改
--notify-uuid 参数即可，不需要改代码）:
    Notify 特征值 UUID: 0000ffe4-0000-1000-8000-00805f9a34fb  (推荐先试这个)
    备用/写入特征值 UUID: 0000ffe5-0000-1000-8000-00805f9a34fb
    备用 Notify: 0000ffe1-0000-1000-8000-00805f9a34fb (部分批次/固件)

用法:
    # 第一步：先扫描，确认能看到设备、记下设备名称或MAC地址
    python wit_ble_live.py --scan

    # 按名称关键字自动查找并连接（推荐，不用记MAC地址）
    python wit_ble_live.py --name WT901 -o live_labelstudio.csv

    # 只在终端实时打印数据，不创建/写入任何文件
    python wit_ble_live.py --name WT901 --print-only

    # 也可以用MAC地址直连（Windows上bleak的地址就是标准MAC格式）
    python wit_ble_live.py --address AA:BB:CC:DD:EE:FF -o live_labelstudio.csv

    # 如果默认UUID订阅不到数据，先列出该设备真实的服务/特征值：
    python wit_ble_live.py --name WT901 --list-services

    # 按 Ctrl+C 停止采集，已写入的CSV文件会被正常关闭保存。
"""

import argparse
import asyncio
import struct
import sys
import time
from datetime import datetime, timedelta

from bleak import BleakClient, BleakScanner

# 复用 parse_wit.py 里的协议解析/格式化逻辑，保证实时流和离线文件解析出来的
# 数值、时间格式完全一致。
from parse_wit import (
    ACC_RANGE,
    ANGLE_RANGE,
    GYRO_RANGE,
    LABELSTUDIO_HEADER,
    fmt_chip_time_dotms,
    fmt_num,
)

PACKET_LEN = 28
HEADER = 0x55
TYPE_61 = 0x61

# 常见的 WitMotion BLE 特征值 UUID（小写，bleak 返回的 UUID 也是小写）。
# 实测 WT901BLECL 系列用 FFE5 做读写、FFE4 做 notify 推送数据；不同固件/
# 批次可能有差异，所以提供 --list-services 方便现场核对。
DEFAULT_NOTIFY_CANDIDATES = [
    '0000ffe4-0000-1000-8000-00805f9a34fb',
    '0000ffe1-0000-1000-8000-00805f9a34fb',
    '0000ffe5-0000-1000-8000-00805f9a34fb',
]


def parse_one_packet(pkt: bytes):
    """解析单个28字节 0x55 0x61 数据包，返回字典（结构与 parse_wit.parse_packets 一致）。"""
    if len(pkt) != PACKET_LEN or pkt[0] != HEADER or pkt[1] != TYPE_61:
        return None
    vals = struct.unpack('<9h', pkt[2:20])
    acc = [v / 32768.0 * ACC_RANGE for v in vals[0:3]]
    gyro = [v / 32768.0 * GYRO_RANGE for v in vals[3:6]]
    angle = [v / 32768.0 * ANGLE_RANGE for v in vals[6:9]]
    yy, mm, dd, hh, mi, ss = pkt[20:26]
    ms = struct.unpack('<H', pkt[26:28])[0]
    try:
        chip_time = datetime(2000 + yy, mm, dd, hh, mi, ss) + timedelta(milliseconds=ms)
    except ValueError:
        chip_time = None
    return {
        'acc': acc,
        'gyro': gyro,
        'angle': angle,
        'year': 2000 + yy,
        'month': mm,
        'day': dd,
        'hour': hh,
        'minute': mi,
        'second': ss,
        'ms': ms,
        'chip_time': chip_time,
    }


class StreamingByteBuffer:
    """
    BLE notify 推送的数据不一定每次都恰好是28字节一个完整包，可能会被
    底层分片成更小的片段，也可能一次回调里带了好几个包。这里维护一个
    累积缓冲区，按 0x55 0x61 同步头切出完整的28字节包。
    """

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        """喂入新收到的字节，返回这次能切出的所有完整数据包（可能为空列表）。"""
        self.buf.extend(data)
        packets = []
        i = 0
        n = len(self.buf)
        while i + PACKET_LEN <= n:
            if self.buf[i] == HEADER and self.buf[i + 1] == TYPE_61:
                packets.append(bytes(self.buf[i:i + PACKET_LEN]))
                i += PACKET_LEN
            else:
                i += 1
        # 保留缓冲区里还不够拼成一个完整包的尾部字节，等下次 feed 时续上
        del self.buf[:i]
        return packets


class LiveCsvWriter:
    """
    边收数据边写 Label Studio 格式 CSV 的增量写入器。

    复刻 parse_wit.py 里 fix_nonmonotonic_packets() 的坏帧过滤逻辑：
    只有时间戳严格大于"上一个已写入帧"的时间戳，才会被写入文件；
    否则视为蓝牙重连导致的时钟复位/重传坏帧，直接丢弃并打印提示。
    这样实时采集中途如果发生短暂断连重连，导出的CSV依然保证时间戳
    严格单调递增，可以直接用于 Label Studio。
    """

    def __init__(self, path, encoding='utf-8', keep_bad_frames=False):
        import csv
        self._f = open(path, 'w', encoding=encoding, newline='')
        self._writer = csv.writer(self._f)
        self._writer.writerow(LABELSTUDIO_HEADER)
        self._f.flush()
        self.keep_bad_frames = keep_bad_frames
        self.last_good_time = None
        self.count_written = 0
        self.count_dropped = 0

    def write_packet(self, p):
        t = p['chip_time']
        if not self.keep_bad_frames:
            if t is None:
                self.count_dropped += 1
                return False
            if self.last_good_time is not None and t <= self.last_good_time:
                self.count_dropped += 1
                print(f'  [丢弃坏帧] 时间戳非单调递增: {fmt_chip_time_dotms(p)}')
                return False
        ts = fmt_chip_time_dotms(p)
        row = [
            ts,
            fmt_num(p['acc'][0]), fmt_num(p['acc'][1]), fmt_num(p['acc'][2]),
            fmt_num(p['gyro'][0]), fmt_num(p['gyro'][1]), fmt_num(p['gyro'][2]),
        ]
        self._writer.writerow(row)
        self._f.flush()  # 实时落盘，避免程序中途被打断丢数据
        if t is not None:
            self.last_good_time = t
        self.count_written += 1
        return True

    def close(self):
        self._f.close()


async def scan_devices(timeout=6.0):
    print(f'扫描 BLE 设备中（{timeout:.0f} 秒）...')
    devices = await BleakScanner.discover(timeout=timeout)
    if not devices:
        print('未发现任何 BLE 设备。请确认: 1) 传感器已开机且未被其它程序占用连接；'
              '2) Windows 蓝牙已打开；3) 电脑支持 BLE（蓝牙5.0或BLE适配器）。')
        return
    print(f'发现 {len(devices)} 个设备:')
    for d in devices:
        name = d.name or '(无名称)'
        print(f'  - {name:<30s}  地址: {d.address}')


async def list_services(client: BleakClient):
    print('该设备的 GATT 服务/特征值列表:')
    for service in client.services:
        print(f'  服务 {service.uuid}')
        for ch in service.characteristics:
            props = ','.join(ch.properties)
            print(f'      特征值 {ch.uuid}  属性=[{props}]  handle={ch.handle}')


async def find_device(name_filter, address, timeout=8.0):
    if address:
        print(f'按地址查找设备: {address}')
        dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if dev is None:
            print(f'未找到地址为 {address} 的设备，请确认设备已开机、在范围内。')
        return dev

    print(f'扫描中，查找名称包含 "{name_filter}" 的设备（最多等待 {timeout:.0f} 秒）...')
    found = {}

    def _detection_callback(device, adv_data):
        found[device.address] = device

    scanner = BleakScanner(detection_callback=_detection_callback)
    await scanner.start()
    deadline = time.time() + timeout
    target = None
    while time.time() < deadline:
        await asyncio.sleep(0.3)
        for addr, dev in found.items():
            if dev.name and name_filter.lower() in dev.name.lower():
                target = dev
                break
        if target:
            break
    await scanner.stop()

    if target is None:
        print(f'未找到名称包含 "{name_filter}" 的设备。已发现的设备:')
        for addr, dev in found.items():
            print(f'  - {dev.name or "(无名称)"}  地址: {addr}')
    return target


async def run_calibrate(args):
    """
    时间漂移评估模式。
    前提：已用官方上位机软件对设备做过时间校准，设备芯片时间已准确。
    连接后采集 --cal-duration 秒的数据，逐帧对比 PC 系统时间与片上时间戳，
    统计平均偏移（BLE传输延迟）和漂移率（ms/min，反映晶振精度）。
    """
    device = await find_device(args.name, args.address, timeout=args.scan_timeout)
    if device is None:
        sys.exit(1)

    print(f'连接中: {device.name or "(无名称)"}  {device.address}')

    buf = StreamingByteBuffer()
    samples: list[tuple[float, datetime]] = []  # (pc_recv_time_epoch_ms, chip_datetime)
    duration = args.cal_duration

    def notification_handler(sender, data: bytearray):
        pc_epoch_ms = time.time() * 1000.0
        packets = buf.feed(bytes(data))
        for pkt in packets:
            p = parse_one_packet(pkt)
            if p is None or p['chip_time'] is None:
                continue
            chip_dt = p['chip_time']          # naive datetime（设备本地时间）
            samples.append((pc_epoch_ms, chip_dt))
            # 用 PC 当前本地时间与片上时间对比（两者同属本地时间域）
            pc_local_dt = datetime.now()
            offset_ms = (pc_local_dt - chip_dt).total_seconds() * 1000.0
            elapsed_s = (pc_epoch_ms - samples[0][0]) / 1000.0 if len(samples) > 1 else 0.0
            chip_str = chip_dt.strftime('%H:%M:%S.') + f'{p["ms"]:03d}'
            pc_str   = pc_local_dt.strftime('%H:%M:%S.') + f'{int(pc_local_dt.microsecond/1000):03d}'
            print(f'  [{len(samples):>4d}] PC={pc_str}  片上={chip_str}  '
                  f'PC-chip={offset_ms:+.1f}ms  elapsed={elapsed_s:.1f}s')

    async with BleakClient(device) as client:
        print('已连接。')

        notify_uuid = args.notify_uuid
        candidates = [notify_uuid] if notify_uuid else DEFAULT_NOTIFY_CANDIDATES
        subscribed = None
        for uuid in candidates:
            try:
                await client.start_notify(uuid, notification_handler)
                subscribed = uuid
                break
            except Exception as e:
                print(f'  尝试订阅 {uuid} 失败: {e}')

        if subscribed is None:
            print('订阅失败，请用 --list-services 核实 UUID。')
            return

        print(f'已订阅 {subscribed}，开始采集（{duration:.0f} 秒）...\n')
        try:
            await asyncio.sleep(duration)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            try:
                await client.stop_notify(subscribed)
            except Exception:
                pass

    if len(samples) < 2:
        print('帧数太少，无法统计。')
        return

    pc_epoch0, chip_dt0 = samples[0]
    pc_epoch1, chip_dt1 = samples[-1]
    total_pc_s   = (pc_epoch1 - pc_epoch0) / 1000.0
    total_chip_s = (chip_dt1 - chip_dt0).total_seconds()

    offsets_ms = [
        (datetime.fromtimestamp(pc_ms / 1000.0) - chip_dt).total_seconds() * 1000.0
        for pc_ms, chip_dt in samples
    ]
    avg_offset  = sum(offsets_ms) / len(offsets_ms)
    drift_ms_per_min = (offsets_ms[-1] - offsets_ms[0]) / total_pc_s * 60.0 if total_pc_s > 0 else 0.0
    drift_ppm = (total_pc_s - total_chip_s) / total_pc_s * 1_000_000 if total_pc_s > 0 else 0.0

    print(f'\n── 漂移评估结果（{len(samples)} 帧，{total_pc_s:.1f} 秒）──')
    print(f'  平均偏移 (PC-chip):  {avg_offset:+.1f} ms  ← BLE 传输延迟')
    print(f'  偏移范围:            {min(offsets_ms):+.1f} ~ {max(offsets_ms):+.1f} ms')
    print(f'  漂移率:              {drift_ms_per_min:+.2f} ms/min  ({drift_ppm:+.1f} ppm)')
    print(f'  推算 1分钟误差:      约 {abs(drift_ms_per_min):.1f} ms')
    print(f'  推算 1小时误差:      约 {abs(drift_ms_per_min)*60/1000:.1f} 秒')
    print(f'  推算 1天误差:        约 {abs(drift_ms_per_min)*1440/1000:.1f} 秒')
    if abs(drift_ms_per_min) < 1.0:
        print('  ✓ 晶振精度良好（<1 ms/min）')
    elif abs(drift_ms_per_min) < 10.0:
        print('  ⚠ 晶振有轻微漂移，长时采集建议重新校时')
    else:
        print('  ✗ 漂移较严重，建议检查晶振或重新用上位机校时')


async def run(args):
    if args.calibrate:
        await run_calibrate(args)
        return

    if args.scan:
        await scan_devices(timeout=args.scan_timeout)
        return

    device = await find_device(args.name, args.address, timeout=args.scan_timeout)
    if device is None:
        sys.exit(1)

    print(f'尝试连接: {device.name or "(无名称)"}  地址: {device.address}')

    buffer = StreamingByteBuffer()
    writer = None
    print_only = args.print_only

    if args.list_services:
        pass  # 既不打印数据也不写文件，仅列服务
    elif print_only:
        print('打印模式: 不创建/写入任何文件，仅在终端实时显示数据。')
    else:
        writer = LiveCsvWriter(args.output, keep_bad_frames=args.keep_bad_frames)
        print(f'实时数据将写入: {args.output}')
        print('提示: 在 Label Studio 的 Time Series 标注配置里，timeFormat 请填: %Y-%m-%d %H:%M:%S.%L')

    print_count = [0]
    last_good_time_print = [None]
    dropped_count_print = [0]
    hz_window: list[float] = []   # 滑动1秒窗口，用于计算实际采样率

    def calc_hz() -> float:
        now = time.time()
        cutoff = now - 1.0
        while hz_window and hz_window[0] < cutoff:
            hz_window.pop(0)
        hz_window.append(now)
        return float(len(hz_window))

    def notification_handler(sender, data: bytearray):
        packets = buffer.feed(bytes(data))
        for pkt in packets:
            p = parse_one_packet(pkt)
            if p is None:
                continue

            hz = calc_hz()

            if print_only:
                t = p['chip_time']
                # 打印模式下也做同样的坏帧过滤提示（仅提示，不影响打印，除非用户没加 --keep-bad-frames）
                if not args.keep_bad_frames and t is not None and last_good_time_print[0] is not None and t <= last_good_time_print[0]:
                    dropped_count_print[0] += 1
                    print(f'  [丢弃坏帧] 时间戳非单调递增: {fmt_chip_time_dotms(p)}')
                    continue
                if t is not None:
                    last_good_time_print[0] = t
                print_count[0] += 1
                chip_ts = fmt_chip_time_dotms(p)
                now = datetime.now()
                pc_ts = now.strftime('%H:%M:%S.') + f'{now.microsecond // 1000:03d}'
                acc = p['acc']
                gyro = p['gyro']
                print(f'[{print_count[0]:>6d}] PC={pc_ts}  片上={chip_ts}  '
                      f'acc=({acc[0]:+.3f},{acc[1]:+.3f},{acc[2]:+.3f})g  '
                      f'gyro=({gyro[0]:+7.3f},{gyro[1]:+7.3f},{gyro[2]:+7.3f})°/s  '
                      f'{hz:.1f}Hz')
            else:
                writer.write_packet(p)
                if writer.count_written % 50 == 0:
                    acc = p['acc']
                    print(f'  已接收 {writer.count_written} 帧  最新加速度: '
                          f'X={acc[0]:.3f} Y={acc[1]:.3f} Z={acc[2]:.3f} g  '
                          f'{hz:.1f}Hz')

    async with BleakClient(device) as client:
        print('已连接。')

        if args.list_services:
            await list_services(client)
            return

        notify_uuid = args.notify_uuid
        if notify_uuid:
            candidates = [notify_uuid]
        else:
            candidates = DEFAULT_NOTIFY_CANDIDATES

        subscribed = None
        for uuid in candidates:
            try:
                await client.start_notify(uuid, notification_handler)
                subscribed = uuid
                break
            except Exception as e:
                print(f'  尝试订阅 {uuid} 失败: {e}')

        if subscribed is None:
            print('所有候选 UUID 均订阅失败。请先运行 --list-services 查看该设备真实的'
                  '服务/特征值列表，然后用 --notify-uuid 指定正确的 Notify 特征值。')
            if writer is not None:
                writer.close()
            return

        print(f'已订阅特征值 {subscribed}，开始接收数据... (按 Ctrl+C 停止)')

        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            try:
                await client.stop_notify(subscribed)
            except Exception:
                pass
            if writer is not None:
                writer.close()
                print(f'\n采集结束。共写入 {writer.count_written} 帧，丢弃坏帧 {writer.count_dropped} 个。'
                      f'\n文件已保存: {args.output}')
            elif print_only:
                print(f'\n采集结束。共打印 {print_count[0]} 帧，丢弃坏帧 {dropped_count_print[0]} 个。'
                      f'\n（打印模式未写入任何文件。）')


def main():
    ap = argparse.ArgumentParser(
        description='通过 BLE 连接 WT901SDCL-BT50，实时接收IMU数据并写入 Label Studio 格式 CSV',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--scan', action='store_true', help='仅扫描并列出附近所有 BLE 设备，不连接')
    ap.add_argument('--name', default='WT901', help='按名称关键字（忽略大小写）查找设备，默认 "WT901"')
    ap.add_argument('--address', default=None, help='直接按 MAC 地址连接（优先于 --name）')
    ap.add_argument('--scan-timeout', type=float, default=8.0, help='扫描超时时间（秒），默认8秒')
    ap.add_argument('-o', '--output', default='live_labelstudio.csv', help='实时写入的CSV文件路径')
    ap.add_argument('--print-only', action='store_true',
                     help='只在终端实时打印每一帧数据，不创建/写入任何CSV文件')
    ap.add_argument('--notify-uuid', default=None,
                     help='手动指定 Notify 特征值 UUID；不指定则按内置候选列表依次尝试')
    ap.add_argument('--list-services', action='store_true',
                     help='连接成功后只打印该设备的服务/特征值列表，不订阅、不写文件'
                          '（用于现场核实实际 UUID）')
    ap.add_argument('--keep-bad-frames', action='store_true',
                     help='默认会丢弃时间戳非单调递增的坏帧（蓝牙重连导致的时钟复位/重传），'
                          '加此参数则全部写入不做过滤')
    ap.add_argument('--calibrate', action='store_true',
                     help='时间漂移评估模式：采集数据并逐帧对比 PC 时间与片上时间，'
                          '统计漂移率（前提：已用官方上位机软件校准过设备时间）')
    ap.add_argument('--cal-duration', type=float, default=30.0,
                     help='漂移评估采集时长（秒），默认 30 秒')
    args = ap.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
