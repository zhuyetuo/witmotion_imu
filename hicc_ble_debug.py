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
    python hicc_ble_debug.py --scan

    # 连接指定 MAC，自动校时，终端打印数据
    python hicc_ble_debug.py --address EA:CB:3E:CF:00:1B

    # 连接并同时写入 CSV 文件
    python hicc_ble_debug.py --address EA:CB:3E:CF:00:1B -o hicc_imu.csv

    # 连接后只列出服务/特征值，不接收数据（用于核对 UUID）
    python hicc_ble_debug.py --address EA:CB:3E:CF:00:1B --list-services

    # 不自动校时（设备时钟已经准确时使用）
    python hicc_ble_debug.py --address EA:CB:3E:CF:00:1B --no-timesync
"""

import argparse
import asyncio
import struct
import sys
import time
import csv
from datetime import datetime, timezone, timedelta

from bleak import BleakClient, BleakScanner

# ── GATT UUID ──────────────────────────────────────────────────────────────
SERVICE_UUID  = 'a6ed0201-d344-460a-8075-b9e8ec90d71b'
TX_UUID       = 'a6ed0202-d344-460a-8075-b9e8ec90d71b'  # Notify，设备→App
RX_UUID       = 'a6ed0203-d344-460a-8075-b9e8ec90d71b'  # Write，App→设备

DEVICE_NAME   = 'HICC_PetCollar'

# 设备把北京时间当 UTC 算 Unix ms，PC 时间需加此偏移才能对齐芯片时间基准
TZ_OFFSET_MS  = 8 * 3600 * 1000.0

# ── 帧常量 ─────────────────────────────────────────────────────────────────
FRAME_HEADER  = bytes([0x55, 0xAA])
CMD_REPORT    = 0x05
CMD_TIMESYNC  = 0x06

# payload 字节数对应的帧类型 (v1.1)
PAYLOAD_6AXIS = 0x3C   # 60 字节 -> 六轴帧，总长 67
PAYLOAD_ENV   = 0x24   # 36 字节 -> 温湿度帧，总长 43

# DP id
DP_TIMESTAMP  = 0x0A
DP_GYRO_X     = 0x14
DP_GYRO_Y     = 0x15
DP_GYRO_Z     = 0x16
DP_ACC_X      = 0x17
DP_ACC_Y      = 0x18
DP_ACC_Z      = 0x19
DP_TEMP_IN    = 0x20   # 室内温度
DP_HUM_IN     = 0x21   # 室内湿度
DP_TEMP_BODY  = 0x22   # 设备体温
DP_BATT_MV    = 0x0B   # 电池电压 mV

# 东八区偏移（校时下发北京时间）
TZ_CST = timezone(timedelta(hours=8))


# ── 校验和 ─────────────────────────────────────────────────────────────────

def calc_checksum(frame_without_cs: bytes) -> int:
    return sum(frame_without_cs) & 0xFF


def verify_frame(frame: bytes) -> bool:
    """验证完整帧（含末尾校验字节）的校验和。"""
    return calc_checksum(frame[:-1]) == frame[-1]


# ── 构造校时帧 ──────────────────────────────────────────────────────────────

def build_timesync_frame(dt: datetime | None = None) -> bytes:
    """
    构造向 RX 特征写入的校时帧。
    dt: 北京时间（aware 或 naive 均可，naive 视为北京时间）。
        默认使用当前系统时间。
    """
    if dt is None:
        dt = datetime.now(TZ_CST)
    year_offset = dt.year - 2000
    payload = bytes([
        0x01,           # 状态=成功
        year_offset,
        dt.month,
        dt.day,
        dt.hour,
        dt.minute,
        dt.second,
    ])
    header = bytes([0x55, 0xAA, 0x00, CMD_TIMESYNC, 0x00, len(payload)])
    body = header + payload
    cs = calc_checksum(body)
    return body + bytes([cs])


# ── DP 解析 ────────────────────────────────────────────────────────────────

def parse_dp_sequence(payload: bytes) -> dict:
    """
    遍历 payload 中的 DP 单元序列，返回 {dpid: int_value} 字典。
    DP 单元: dpid(1) + type(1) + len(2大端) + value(len字节，有符号大端)
    """
    result = {}
    i = 0
    n = len(payload)
    while i + 4 <= n:
        dpid = payload[i]
        dp_type = payload[i + 1]
        dp_len = struct.unpack('>H', payload[i + 2:i + 4])[0]
        i += 4
        if i + dp_len > n:
            break
        raw = payload[i:i + dp_len]
        i += dp_len

        if dp_type == 0x02:  # value: 有符号整型，大端
            signed = int.from_bytes(raw, byteorder='big', signed=True)
            result[dpid] = signed
        elif dp_type == 0x04:  # enum: 单字节枚举
            result[dpid] = raw[0]
        # 其它 type 暂不解析，跳过
    return result


def decode_report(dp: dict, frame_type: str) -> dict:
    """将原始 DP 整数值换算为物理量，返回便于打印/写文件的字典。"""
    out = {'frame_type': frame_type}

    # 时间戳
    # 设备把我们下发的北京时间当作 UTC naive 计算 Unix ms，
    # 所以解码时用 utcfromtimestamp 直接还原，不做时区转换。
    if DP_TIMESTAMP in dp:
        ts_ms = dp[DP_TIMESTAMP]   # v1.1: int64 毫秒
        try:
            dt = datetime.utcfromtimestamp(ts_ms / 1000.0)
            out['timestamp_ms'] = ts_ms
            out['timestamp_str'] = dt.strftime('%Y-%m-%d %H:%M:%S.') + f'{ts_ms % 1000:03d}'
        except (OSError, OverflowError, ValueError):
            out['timestamp_ms'] = ts_ms
            out['timestamp_str'] = f'raw={ts_ms}'

    if frame_type == '6axis':
        out['gyro_x'] = dp.get(DP_GYRO_X, 0) / 1_000_000.0   # rad/s
        out['gyro_y'] = dp.get(DP_GYRO_Y, 0) / 1_000_000.0
        out['gyro_z'] = dp.get(DP_GYRO_Z, 0) / 1_000_000.0
        out['acc_x']  = dp.get(DP_ACC_X,  0) / 1_000_000.0   # m/s²
        out['acc_y']  = dp.get(DP_ACC_Y,  0) / 1_000_000.0
        out['acc_z']  = dp.get(DP_ACC_Z,  0) / 1_000_000.0
    else:  # env
        out['temp_in']   = dp.get(DP_TEMP_IN,   0) / 10.0     # °C
        out['hum_in']    = dp.get(DP_HUM_IN,    0) / 10.0     # %RH
        out['temp_body'] = dp.get(DP_TEMP_BODY, 0) / 10.0     # °C
        out['batt_mv']   = dp.get(DP_BATT_MV,   0)             # mV

    return out


# ── 流式缓冲区 ─────────────────────────────────────────────────────────────

class FrameBuffer:
    """
    累积 BLE notify 的分片数据，按 0x55 0xAA 帧头 + 长度字段切出完整帧。
    BLE MTU 不足时单帧会被分片，这里做重组。
    """

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf.extend(data)
        frames = []
        while True:
            # 找帧头
            idx = -1
            for i in range(len(self.buf) - 1):
                if self.buf[i] == 0x55 and self.buf[i + 1] == 0xAA:
                    idx = i
                    break
            if idx == -1:
                # 没有帧头，保留最后1字节（可能是0x55的前半）
                self.buf = self.buf[-1:] if self.buf else bytearray()
                break
            if idx > 0:
                self.buf = self.buf[idx:]  # 丢弃帧头前的垃圾字节

            # 需要至少6字节才能读长度
            if len(self.buf) < 6:
                break

            data_len = struct.unpack('>H', self.buf[4:6])[0]
            total_len = 6 + data_len + 1  # header(6) + payload + checksum(1)

            if len(self.buf) < total_len:
                break  # 帧还没收全，等下次

            frame = bytes(self.buf[:total_len])
            self.buf = self.buf[total_len:]

            if verify_frame(frame):
                frames.append(frame)
            else:
                cs_got = frame[-1]
                cs_exp = calc_checksum(frame[:-1])
                print(f'  [校验失败] 丢弃帧  got=0x{cs_got:02X} expected=0x{cs_exp:02X}  '
                      f'hex={frame.hex(" ")}')
        return frames


# ── 解析完整帧 ─────────────────────────────────────────────────────────────

def parse_frame(frame: bytes) -> dict | None:
    """
    解析一帧，返回解码后的字典。
    返回 None 表示不认识的帧（非 cmd=0x05/0x06）。
    """
    cmd = frame[3]
    data_len = struct.unpack('>H', frame[4:6])[0]
    payload = frame[6:6 + data_len]

    if cmd == CMD_TIMESYNC:
        # 设备发来的校时请求 55 AA 00 06 00 00 CS
        return {'frame_type': 'timesync_request', 'raw': frame.hex(' ')}

    if cmd != CMD_REPORT:
        return {'frame_type': f'unknown_cmd_0x{cmd:02X}', 'raw': frame.hex(' ')}

    dp = parse_dp_sequence(payload)

    # 按 payload 长度区分六轴帧 / 温湿度帧
    has_env = (DP_TEMP_IN in dp or DP_HUM_IN in dp or
               DP_TEMP_BODY in dp or DP_BATT_MV in dp)

    frame_type = 'env' if has_env else '6axis'
    return decode_report(dp, frame_type)


# ── 打印 ───────────────────────────────────────────────────────────────────

_frame_count = 0
_hz_window: list[float] = []   # 滑动1秒窗口，用于计算实际采样率

def _calc_hz() -> float:
    now = time.time()
    cutoff = now - 1.0
    while _hz_window and _hz_window[0] < cutoff:
        _hz_window.pop(0)
    _hz_window.append(now)
    return float(len(_hz_window))

def print_decoded(d: dict):
    global _frame_count
    _frame_count += 1
    chip_ts = d.get('timestamp_str', '?')
    ft = d.get('frame_type', '?')
    now_cst = datetime.now(TZ_CST)
    pc_ts = now_cst.strftime('%H:%M:%S.') + f'{now_cst.microsecond // 1000:03d}'

    if ft == '6axis':
        hz = _calc_hz()
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

CSV_HEADER_6AXIS = ['timestamp', 'acc_x', 'acc_y', 'acc_z',
                    'gyro_x', 'gyro_y', 'gyro_z']
CSV_HEADER_ENV   = ['timestamp', 'temp_in_c', 'hum_in_pct', 'temp_body_c', 'batt_mv']


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


# ── BLE 操作 ───────────────────────────────────────────────────────────────

async def scan_devices(timeout: float = 6.0):
    print(f'扫描 BLE 设备中（{timeout:.0f} 秒）...')
    devices = await BleakScanner.discover(timeout=timeout)
    if not devices:
        print('未发现任何 BLE 设备。请确认设备已开机、蓝牙已打开。')
        return
    print(f'发现 {len(devices)} 个设备:')
    for d in sorted(devices, key=lambda x: x.name or ''):
        name = d.name or '(无名称)'
        print(f'  {name:<30s}  {d.address}')


async def list_services(client: BleakClient):
    print('GATT 服务/特征值:')
    for svc in client.services:
        marker = '  *** GUS ***' if svc.uuid.lower() == SERVICE_UUID else ''
        print(f'  服务  {svc.uuid}{marker}')
        for ch in svc.characteristics:
            props = ','.join(ch.properties)
            tx = ' ← TX(Notify)' if ch.uuid.lower() == TX_UUID else ''
            rx = ' ← RX(Write)'  if ch.uuid.lower() == RX_UUID  else ''
            print(f'    特征 {ch.uuid}  [{props}]{tx}{rx}')


async def send_timesync(client: BleakClient, rx_uuid: str):
    now_cst = datetime.now(TZ_CST)
    frame = build_timesync_frame(now_cst)
    print(f'  发送校时帧 ({now_cst.strftime("%Y-%m-%d %H:%M:%S")} CST): '
          f'{frame.hex(" ")}')
    await client.write_gatt_char(rx_uuid, frame, response=False)


async def find_rx_uuid(client: BleakClient) -> str | None:
    """在连接的设备上找 RX 特征（Write 属性）的 UUID。优先用已知 UUID，否则自动搜索。"""
    for svc in client.services:
        for ch in svc.characteristics:
            if ch.uuid.lower() == RX_UUID:
                return ch.uuid
    # 备用：找任何有 write/write-without-response 属性的特征
    for svc in client.services:
        for ch in svc.characteristics:
            if 'write' in ch.properties or 'write-without-response' in ch.properties:
                print(f'  提示: 未找到标准 RX UUID，使用备用写特征: {ch.uuid}')
                return ch.uuid
    return None


async def find_tx_uuid(client: BleakClient) -> str | None:
    """找 TX 特征（Notify 属性）的 UUID。"""
    for svc in client.services:
        for ch in svc.characteristics:
            if ch.uuid.lower() == TX_UUID:
                return ch.uuid
    for svc in client.services:
        for ch in svc.characteristics:
            if 'notify' in ch.properties:
                print(f'  提示: 未找到标准 TX UUID，使用备用 Notify 特征: {ch.uuid}')
                return ch.uuid
    return None


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
