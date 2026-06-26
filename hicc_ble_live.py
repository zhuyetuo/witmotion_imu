# -*- coding: utf-8 -*-
"""
HICC_PetCollar BLE 调试脚本
============================

适用固件: HICC_PetCollar  协议版本: v1.1
物理通道: BLE GUS 服务，TX 特征 Notify 上报，RX 特征 Write 下发

协议帧格式 (0x55 0xAA):
    帧头(2) + 版本(1,固定0x00) + 命令字(1) + 数据长度(2,大端) + payload + 校验和(1)
    校验和 = 从帧头开始所有字节求和 mod 256

上行帧 (cmd=0x05) 两种类型:
    六轴帧 (25Hz, 67字节): 时间戳(int64 ms) + 陀螺XYZ + 加速XYZ
    温湿度帧 (1Hz, 43字节): 时间戳(int64 ms) + 室温 + 湿度 + 体温 + 电池电压

校时帧 (cmd=0x06):
    设备上电后发送请求，App 向 RX 特征写入当前北京时间

BLE GATT UUID:
    Service:  A6ED0201-D344-460A-8075-B9E8EC90D71B
    TX Notify: A6ED0202-D344-460A-8075-B9E8EC90D71B
    RX Write:  A6ED0203-D344-460A-8075-B9E8EC90D71B

用法:
    # 扫描附近所有 BLE 设备
    python hicc_ble_live.py --scan

    # 连接指定 MAC，自动校时，终端打印数据
    python hicc_ble_live.py --address EA:CB:3E:CF:00:1B

    # 连接并同时写入 CSV 文件
    python hicc_ble_live.py --address EA:CB:3E:CF:00:1B -o hicc_imu.csv

    # 连接后只列出服务/特征值，不接收数据（用于核对 UUID）
    python hicc_ble_live.py --address EA:CB:3E:CF:00:1B --list-services

    # 不自动校时（设备时钟已经准确时使用）
    python hicc_ble_live.py --address EA:CB:3E:CF:00:1B --no-timesync
"""

import argparse
import asyncio
import sys
import time
import csv
from datetime import datetime

from bleak import BleakClient

from ble_utils import HzCounter, scan_devices, list_services
from hicc_parse import (
    SERVICE_UUID, TX_UUID, RX_UUID, DEVICE_NAME,
    TZ_OFFSET_MS, TZ_CST,
    FRAME_HEADER, CMD_REPORT, CMD_TIMESYNC, PAYLOAD_6AXIS, PAYLOAD_ENV,
    DP_TIMESTAMP, DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z,
    DP_ACC_X, DP_ACC_Y, DP_ACC_Z,
    DP_TEMP_IN, DP_HUM_IN, DP_TEMP_BODY, DP_BATT_MV,
    CSV_HEADER_6AXIS, CSV_HEADER_ENV,
    calc_checksum, verify_frame, build_timesync_frame,
    parse_dp_sequence, decode_report, FrameBuffer, parse_frame,
    find_rx_uuid, find_tx_uuid, send_timesync,
)

# ── 打印 ───────────────────────────────────────────────────────────────────

_frame_count = 0
_hz = HzCounter()

def print_decoded(d: dict):
    global _frame_count
    _frame_count += 1
    chip_ts = d.get('timestamp_str', '?')
    ft = d.get('frame_type', '?')
    now_cst = datetime.now(TZ_CST)
    pc_ts = now_cst.strftime('%H:%M:%S.') + f'{now_cst.microsecond // 1000:03d}'

    if ft == '6axis':
        hz = _hz.tick()
        ax, ay, az = d.get('acc_x', 0), d.get('acc_y', 0), d.get('acc_z', 0)
        gx, gy, gz = d.get('gyro_x', 0), d.get('gyro_y', 0), d.get('gyro_z', 0)
        print(f'[{_frame_count:>5d}][6轴] PC={pc_ts}  片上={chip_ts}  '
              f'acc=({ax:+.4f},{ay:+.4f},{az:+.4f})m/s²  '
              f'gyro=({gx:+.6f},{gy:+.6f},{gz:+.6f})rad/s  '
              f'{hz:.1f}Hz')
    elif ft == 'env':
        t_in   = d.get('temp_in',   0)
        h_in   = d.get('hum_in',    0)
        t_body = d.get('temp_body', 0)
        batt   = d.get('batt_mv',   0)
        print(f'[{_frame_count:>5d}][环境] PC={pc_ts}  片上={chip_ts}  '
              f'室温={t_in:.1f}°C  湿度={h_in:.1f}%RH  '
              f'体温={t_body:.1f}°C  电池={batt}mV')
    elif ft == 'timesync_request':
        print(f'  [校时请求] 设备请求校时: {d.get("raw", "")}')
    else:
        print(f'  [未知帧] {d}')


# ── CSV 写入 ───────────────────────────────────────────────────────────────


class CsvWriter:
    def __init__(self, path_6axis: str, path_env: str):
        self._f6 = open(path_6axis, 'w', encoding='utf-8', newline='')
        self._fe = open(path_env,   'w', encoding='utf-8', newline='')
        self._w6 = csv.writer(self._f6)
        self._we = csv.writer(self._fe)
        self._w6.writerow(CSV_HEADER_6AXIS)
        self._we.writerow(CSV_HEADER_ENV)
        self.count_6axis = 0
        self.count_env   = 0

    def write(self, d: dict):
        ts = d.get('timestamp_str', '')
        if d.get('frame_type') == '6axis':
            self._w6.writerow([ts,
                                d.get('acc_x', ''), d.get('acc_y', ''), d.get('acc_z', ''),
                                d.get('gyro_x', ''), d.get('gyro_y', ''), d.get('gyro_z', '')])
            self._f6.flush()
            self.count_6axis += 1
        elif d.get('frame_type') == 'env':
            self._we.writerow([ts,
                                d.get('temp_in', ''), d.get('hum_in', ''),
                                d.get('temp_body', ''), d.get('batt_mv', '')])
            self._fe.flush()
            self.count_env += 1

    def close(self):
        self._f6.close()
        self._fe.close()


# ── 时间校准 ───────────────────────────────────────────────────────────────

async def run_calibrate(args):
    """
    时间校准模式：连接设备、发送校时，然后收 --cal-duration 秒的六轴帧，
    逐帧打印 PC 收包时刻与片上时间戳的偏移（offset = PC_ms - chip_ms），
    最后统计平均偏移和时间漂移率（ms/s）。

    偏移接近 0  → 校时准确，BLE 传输延迟可忽略
    偏移持续增大/减小 → 片上时钟有漂移，漂移率 = Δoffset / Δt
    """
    if not args.address:
        print('--calibrate 需要指定 --address')
        sys.exit(1)

    duration = args.cal_duration
    print(f'连接中: {args.address} ...')

    buf = FrameBuffer()
    samples: list[tuple[float, float]] = []   # (pc_ms, chip_ms)

    def notification_handler(sender, data: bytearray):
        pc_ms = time.time() * 1000.0 + TZ_OFFSET_MS
        frames = buf.feed(bytes(data))
        for frame in frames:
            if frame[3] != CMD_REPORT:
                continue
            data_len = struct.unpack('>H', frame[4:6])[0]
            payload = frame[6:6 + data_len]
            dp = parse_dp_sequence(payload)
            if DP_TIMESTAMP not in dp:
                continue
            # 只用六轴帧（25Hz），跳过温湿度帧
            has_env = (DP_TEMP_IN in dp or DP_HUM_IN in dp or
                       DP_TEMP_BODY in dp or DP_BATT_MV in dp)
            if has_env:
                continue
            chip_ms = float(dp[DP_TIMESTAMP])
            offset_ms = pc_ms - chip_ms
            samples.append((pc_ms, chip_ms))
            elapsed = pc_ms - samples[0][0] if len(samples) > 1 else 0.0
            print(f'  [{len(samples):>4d}] chip={chip_ms:.0f}ms  '
                  f'PC-chip={offset_ms:+.1f}ms  '
                  f'elapsed={elapsed/1000:.2f}s')

    async with BleakClient(args.address) as client:
        print(f'已连接: {args.address}')
        rx_uuid = await find_rx_uuid(client)
        tx_uuid = await find_tx_uuid(client)
        if tx_uuid is None:
            print('错误: 未找到 TX Notify 特征')
            return
        if rx_uuid:
            print(f'  [校时] 下发当前北京时间...')
            await send_timesync(client, rx_uuid)
        await asyncio.sleep(0.2)   # 等设备处理校时

        await client.start_notify(tx_uuid, notification_handler)
        print(f'开始校准采集（{duration} 秒）...\n')
        try:
            await asyncio.sleep(duration)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            try:
                await client.stop_notify(tx_uuid)
            except Exception:
                pass

    if len(samples) < 2:
        print('采集到的帧太少，无法计算漂移。')
        return

    offsets = [pc - chip for pc, chip in samples]
    avg_offset = sum(offsets) / len(offsets)
    # 用首尾线性拟合估算漂移率
    t0_pc, t0_chip = samples[0]
    t1_pc, t1_chip = samples[-1]
    total_pc_s   = (t1_pc   - t0_pc)   / 1000.0
    total_chip_s = (t1_chip - t0_chip) / 1000.0
    drift_ppm = (total_pc_s - total_chip_s) / total_pc_s * 1_000_000 if total_pc_s > 0 else 0.0
    drift_ms_per_min = (offsets[-1] - offsets[0]) / total_pc_s * 60.0 if total_pc_s > 0 else 0.0

    print(f'\n── 校准结果（{len(samples)} 帧，{total_pc_s:.1f} 秒）──')
    print(f'  平均偏移 (PC-chip): {avg_offset:+.1f} ms')
    print(f'  偏移变化范围:       {min(offsets):+.1f} ~ {max(offsets):+.1f} ms')
    print(f'  漂移率:             {drift_ms_per_min:+.2f} ms/min  ({drift_ppm:+.1f} ppm)')
    print(f'  推算 1分钟误差:     约 {abs(drift_ms_per_min):.1f} ms')
    print(f'  推算 1小时误差:     约 {abs(drift_ms_per_min)*60/1000:.1f} 秒')
    print(f'  推算 1天误差:       约 {abs(drift_ms_per_min)*1440/1000:.1f} 秒')
    if abs(avg_offset) < 100:
        print('  ✓ 时间同步良好')
    else:
        print(f'  ⚠ 偏移较大（{avg_offset:+.0f}ms），建议检查校时是否成功')


# ── 主运行逻辑 ─────────────────────────────────────────────────────────────

async def run(args):
    if args.calibrate:
        await run_calibrate(args)
        return

    if args.scan:
        await scan_devices(timeout=args.scan_timeout)
        return

    if not args.address:
        print('请用 --address 指定设备 MAC 地址，或用 --scan 先扫描。')
        print(f'已知 MAC: EA:CB:3E:CF:00:1B  EA:CB:3E:CF:00:1D')
        sys.exit(1)

    print(f'连接中: {args.address} ...')

    buf = FrameBuffer()
    csv_writer = None
    if args.output:
        mac_tag = args.address.replace(':', '').lower()
        ts_tag = datetime.now(TZ_CST).strftime('%Y%m%d_%H%M%S')
        path_6axis = f'data/hicc_{mac_tag}_{ts_tag}_6axis.csv'
        path_env   = f'data/hicc_{mac_tag}_{ts_tag}_env.csv'
        csv_writer = CsvWriter(path_6axis, path_env)
        print(f'六轴数据 -> {path_6axis}')
        print(f'环境数据 -> {path_env}')

    timesync_sent = [False]

    def notification_handler(sender, data: bytearray):
        frames = buf.feed(bytes(data))
        for frame in frames:
            cmd = frame[3]

            # 收到设备发来的校时请求时，自动回应
            if cmd == CMD_TIMESYNC and not timesync_sent[0] and not args.no_timesync:
                print('  [校时] 收到设备校时请求，自动回应...')
                # asyncio 回调里不能直接 await，用 create_task
                asyncio.get_event_loop().create_task(
                    send_timesync(_current_client[0], _rx_uuid[0])
                )
                timesync_sent[0] = True
                return

            d = parse_frame(frame)
            if d is None:
                return
            print_decoded(d)
            if csv_writer:
                csv_writer.write(d)

    _current_client: list[BleakClient | None] = [None]
    _rx_uuid: list[str | None] = [None]

    async with BleakClient(args.address) as client:
        _current_client[0] = client
        print(f'已连接: {args.address}')

        if args.list_services:
            await list_services(client)
            return

        if args.verbose:
            await list_services(client)

        tx_uuid = await find_tx_uuid(client)
        rx_uuid = await find_rx_uuid(client)
        _rx_uuid[0] = rx_uuid

        if tx_uuid is None:
            print('错误: 未找到 Notify 特征（TX），请用 --list-services 检查 UUID。')
            return

        # 主动先发一次校时（不等设备请求）
        if not args.no_timesync and rx_uuid:
            print('  [校时] 主动下发当前北京时间...')
            await send_timesync(client, rx_uuid)
            timesync_sent[0] = True
        elif args.no_timesync:
            print('  [校时] --no-timesync 已跳过校时')
        else:
            print('  警告: 未找到 RX 特征，无法发送校时帧')

        await client.start_notify(tx_uuid, notification_handler)
        print(f'已订阅 TX: {tx_uuid}')
        duration = args.duration
        if duration and duration > 0:
            print(f'开始接收数据... (采集 {duration:.0f} 秒后自动停止)')
        else:
            print('开始接收数据... (按 Ctrl+C 停止)')
        print()

        try:
            if duration and duration > 0:
                await asyncio.sleep(duration)
            else:
                while True:
                    await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            try:
                await client.stop_notify(tx_uuid)
            except Exception:
                pass
            if csv_writer:
                csv_writer.close()
                print(f'\n采集结束。六轴帧 {csv_writer.count_6axis} 条，'
                      f'环境帧 {csv_writer.count_env} 条。')
            else:
                print(f'\n采集结束。共 {_frame_count} 帧。')


def main():
    ap = argparse.ArgumentParser(
        description='HICC_PetCollar BLE 调试脚本（0x55AA 协议，v1.1）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--scan', action='store_true',
                    help='扫描附近所有 BLE 设备并列出，不连接')
    ap.add_argument('--address', default=None,
                    help='设备 MAC 地址，例如 EA:CB:3E:CF:00:1B')
    ap.add_argument('--scan-timeout', type=float, default=8.0,
                    help='扫描超时（秒），默认 8')
    ap.add_argument('-o', '--output', default=None,
                    help='CSV 输出文件基础名，自动生成 _6axis.csv 和 _env.csv 两个文件；'
                         '不指定则只打印到终端')
    ap.add_argument('--list-services', action='store_true',
                    help='连接后列出所有 GATT 服务/特征值，不接收数据')
    ap.add_argument('--no-timesync', action='store_true',
                    help='跳过校时（设备时钟已准确时使用）')
    ap.add_argument('--verbose', action='store_true',
                    help='连接后打印完整的 GATT 服务/特征值列表，再开始接收')
    ap.add_argument('--calibrate', action='store_true',
                    help='时间校准模式：发校时帧后采集数据，逐帧打印 PC时间−片上时间 偏移，'
                         '最后统计平均偏移和漂移率（ms/min）')
    ap.add_argument('--cal-duration', type=float, default=30.0,
                    help='校准采集时长（秒），默认 30 秒')
    ap.add_argument('--duration', type=float, default=0,
                    help='采集时长（秒），到时自动停止；不填或填 0 则手动 Ctrl+C 停止')
    args = ap.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
