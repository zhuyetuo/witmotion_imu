# -*- coding: utf-8 -*-
"""
HICC_PetCollar 时间漂移分析与补偿验证脚本
==========================================

与 wit_drift_analysis.py 功能相同，但针对自制设备的 0x55AA 协议。

关键差异：
  - HICC 片上时间戳是 Unix 毫秒（int64，DP 0x0A），精度到毫秒
  - WitMotion 片上时间是年月日时分秒+ms字段，精度相同但格式不同
  - HICC 设备脚本连接时自动校时（下发北京时间），WitMotion 需要上位机校时

工作流程（三阶段连续执行）：
  阶段1：采集 --duration 秒六轴帧，逐帧计算 PC-chip 偏移，线性回归评估漂移率和匀速性
  阶段2：用拟合斜率对所有帧做线性补偿
  阶段3：重新评估补偿后的残余偏移，验证效果

用法:
    python hicc_drift_analysis.py --address EA:CB:3E:CF:00:1B
    python hicc_drift_analysis.py --address EA:CB:3E:CF:00:1B --duration 120
    python hicc_drift_analysis.py --address EA:CB:3E:CF:00:1B --plot
    python hicc_drift_analysis.py --address EA:CB:3E:CF:00:1B --duration 120 -o hicc_drift.csv
"""

import argparse
import asyncio
import csv
import struct
import sys
import time
from datetime import datetime, timezone, timedelta

from bleak import BleakClient, BleakScanner

from hicc_parse import (
    FrameBuffer,
    parse_dp_sequence,
    build_timesync_frame,
    find_tx_uuid,
    find_rx_uuid,
    send_timesync,
    DP_TIMESTAMP, DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z,
    DP_ACC_X, DP_ACC_Y, DP_ACC_Z,
    DP_TEMP_IN, DP_HUM_IN, DP_TEMP_BODY, DP_BATT_MV,
    CMD_REPORT,
    TZ_OFFSET_MS,
)

# ── 线性回归（纯 Python）────────────────────────────────────────────────────

def linear_fit(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0
    sx  = sum(xs);  sy  = sum(ys)
    sxx = sum(x*x for x in xs)
    sxy = sum(x*y for x, y in zip(xs, ys))
    d   = n * sxx - sx * sx
    if d == 0:
        return 0.0, sy / n, 1.0
    slope = (n * sxy - sx * sy) / d
    inter = (sy - slope * sx) / n
    ym    = sy / n
    ss_tot = sum((y - ym)**2 for y in ys)
    ss_res = sum((y - (slope*x + inter))**2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return slope, inter, r2


def vstats(values):
    n = len(values)
    if n == 0:
        return {}
    m = sum(values) / n
    s = (sum((v - m)**2 for v in values) / n) ** 0.5
    return {'n': n, 'mean': m, 'std': s, 'min': min(values), 'max': max(values)}


# ── BLE 采集 ─────────────────────────────────────────────────────────────────

async def collect(args):
    """连接 HICC_PetCollar，发校时，采集 args.duration 秒六轴帧。"""
    print(f'连接中: {args.address} ...')

    buf   = FrameBuffer()
    rows  = []
    t0    = [None]

    def handler(sender, data: bytearray):
        pc_ms = time.time() * 1000.0 + TZ_OFFSET_MS   # 对齐芯片的 naive-UTC 基准
        for frame in buf.feed(bytes(data)):
            if frame[3] != CMD_REPORT:
                continue
            data_len = struct.unpack('>H', frame[4:6])[0]
            payload  = frame[6:6 + data_len]
            dp = parse_dp_sequence(payload)
            if DP_TIMESTAMP not in dp:
                continue
            # 跳过温湿度帧，只用六轴帧（25Hz）
            if any(k in dp for k in (DP_TEMP_IN, DP_HUM_IN, DP_TEMP_BODY, DP_BATT_MV)):
                continue
            chip_ms = float(dp[DP_TIMESTAMP])
            if t0[0] is None:
                t0[0] = pc_ms
            rows.append({
                'pc_ms'   : pc_ms,
                'chip_ms' : chip_ms,
                'acc'     : [dp.get(k, 0) / 1_000_000.0 for k in (DP_ACC_X, DP_ACC_Y, DP_ACC_Z)],
                'gyro'    : [dp.get(k, 0) / 1_000_000.0 for k in (DP_GYRO_X, DP_GYRO_Y, DP_GYRO_Z)],
            })

    async with BleakClient(args.address) as client:
        print('已连接。')
        tx_uuid = await find_tx_uuid(client)
        rx_uuid = await find_rx_uuid(client)
        if tx_uuid is None:
            print('错误: 未找到 TX Notify 特征')
            return []
        # 自动校时
        if rx_uuid:
            print('  [校时] 下发当前北京时间...')
            await send_timesync(client, rx_uuid)
            await asyncio.sleep(0.2)
        await client.start_notify(tx_uuid, handler)
        print(f'已订阅 {tx_uuid}，采集 {args.duration:.0f} 秒...\n')
        try:
            await asyncio.sleep(args.duration)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print('用户中断，提前结束采集。')
        finally:
            try:
                await client.stop_notify(tx_uuid)
            except Exception:
                pass
    return rows


# ── 分析 ─────────────────────────────────────────────────────────────────────

def analyse(rows):
    if len(rows) < 2:
        return {}

    pc0   = rows[0]['pc_ms']
    chip0 = rows[0]['chip_ms']

    elapsed_s = []
    offset_ms = []

    for r in rows:
        e   = (r['pc_ms']   - pc0)   / 1000.0
        off = (r['pc_ms']   - pc0) - (r['chip_ms'] - chip0)   # 累积偏移
        elapsed_s.append(e)
        offset_ms.append(off)
        r['elapsed_s'] = e
        r['offset_ms'] = off

    slope_ms_per_s, intercept, r2 = linear_fit(elapsed_s, offset_ms)
    slope_ms_per_min = slope_ms_per_s * 60.0
    ppm = slope_ms_per_s / 1000.0 * 1_000_000

    residuals = [off - (slope_ms_per_s * e + intercept)
                 for e, off in zip(elapsed_s, offset_ms)]

    return {
        'rows'            : rows,
        'elapsed_s'       : elapsed_s,
        'offset_ms'       : offset_ms,
        'slope_ms_per_s'  : slope_ms_per_s,
        'slope_ms_per_min': slope_ms_per_min,
        'intercept'       : intercept,
        'r2'              : r2,
        'ppm'             : ppm,
        'residuals'       : residuals,
        'res_stats'       : vstats(residuals),
        'n'               : len(rows),
        'duration_s'      : elapsed_s[-1],
    }


def apply_compensation(result):
    slope  = result['slope_ms_per_s']
    interc = result['intercept']
    rows   = result['rows']
    pc0    = rows[0]['pc_ms']
    chip0  = rows[0]['chip_ms']

    comp = []
    for r in rows:
        e = r['elapsed_s']
        corrected_chip_elapsed = (r['chip_ms'] - chip0) + slope * e + interc
        pc_elapsed = r['pc_ms'] - pc0
        comp.append(pc_elapsed - corrected_chip_elapsed)
    return comp


# ── 打印报告 ──────────────────────────────────────────────────────────────────

def print_phase1(result):
    n, dur  = result['n'], result['duration_s']
    sm, ppm = result['slope_ms_per_min'], result['ppm']
    r2, rs  = result['r2'], result['res_stats']

    print('=' * 60)
    print('阶段1：漂移测量结果')
    print('=' * 60)
    print(f'  采集帧数:           {n} 帧')
    print(f'  采集时长:           {dur:.1f} 秒')
    print(f'  采样率:             {n/dur:.1f} Hz')
    print()
    print(f'  漂移率:             {sm:+.3f} ms/min  ({ppm:+.1f} ppm)')
    print(f'  推算 1小时误差:     {sm*60:+.0f} ms')
    print(f'  推算 1天误差:       {sm*60*24/1000:+.2f} 秒')
    print()
    print(f'  线性度 R²:          {r2:.6f}')
    if r2 >= 0.999:
        print('  线性评估:           ✓ 漂移高度匀速，线性补偿效果极佳')
    elif r2 >= 0.99:
        print('  线性评估:           ✓ 漂移基本匀速，线性补偿效果良好')
    elif r2 >= 0.95:
        print('  线性评估:           ⚠ 漂移有轻微非线性，补偿后仍有残余误差')
    else:
        print('  线性评估:           ✗ 漂移非线性严重，线性补偿效果有限')
    print()
    print(f'  拟合残差 std:       {rs.get("std",0):.2f} ms  '
          f'(范围 {rs.get("min",0):+.1f} ~ {rs.get("max",0):+.1f} ms)')
    print('  （残差 = BLE传输抖动 + 非线性分量）')


def print_phase3(comp, elapsed_s, res_std_before):
    s = vstats(comp)
    slope2, _, _ = linear_fit(elapsed_s, comp)
    trend = slope2 * 60.0
    trend_ppm = slope2 / 1000.0 * 1_000_000

    print()
    print('=' * 60)
    print('阶段3：补偿后漂移再评估')
    print('=' * 60)
    print(f'  补偿后偏移均值:     {s["mean"]:+.2f} ms')
    print(f'  补偿后偏移范围:     {s["min"]:+.1f} ~ {s["max"]:+.1f} ms')
    print(f'  补偿后偏移 std:     {s["std"]:.2f} ms')
    print()
    print(f'  补偿后残余漂移:     {trend:+.3f} ms/min  ({trend_ppm:+.1f} ppm)  （理想值 ≈ 0）')
    print()

    # HICC 片上时间是 Unix ms，精度高，BLE 批发包抖动比 WitMotion 小得多
    if abs(trend) < 0.5:
        if s['std'] < 5.0:
            print('  ✓ 补偿效果优秀：残余误差 <5ms std，漂移已完全消除')
        elif s['std'] < 20.0:
            print('  ✓ 补偿效果良好：残余趋势≈0，std 主要来自 BLE 传输抖动')
        else:
            print('  ⚠ 残余 std 偏大，BLE 抖动较严重')
    else:
        print(f'  ⚠ 残余漂移率 {trend:+.2f} ms/min 不为零，漂移存在非线性分量')

    # 与 WitMotion 横向对比
    print()
    print('  ── 与 WitMotion 对比 ──────────────────────────')
    witmotion_drift = 430.0   # ms/min，你实测的平均值
    ratio = abs(result_drift_rate[0]) / witmotion_drift if witmotion_drift else 0
    print(f'  WitMotion 漂移率（参考）: ~+430 ms/min  (+7180 ppm)')
    print(f'  HICC 自制 漂移率:         {result_drift_rate[0]:+.3f} ms/min  ({result_drift_rate[0]/430*7180:+.1f} ppm)')
    better = witmotion_drift / abs(result_drift_rate[0]) if result_drift_rate[0] != 0 else float('inf')
    print(f'  自制设备比 WitMotion 好:  {better:.0f} 倍')


# 用全局变量传漂移率给 print_phase3（避免修改函数签名）
result_drift_rate = [0.0]


# ── 可选图表 ──────────────────────────────────────────────────────────────────

def plot_results(result, comp):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print('\n提示: pip install matplotlib 可显示图表')
        return

    # Windows 中文字体配置
    plt.rcParams['font.family'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    elapsed = result['elapsed_s']
    raw_off = result['offset_ms']
    slope   = result['slope_ms_per_s']
    interc  = result['intercept']
    fit_line = [slope * e + interc for e in elapsed]

    slope2, _, _ = linear_fit(elapsed, comp)
    s = vstats(comp)

    fig, axes = plt.subplots(2, 1, figsize=(12, 8))
    fig.suptitle('HICC_PetCollar 时间漂移分析', fontsize=14)

    ax1 = axes[0]
    ax1.scatter(elapsed, raw_off, s=1, alpha=0.4, color='steelblue', label='原始偏移')
    ax1.plot(elapsed, fit_line, color='red', linewidth=1.5,
             label=f'线性拟合  斜率={result["slope_ms_per_min"]:+.3f}ms/min  R²={result["r2"]:.5f}')
    ax1.set_xlabel('经过时间 (秒)')
    ax1.set_ylabel('偏移 PC−chip (ms)')
    ax1.set_title('阶段1：漂移测量（原始）')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    ax2 = axes[1]
    ax2.scatter(elapsed, comp, s=1, alpha=0.4, color='green', label='补偿后偏移')
    ax2.axhline(s['mean'], color='orange', linewidth=1.5,
                label=f'均值 {s["mean"]:+.1f}ms  std={s["std"]:.2f}ms')
    ax2.axhline(s['mean'] + s['std'], color='orange', linewidth=0.8, linestyle='--')
    ax2.axhline(s['mean'] - s['std'], color='orange', linewidth=0.8, linestyle='--')
    ax2.set_xlabel('经过时间 (秒)')
    ax2.set_ylabel('补偿后偏移 (ms)')
    ax2.set_title(f'阶段3：补偿后再评估  残余漂移={slope2*60:+.3f}ms/min'
                  f'  （锯齿=BLE批发包特性，非非线性漂移）')
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.show()


# ── CSV 输出 ──────────────────────────────────────────────────────────────────

def save_csv(path, result, comp):
    slope  = result['slope_ms_per_s']
    interc = result['intercept']
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['elapsed_s', 'chip_ms', 'pc_ms_adjusted',
                    'offset_ms', 'fit_ms', 'residual_ms', 'compensated_offset_ms',
                    'acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z'])
        for i, r in enumerate(result['rows']):
            e   = r['elapsed_s']
            fit = slope * e + interc
            w.writerow([
                f'{e:.3f}',
                f'{r["chip_ms"]:.0f}',
                f'{r["pc_ms"]:.0f}',
                f'{r["offset_ms"]:.3f}',
                f'{fit:.3f}',
                f'{result["residuals"][i]:.3f}',
                f'{comp[i]:.3f}',
                f'{r["acc"][0]:.6f}', f'{r["acc"][1]:.6f}', f'{r["acc"][2]:.6f}',
                f'{r["gyro"][0]:.6f}', f'{r["gyro"][1]:.6f}', f'{r["gyro"][2]:.6f}',
            ])
    print(f'\n详细数据已保存: {path}')


# ── 主逻辑 ───────────────────────────────────────────────────────────────────

async def run(args):
    print('【阶段1】开始采集，评估时间漂移...\n')
    rows = await collect(args)
    if len(rows) < 10:
        print('采集到的帧数太少（<10），无法分析。')
        return

    result = analyse(rows)
    result_drift_rate[0] = result['slope_ms_per_min']
    print_phase1(result)

    print()
    print('=' * 60)
    print('阶段2：应用线性漂移补偿')
    print('=' * 60)
    comp = apply_compensation(result)
    print(f'  补偿公式: corrected_chip_elapsed += '
          f'{result["slope_ms_per_s"]:+.6f}ms/s × t + {result["intercept"]:+.3f}ms')
    print(f'  已对 {result["n"]} 帧应用补偿。')

    print_phase3(comp, result['elapsed_s'], result['res_stats'].get('std', 1.0))

    if args.output:
        save_csv(args.output, result, comp)

    if args.plot:
        plot_results(result, comp)


def main():
    ap = argparse.ArgumentParser(
        description='HICC_PetCollar 时间漂移分析：测量漂移 → 线性补偿 → 验证效果',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--address', required=True,
                    help='设备 MAC 地址，如 EA:CB:3E:CF:00:1B')
    ap.add_argument('--duration', type=float, default=60.0,
                    help='采集时长（秒），建议 ≥60，默认 60')
    ap.add_argument('-o', '--output', default=None,
                    help='逐帧详细数据保存到 CSV')
    ap.add_argument('--plot', action='store_true',
                    help='采集结束后弹出漂移趋势图（需要 matplotlib）')
    args = ap.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
