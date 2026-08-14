#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同时连接两个（或更多）IMU设备（WitMotion 和/或 HICC_PetCollar 混用），在同样
的物理条件下（比如都静置在桌上）各采集一段时间的6轴数据，逐项对比统计量，
帮助判断"不同型号设备的数据量级/噪声水平是否一致"——如果差异很大，说明
拿一种设备收集的数据训练出来的模型，直接套到另一种设备上效果大概率会差，
需要考虑单独训练或者做好每种设备的校准/归一化再混合训练。

背景:
    check_device_worn.py 只给"静置/活动"这一个二元判断，看不出具体差多少；
    这个脚本把详细的统计量（每个轴的均值/标准差、加速度模长、角速度模长）
    都列出来，方便直接比较两种设备。

用法:
    # 都放桌上静置，同时采集8秒对比
    python compare_imu_devices.py --imu wit=WT5 --imu hicc=EA:CB:3E:CF:00:1A --duration 8

    # 三个以上也可以，比如两个wit一个hicc一起比
    python compare_imu_devices.py --imu wit=WT1 --imu wit=WT5 --imu hicc=EA:CB:3E:CF:00:1A --duration 8

怎么看结果:
    - 静置状态下，理想情况所有设备的 |acc| 均值都应该在 1.0g 附近（重力），
      角速度均值应该接近 0°/s；如果某个设备的 |acc| 均值明显偏离1.0g，可能
      是没校准好或者单位换算有问题，不只是噪声大小的问题。
    - acc/gyro 的标准差反映本底噪声水平：同样静置条件下，如果两种设备的
      标准差相差好几倍（脚本会算出比值），说明信号"干净程度"差异较大，
      模型如果只在低噪声设备上训练，遇到高噪声设备的数据很可能识别变差
      （特征分布不一样了），这种情况下比较稳妥的做法是：
        1. 分别用两种设备各自采集一批训练数据，各自训练一个模型；或者
        2. 两种设备的数据都收集足够多，混合训练一个模型（让模型见过两种
           噪声水平的样本，泛化性更好），但要保证两边訓練样本量别差太多；
      不建议只用一种设备训练、直接拿去用在另一种设备上不做任何验证。
"""

import argparse
import asyncio
import sys

try:
    import numpy as np
except ImportError:
    print('缺少 numpy，请先安装: pip install numpy')
    sys.exit(1)

try:
    from bleak import BleakClient  # noqa: F401  (check_device_worn 内部用到，这里只是确认依赖存在)
except ImportError:
    print('缺少 bleak，请先安装: pip install bleak')
    sys.exit(1)

from check_device_worn import parse_imu_spec, collect_one


def compute_stats(rows):
    acc = np.array([r['acc'] for r in rows])
    gyro = np.array([r['gyro'] for r in rows])
    acc_mag = np.linalg.norm(acc, axis=1)
    gyro_mag = np.linalg.norm(gyro, axis=1)
    return {
        'n': len(rows),
        'acc_mean': acc.mean(axis=0), 'acc_std': acc.std(axis=0),
        'gyro_mean': gyro.mean(axis=0), 'gyro_std': gyro.std(axis=0),
        'acc_mag_mean': float(acc_mag.mean()), 'acc_mag_std': float(acc_mag.std()),
        'gyro_mag_mean': float(gyro_mag.mean()), 'gyro_mag_std': float(gyro_mag.std()),
    }


async def main_async(args):
    try:
        specs = [parse_imu_spec(spec) for spec in args.imu]
    except ValueError as e:
        print(e)
        sys.exit(1)
    if len(specs) < 2:
        print('至少要传2个 --imu 才有得比。')
        sys.exit(1)

    result = {}
    print(f'并发连接 {len(specs)} 个设备，各采集 {args.duration:.0f} 秒...')
    await asyncio.gather(*(
        collect_one(dev_type, ident, args.duration, args.scan_timeout, result)
        for dev_type, ident in specs
    ))

    stats_by_label = {}
    for dev_type, ident in specs:
        label = f'{dev_type}={ident}'
        info = result.get(ident, {'error': '内部错误：没有结果'})
        if 'error' in info:
            print(f'[{label}] ✘ {info["error"]}')
            continue
        rows = info['rows']
        if len(rows) < 5:
            print(f'[{label}] ✘ 样本太少（{len(rows)}个），跳过')
            continue
        stats_by_label[label] = compute_stats(rows)

    if len(stats_by_label) < 2:
        print('\n有效数据的设备不足2个，没法对比。')
        return

    print()
    print('── 各设备统计量 ──')
    for label, s in stats_by_label.items():
        print(f'\n[{label}]  样本数={s["n"]}')
        print(f'  Acc  X={s["acc_mean"][0]:+.4f}±{s["acc_std"][0]:.4f}  '
              f'Y={s["acc_mean"][1]:+.4f}±{s["acc_std"][1]:.4f}  '
              f'Z={s["acc_mean"][2]:+.4f}±{s["acc_std"][2]:.4f}  (g)')
        print(f'  |Acc|  均值={s["acc_mag_mean"]:.4f}g  标准差={s["acc_mag_std"]:.4f}g'
              + ('   ← 偏离1.0g较多，可能没校准好' if abs(s['acc_mag_mean'] - 1.0) > 0.1 else ''))
        print(f'  Gyro X={s["gyro_mean"][0]:+.3f}±{s["gyro_std"][0]:.3f}  '
              f'Y={s["gyro_mean"][1]:+.3f}±{s["gyro_std"][1]:.3f}  '
              f'Z={s["gyro_mean"][2]:+.3f}±{s["gyro_std"][2]:.3f}  (°/s)')
        print(f'  |Gyro| 均值={s["gyro_mag_mean"]:.3f}°/s  标准差={s["gyro_mag_std"]:.3f}°/s')

    print()
    print('── 两两对比（本底噪声水平比值，取较大值/较小值）──')
    labels = list(stats_by_label.keys())
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            a, b = stats_by_label[labels[i]], stats_by_label[labels[j]]
            acc_ratio = max(a['acc_mag_std'], b['acc_mag_std']) / max(min(a['acc_mag_std'], b['acc_mag_std']), 1e-9)
            gyro_ratio = max(a['gyro_mag_mean'], b['gyro_mag_mean']) / max(min(a['gyro_mag_mean'], b['gyro_mag_mean']), 1e-9)
            print(f'  {labels[i]}  vs  {labels[j]}:')
            print(f'    |Acc|标准差比值  ≈ {acc_ratio:.1f}x')
            print(f'    |Gyro|均值比值   ≈ {gyro_ratio:.1f}x')
            if acc_ratio > 3 or gyro_ratio > 3:
                print(f'    ⚠ 差异较大（>3倍），两种设备的信号特征分布不太一样，'
                      f'建议两种设备都单独收集训练数据（分开训练或混合训练验证效果），'
                      f'不建议只用一种训练、直接套到另一种设备上不做验证。')
            print()


def main():
    ap = argparse.ArgumentParser(
        description='同时采集多个IMU设备的数据并对比统计量（均值/标准差/模长），'
                     '帮助判断不同型号设备数据是否需要分开训练/校准')
    ap.add_argument('--imu', action='append', required=True,
                     help='设备标识，格式 类型=标识，至少传2个，比如 '
                          '--imu wit=WT5 --imu hicc=EA:CB:3E:CF:00:1A')
    ap.add_argument('--duration', type=float, default=8.0, help='每个设备采集时长（秒），默认8秒')
    ap.add_argument('--scan-timeout', type=float, default=8.0, help='扫描超时（秒），默认8秒')
    args = ap.parse_args()

    asyncio.run(main_async(args))


if __name__ == '__main__':
    main()
