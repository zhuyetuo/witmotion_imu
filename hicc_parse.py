# -*- coding: utf-8 -*-
"""
HICC_PetCollar 协议解析
========================
GATT UUID、帧常量、DP 解析、FrameBuffer 重组、校时帧构造，
以及设备连接所需的 find_tx_uuid / find_rx_uuid / send_timesync。
被 hicc_ble_live.py、hicc_drift_analysis.py、imu_camera_sync.py 共用。
"""

import struct
from datetime import datetime, timezone, timedelta

from bleak import BleakClient

# ── GATT UUID ──────────────────────────────────────────────────────────────
SERVICE_UUID = 'a6ed0201-d344-460a-8075-b9e8ec90d71b'
TX_UUID      = 'a6ed0202-d344-460a-8075-b9e8ec90d71b'  # Notify，设备→App
RX_UUID      = 'a6ed0203-d344-460a-8075-b9e8ec90d71b'  # Write，App→设备

DEVICE_NAME  = 'HICC_PetCollar'
TZ_OFFSET_MS = 8 * 3600 * 1000.0   # 设备把北京时间当 UTC 算 Unix ms
TZ_CST       = timezone(timedelta(hours=8))

# ── 帧常量 ─────────────────────────────────────────────────────────────────
FRAME_HEADER  = bytes([0x55, 0xAA])
CMD_REPORT    = 0x05
CMD_TIMESYNC  = 0x06
PAYLOAD_6AXIS = 0x3C
PAYLOAD_ENV   = 0x24

# ── DP id ──────────────────────────────────────────────────────────────────
DP_TIMESTAMP = 0x0A
DP_GYRO_X    = 0x14
DP_GYRO_Y    = 0x15
DP_GYRO_Z    = 0x16
DP_ACC_X     = 0x17
DP_ACC_Y     = 0x18
DP_ACC_Z     = 0x19
DP_TEMP_IN   = 0x20
DP_HUM_IN    = 0x21
DP_TEMP_BODY = 0x22
DP_BATT_MV   = 0x0B

# ── CSV 表头 ───────────────────────────────────────────────────────────────
CSV_HEADER_6AXIS = ['timestamp', 'acc_x', 'acc_y', 'acc_z',
                    'gyro_x', 'gyro_y', 'gyro_z']
CSV_HEADER_ENV   = ['timestamp', 'temp_in_c', 'hum_in_pct', 'temp_body_c', 'batt_mv']


# ── 校验和 ─────────────────────────────────────────────────────────────────

def calc_checksum(frame_without_cs: bytes) -> int:
    return sum(frame_without_cs) & 0xFF


def verify_frame(frame: bytes) -> bool:
    return calc_checksum(frame[:-1]) == frame[-1]


# ── 校时帧构造 ──────────────────────────────────────────────────────────────

def build_timesync_frame(dt: datetime | None = None) -> bytes:
    if dt is None:
        dt = datetime.now(TZ_CST)
    year_offset = dt.year - 2000
    payload = bytes([0x01, year_offset, dt.month, dt.day,
                     dt.hour, dt.minute, dt.second])
    header = bytes([0x55, 0xAA, 0x00, CMD_TIMESYNC, 0x00, len(payload)])
    body = header + payload
    return body + bytes([calc_checksum(body)])


# ── DP 解析 ────────────────────────────────────────────────────────────────

def parse_dp_sequence(payload: bytes) -> dict:
    result = {}
    i = 0
    n = len(payload)
    while i + 4 <= n:
        dpid    = payload[i]
        dp_type = payload[i + 1]
        dp_len  = struct.unpack('>H', payload[i + 2:i + 4])[0]
        i += 4
        if i + dp_len > n:
            break
        raw = payload[i:i + dp_len]
        i += dp_len
        if dp_type == 0x02:
            result[dpid] = int.from_bytes(raw, byteorder='big', signed=True)
        elif dp_type == 0x04:
            result[dpid] = raw[0]
    return result


def decode_report(dp: dict, frame_type: str) -> dict:
    out = {'frame_type': frame_type}
    if DP_TIMESTAMP in dp:
        ts_ms = dp[DP_TIMESTAMP]
        try:
            dt = datetime.utcfromtimestamp(ts_ms / 1000.0)
            out['timestamp_ms']  = ts_ms
            out['timestamp_str'] = dt.strftime('%Y-%m-%d %H:%M:%S.') + f'{ts_ms % 1000:03d}'
        except (OSError, OverflowError, ValueError):
            out['timestamp_ms']  = ts_ms
            out['timestamp_str'] = f'raw={ts_ms}'
    if frame_type == '6axis':
        out['gyro_x'] = dp.get(DP_GYRO_X, 0) / 1_000_000.0
        out['gyro_y'] = dp.get(DP_GYRO_Y, 0) / 1_000_000.0
        out['gyro_z'] = dp.get(DP_GYRO_Z, 0) / 1_000_000.0
        out['acc_x']  = dp.get(DP_ACC_X,  0) / 1_000_000.0
        out['acc_y']  = dp.get(DP_ACC_Y,  0) / 1_000_000.0
        out['acc_z']  = dp.get(DP_ACC_Z,  0) / 1_000_000.0
    else:
        out['temp_in']   = dp.get(DP_TEMP_IN,   0) / 10.0
        out['hum_in']    = dp.get(DP_HUM_IN,    0) / 10.0
        out['temp_body'] = dp.get(DP_TEMP_BODY, 0) / 10.0
        out['batt_mv']   = dp.get(DP_BATT_MV,   0)
    return out


# ── 流式帧缓冲区 ───────────────────────────────────────────────────────────

class FrameBuffer:
    """累积 BLE notify 分片数据，按 0x55 0xAA + 长度字段重组完整帧。"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf.extend(data)
        frames = []
        while True:
            idx = -1
            for i in range(len(self.buf) - 1):
                if self.buf[i] == 0x55 and self.buf[i + 1] == 0xAA:
                    idx = i
                    break
            if idx == -1:
                self.buf = self.buf[-1:] if self.buf else bytearray()
                break
            if idx > 0:
                self.buf = self.buf[idx:]
            if len(self.buf) < 6:
                break
            data_len  = struct.unpack('>H', self.buf[4:6])[0]
            total_len = 6 + data_len + 1
            if len(self.buf) < total_len:
                break
            frame = bytes(self.buf[:total_len])
            self.buf = self.buf[total_len:]
            if verify_frame(frame):
                frames.append(frame)
            else:
                cs_got = frame[-1]
                cs_exp = calc_checksum(frame[:-1])
                print(f'  [校验失败] 丢弃帧  got=0x{cs_got:02X} expected=0x{cs_exp:02X}')
        return frames


# ── 帧解析 ─────────────────────────────────────────────────────────────────

def parse_frame(frame: bytes) -> dict | None:
    cmd      = frame[3]
    data_len = struct.unpack('>H', frame[4:6])[0]
    payload  = frame[6:6 + data_len]
    if cmd == CMD_TIMESYNC:
        return {'frame_type': 'timesync_request', 'raw': frame.hex(' ')}
    if cmd != CMD_REPORT:
        return {'frame_type': f'unknown_cmd_0x{cmd:02X}', 'raw': frame.hex(' ')}
    dp = parse_dp_sequence(payload)
    has_env = (DP_TEMP_IN in dp or DP_HUM_IN in dp or
               DP_TEMP_BODY in dp or DP_BATT_MV in dp)
    return decode_report(dp, 'env' if has_env else '6axis')


# ── HICC 专用 BLE 辅助 ────────────────────────────────────────────────────

async def find_rx_uuid(client: BleakClient) -> str | None:
    for svc in client.services:
        for ch in svc.characteristics:
            if ch.uuid.lower() == RX_UUID:
                return ch.uuid
    for svc in client.services:
        for ch in svc.characteristics:
            if 'write' in ch.properties or 'write-without-response' in ch.properties:
                print(f'  提示: 未找到标准 RX UUID，使用备用写特征: {ch.uuid}')
                return ch.uuid
    return None


async def find_tx_uuid(client: BleakClient) -> str | None:
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


async def send_timesync(client: BleakClient, rx_uuid: str):
    now_cst = datetime.now(TZ_CST)
    frame = build_timesync_frame(now_cst)
    print(f'  发送校时帧 ({now_cst.strftime("%Y-%m-%d %H:%M:%S")} CST): {frame.hex(" ")}')
    await client.write_gatt_char(rx_uuid, frame, response=False)
