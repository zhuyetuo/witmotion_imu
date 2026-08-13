#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量检测多个 WitMotion 设备当前是"静置"（可能没戴、在充电/桌上）还是
"有活动"（可能正戴在狗身上），方便正式录制前挑选可用设备，不用一个个手动
打开 wit_ble_live.py 看数值再判断。

原理:
    并发连接每个设备，短时间内采集加速度/角速度样本：
    - 静置在桌上/充电时，角速度应该接近0，加速度模长应该稳定在重力附近
      （标准差很小）。
    - 戴在狗身上，哪怕安静趴着不动，呼吸起伏、环境振动、项圈本身的轻微晃动
      也会让角速度/加速度模长的波动明显比纯静置大，走动/挠痒就更不用说了。
    按这两个统计量（角速度模长均值、加速度模长标准差）分别设一个阈值，
    低于阈值才判定为"静置"，阈值可以用 --gyro-threshold/--acc-std-threshold
    按实际情况调（不同设备/不同表面材质，静置时的本底噪声可能不太一样，
    建议先跑一次全都静置在桌上的对照组，看输出的数值大概什么范围，再调阈值）。

用法:
    python check_device_worn.py --imu wit=WT1 --imu wit=WT4 --imu wit=WT5 \\
        --imu wit=WT6 --imu wit=WT7 --imu wit=WT8 --duration 8

    # 阈值不合适时调整（比如静置时本底噪声偏大，误判成"有活动"，就调高阈值）
    python check_device_worn.py --imu wit=WT1 --imu wit=WT4 --duration 8 \\
        --gyro-threshold 8 --acc-std-threshold 0.08

输出末尾会直接给一行可以复制去用的 IMUS= 环境变量，挑出来"看起来有活动"的
设备，配合 record_multicam.sh 用。

注意: 这只是个粗略的启发式判断，不是精确的"佩戴检测"模型——环境振动大、
设备放在会晃的地方，或者狗睡得特别沉，都可能导致误判，仅供参考，最终建议
还是肉眼确认一下。
"""

import argparse
import asyncio
import re
import sys

try:
    import numpy as np
except ImportError:
    print('缺少 numpy，请先安装: pip install numpy')
    sys.exit(1)

try:
    from bleak import BleakClient
except ImportError:
    print('缺少 bleak，请先安装: pip install bleak')
    sys.exit(1)

from ble_utils import find_device
from wit_parse import DEFAULT_NOTIFY_CANDIDATES, StreamingByteBuffer, parse_one_packet

_MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$')


def parse_ident(spec: str) -> str:
    """跟其它脚本的 --imu 格式保持一致，接受 wit=<名称或MAC>（这个脚本只支持wit设备）。"""
    if '=' not in spec:
        raise ValueError(f'--imu 格式应为 wit=标识，例如 wit=WT1 或 wit=D1:FD:A8:C7:1A:EF，收到: {spec!r}')
    dev_type, ident = spec.split('=', 1)
    if dev_type.strip().lower() != 'wit':
        raise ValueError(f'这个脚本只支持 wit 类型设备（WitMotion），收到: {dev_type!r}')
    return ident.strip()


async def collect_one(ident: str, duration: float, scan_timeout: float, result: dict):
    is_mac = bool(_MAC_RE.match(ident))
    try:
        ble_device = await find_device(
            None if is_mac else ident, ident if is_mac else None, timeout=scan_timeout)
    except Exception as e:
        result[ident] = {'error': f'扫描出错: {e}'}
        return
    if ble_device is None:
        result[ident] = {'error': '未找到设备（不在范围内/没开机）'}
        return

    buf = StreamingByteBuffer()
    rows = []

    def on_data(_sender, data: bytearray):
        for pkt in buf.feed(bytes(data)):
            p = parse_one_packet(pkt)
            if p is not None:
                rows.append(p)

    try:
        async with BleakClient(ble_device) as client:
            subscribed = None
            for uuid in DEFAULT_NOTIFY_CANDIDATES:
                try:
                    await client.start_notify(uuid, on_data)
                    subscribed = uuid
                    break
                except Exception:
                    continue
            if subscribed is None:
                result[ident] = {'error': '订阅Notify失败'}
                return
            await asyncio.sleep(duration)
            try:
                await client.stop_notify(subscribed)
            except Exception:
                pass
    except Exception as e:
        result[ident] = {'error': f'连接出错: {e}'}
        return

    result[ident] = {'rows': rows}


def classify(rows, gyro_threshold: float, acc_std_threshold: float):
    acc = np.array([r['acc'] for r in rows])
    gyro = np.array([r['gyro'] for r in rows])
    acc_mag = np.linalg.norm(acc, axis=1)
    gyro_mag = np.linalg.norm(gyro, axis=1)
    acc_std = float(np.std(acc_mag))
    gyro_mean = float(np.mean(gyro_mag))
    is_static = gyro_mean < gyro_threshold and acc_std < acc_std_threshold
    return is_static, acc_std, gyro_mean


async def main_async(args):
    idents = [parse_ident(spec) for spec in args.imu]
    result = {}
    print(f'并发连接 {len(idents)} 个设备，各采集 {args.duration:.0f} 秒判断静置/活动状态...')
    await asyncio.gather(*(collect_one(ident, args.duration, args.scan_timeout, result) for ident in idents))

    print()
    header = f"{'设备':<20} {'样本数':>6} {'加速度模长std(g)':>16} {'角速度模长均值(°/s)':>18}  状态"
    print(header)
    print('-' * len(header))

    worn, static, failed = [], [], []
    for ident in idents:
        info = result.get(ident, {'error': '内部错误：没有结果'})
        if 'error' in info:
            print(f'{ident:<20} {"":>6} {"":>16} {"":>18}  ✘ {info["error"]}')
            failed.append(ident)
            continue
        rows = info['rows']
        if len(rows) < 5:
            print(f'{ident:<20} {len(rows):>6} {"":>16} {"":>18}  ✘ 样本太少，信号差或没连稳')
            failed.append(ident)
            continue
        is_static, acc_std, gyro_mean = classify(rows, args.gyro_threshold, args.acc_std_threshold)
        status = '静置（可能没戴/在充电）' if is_static else '有活动（可能戴在狗身上）'
        print(f'{ident:<20} {len(rows):>6} {acc_std:>16.4f} {gyro_mean:>18.2f}  {status}')
        (static if is_static else worn).append(ident)

    print()
    if worn:
        print(f'看起来有活动/可能已佩戴: {" ".join(worn)}')
    if static:
        print(f'看起来静置/可能未佩戴:   {" ".join(static)}')
    if failed:
        print(f'连接失败/样本不足，需要人工确认: {" ".join(failed)}')

    if worn:
        imus_env = ' '.join(f'wit={x}' for x in worn)
        print(f'\n挑出来的设备可以这样用:\n  IMUS="{imus_env}" ./record_multicam.sh')


def main():
    ap = argparse.ArgumentParser(
        description='批量检测WitMotion设备当前是静置还是有活动，帮助挑选录制用的设备')
    ap.add_argument('--imu', action='append', required=True,
                     help='设备标识，格式 wit=名称或MAC，可重复传，比如 --imu wit=WT1 --imu wit=WT4')
    ap.add_argument('--duration', type=float, default=8.0, help='每个设备采集时长（秒），默认8秒')
    ap.add_argument('--scan-timeout', type=float, default=8.0, help='扫描超时（秒），默认8秒')
    ap.add_argument('--gyro-threshold', type=float, default=5.0,
                     help='角速度模长均值低于这个(°/s)才算静止，默认5')
    ap.add_argument('--acc-std-threshold', type=float, default=0.05,
                     help='加速度模长标准差低于这个(g)才算静止，默认0.05')
    args = ap.parse_args()

    asyncio.run(main_async(args))


if __name__ == '__main__':
    main()
