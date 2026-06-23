# -*- coding: utf-8 -*-
"""
WitMotion WT901SDCL-BT50 时间漂移分析与补偿验证脚本
=====================================================

工作流程（三个阶段，连续自动执行）：

  阶段1 - 漂移测量
      连接设备，采集 --duration 秒数据，
      每帧记录 PC 收包时刻与片上时间戳的偏移（offset = PC - chip），
      拟合线性回归，评估漂移是否匀速（R² 越接近1越线性）。

  阶段2 - 线性补偿
      用阶段1拟合出的斜率（漂移率 ms/s）对所有帧做线性校正：
          corrected_chip[i] = chip[i] + slope × elapsed[i] + intercept

  阶段3 - 补偿后再评估
      用校正后的时间戳重新计算 PC - chip 偏移，观察残差分布，
      验证补偿效果（理想情况下残差应接近常数 BLE 延迟）。

可选依赖（不安装也能运行，只是没有图表）:
    pip install matplotlib

用法:
    # 用设备名查找（默认采集60秒）
    python wit_drift_analysis.py --name WTSDCL

    # 用 MAC 地址连接，采集120秒，结果保存到CSV
    python wit_drift_analysis.py --address AA:BB:CC:DD:EE:FF --duration 120 -o drift_log.csv

    # 采集完自动弹出漂移趋势图（需要 matplotlib）
    python wit_drift_analysis.py --name WTSDCL --plot
"""

import argparse
import asyncio
import sys
import time
import csv
from datetime import datetime, timedelta

from bleak import BleakClient, BleakScanner

from parse_wit import (
    ACC_RANGE, GYRO_RANGE,
    fmt_chip_time_dotms,
)
from wit_ble_live import (
    StreamingByteBuffer,
    parse_one_packet,
    DEFAULT_NOTIFY_CANDIDATES,
    find_device,
)

# ── 线性回归（不依赖 numpy）──────────────────────────────────────────────────

def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """
    最小二乘线性拟合 y = slope * x + intercept。
    返回 (slope, intercept, r_squared)。
    """
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0
    sum_x  = sum(xs)
    sum_y  = sum(ys)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    denom  = n * sum_xx - sum_x ** 2
    if denom == 0:
        return 0.0, sum_y / n, 1.0
    slope     = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    # R²
    y_mean   = sum_y / n
    ss_tot   = sum((y - y_mean) ** 2 for y in ys)
    ss_res   = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2       = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return slope, intercept, r2


def stats(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {}
    mean = sum(values) / n
    std  = (sum((v - mean) ** 2 for v in values) / n) ** 0.5
    return {'n': n, 'mean': mean, 'std': std,
            'min': min(values), 'max': max(values)}


# ── BLE 采集 ─────────────────────────────────────────────────────────────────

async def collect(args) -> list[dict]:
    """
    连接设备采集 args.duration 秒，返回每帧的记录：
        {elapsed_s, pc_ms, chip_ms, offset_ms, acc, gyro, chip_str}
    """
    device = await find_device(args.name, args.address, timeout=args.scan_timeout)
    if device is None:
        sys.exit(1)

    print(f'连接中: {device.name or "(无名称)"}  {device.address}')

    buf  = StreamingByteBuffer()
    rows: list[dict] = []
    t0   = [None]   # 第一帧 PC 时刻（epoch ms）

    def handler(sender, data: bytearray):
        pc_ms = time.time() * 1000.0
        for pkt in buf.feed(bytes(data)):
            p = parse_one_packet(pkt)
            if p is None or p['chip_time'] is None:
                continue
            if t0[0] is None:
                t0[0] = pc_ms
            chip_dt  = p['chip_time']
            # chip_ms: 把 chip_time 转成"从当天零点起"的毫秒，仅用于偏移差分，
            # 但用 epoch-style 更通用——这里用采集起始点 chip_dt0 做差分，
            # 以避免跨零点问题。先存绝对值，后处理时统一换算。
            rows.append({
                'pc_ms'    : pc_ms,
                'chip_dt'  : chip_dt,
                'chip_str' : fmt_chip_time_dotms(p),
                'acc'      : p['acc'],
                'gyro'     : p['gyro'],
            })

    async with BleakClient(device) as client:
        print('已连接。')
        candidates = [args.notify_uuid] if args.notify_uuid else DEFAULT_NOTIFY_CANDIDATES
        subscribed = None
        for uuid in candidates:
            try:
                await client.start_notify(uuid, handler)
                subscribed = uuid
                break
            except Exception as e:
                print(f'  尝试订阅 {uuid} 失败: {e}')
        if subscribed is None:
            print('订阅失败，请用 wit_ble_live.py --list-services 核实 UUID。')
            return []
        print(f'已订阅 {subscribed}，采集 {args.duration:.0f} 秒...\n')
        try:
            await asyncio.sleep(args.duration)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print('用户中断，提前结束采集。')
        finally:
            try:
                await client.stop_notify(subscribed)
            except Exception:
                pass

    return rows


# ── 分析核心 ─────────────────────────────────────────────────────────────────

def analyse(rows: list[dict]) -> dict:
    """
    阶段1：计算每帧偏移，做线性拟合，判断漂移匀速性。
    返回分析结果字典。
    """
    if len(rows) < 2:
        return {}

    pc0   = rows[0]['pc_ms']
    chip0 = rows[0]['chip_dt']

    elapsed_s  = []
    offset_ms  = []

    for r in rows:
        e = (r['pc_ms'] - pc0) / 1000.0                         # 经过秒数（PC基准）
        chip_elapsed_ms = (r['chip_dt'] - chip0).total_seconds() * 1000.0
        pc_elapsed_ms   = r['pc_ms'] - pc0
        off = pc_elapsed_ms - chip_elapsed_ms                    # 随时间累积的偏移
        elapsed_s.append(e)
        offset_ms.append(off)
        r['elapsed_s']  = e
        r['offset_ms']  = off

    slope_ms_per_s, intercept, r2 = linear_fit(elapsed_s, offset_ms)
    slope_ms_per_min = slope_ms_per_s * 60.0
    ppm = slope_ms_per_s / 1.0 * 1_000_000   # ms/s → ppm

    # 实际 BLE 传输延迟（第一帧 PC 比芯片早多少毫秒，近似恒定）
    initial_offsets = [(r['pc_ms'] - pc0) - (r['chip_dt'] - chip0).total_seconds() * 1000
                       for r in rows[:min(20, len(rows))]]
    ble_latency_ms  = sum(initial_offsets) / len(initial_offsets)

    residuals = [off - (slope_ms_per_s * e + intercept) for e, off in zip(elapsed_s, offset_ms)]
    res_stats = stats(residuals)

    return {
        'rows'            : rows,
        'elapsed_s'       : elapsed_s,
        'offset_ms'       : offset_ms,
        'slope_ms_per_s'  : slope_ms_per_s,
        'slope_ms_per_min': slope_ms_per_min,
        'intercept'       : intercept,
        'r2'              : r2,
        'ppm'             : ppm,
        'ble_latency_ms'  : ble_latency_ms,
        'residuals'       : residuals,
        'res_stats'       : res_stats,
        'n'               : len(rows),
        'duration_s'      : elapsed_s[-1],
    }


def apply_compensation(result: dict) -> list[float]:
    """
    阶段2：用拟合出的漂移率对每帧做线性补偿，
    返回补偿后的 offset_ms 列表（理想值应接近常数）。
    """
    slope  = result['slope_ms_per_s']
    interc = result['intercept']
    rows   = result['rows']
    pc0    = rows[0]['pc_ms']
    chip0  = rows[0]['chip_dt']

    compensated_offsets = []
    for r in rows:
        e      = r['elapsed_s']
        # 把拟合的漂移趋势从 offset 中减去，相当于把芯片时间往前拨
        corrected_chip_elapsed_ms = (r['chip_dt'] - chip0).total_seconds() * 1000.0 \
                                    + slope * e + interc
        pc_elapsed_ms = r['pc_ms'] - pc0
        compensated_offsets.append(pc_elapsed_ms - corrected_chip_elapsed_ms)
    return compensated_offsets


# ── 打印报告 ──────────────────────────────────────────────────────────────────

def print_phase1(result: dict):
    n   = result['n']
    dur = result['duration_s']
    slope_min = result['slope_ms_per_min']
    ppm       = result['ppm']
    r2        = result['r2']
    rs        = result['res_stats']

    print('=' * 60)
    print('阶段1：漂移测量结果')
    print('=' * 60)
    print(f'  采集帧数:           {n} 帧')
    print(f'  采集时长:           {dur:.1f} 秒')
    print(f'  采样率:             {n/dur:.1f} Hz')
    print()
    print(f'  漂移率:             {slope_min:+.3f} ms/min  ({ppm:+.1f} ppm)')
    print(f'  推算 1小时误差:     {slope_min*60:+.0f} ms')
    print(f'  推算 1天误差:       {slope_min*60*24/1000:+.2f} 秒')
    print()
    print(f'  线性度 R²:          {r2:.6f}')
    if r2 >= 0.999:
        linearity = '✓ 漂移高度匀速，线性补偿效果极佳'
    elif r2 >= 0.99:
        linearity = '✓ 漂移基本匀速，线性补偿效果良好'
    elif r2 >= 0.95:
        linearity = '⚠ 漂移有轻微非线性（温度变化？），补偿后仍有残余误差'
    else:
        linearity = '✗ 漂移非线性严重，线性补偿效果有限，建议分段补偿'
    print(f'  线性评估:           {linearity}')
    print()
    print(f'  拟合残差 std:       {rs.get("std", 0):.2f} ms  '
          f'(范围 {rs.get("min", 0):+.1f} ~ {rs.get("max", 0):+.1f} ms)')
    print('  （残差反映 BLE 传输抖动 + 非线性分量）')


def print_phase3(comp_offsets: list[float], original_res_std: float):
    s = stats(comp_offsets)
    print()
    print('=' * 60)
    print('阶段3：补偿后漂移再评估')
    print('=' * 60)
    print(f'  补偿后偏移均值:     {s["mean"]:+.2f} ms  ← 剩余 BLE 延迟（正常）')
    print(f'  补偿后偏移范围:     {s["min"]:+.1f} ~ {s["max"]:+.1f} ms')
    print(f'  补偿后偏移 std:     {s["std"]:.2f} ms')
    print()
    improvement = (1 - s['std'] / original_res_std) * 100 if original_res_std > 0 else 0
    # 补偿后的"漂移趋势"应该近似消失——用首尾偏移差来评估残余趋势
    trend_ms_per_min = 0.0
    if len(comp_offsets) > 1:
        # 对补偿后偏移再做一次线性拟合，看残余斜率
        xs = list(range(len(comp_offsets)))
        slope2, _, r2_2 = linear_fit(xs, comp_offsets)
        # xs 单位是帧序号，转换成 ms/min 需要帧率
        # 暂时只报告偏移变化量
        trend_total_ms = comp_offsets[-1] - comp_offsets[0]
        trend_ms_per_min = trend_total_ms / (len(comp_offsets) / 50.0) * 60.0  # 假设50Hz
    print(f'  补偿后残余漂移:     {trend_ms_per_min:+.2f} ms/min（理想值 ≈ 0）')
    print()
    if s['std'] < 5.0:
        print('  ✓ 补偿效果优秀：时间戳误差已收敛到 BLE 抖动水平（<5ms std）')
    elif s['std'] < 20.0:
        print('  ✓ 补偿效果良好：残余误差主要来自 BLE 传输抖动')
    else:
        print('  ⚠ 仍有较大残余误差，可能漂移存在非线性分量')


# ── 可选图表 ──────────────────────────────────────────────────────────────────

def plot_results(result: dict, comp_offsets: list[float]):
    try:
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
    except ImportError:
        print('\n提示: 安装 matplotlib 可显示漂移趋势图: pip install matplotlib')
        return

    elapsed = result['elapsed_s']
    raw_off = result['offset_ms']
    slope   = result['slope_ms_per_s']
    interc  = result['intercept']
    fit_line = [slope * e + interc for e in elapsed]

    fig, axes = plt.subplots(2, 1, figsize=(12, 8))
    fig.suptitle('WitMotion WT901 时间漂移分析', fontsize=14)

    # 上图：原始偏移 + 线性拟合
    ax1 = axes[0]
    ax1.scatter(elapsed, raw_off, s=1, alpha=0.4, color='steelblue', label='原始偏移')
    ax1.plot(elapsed, fit_line, color='red', linewidth=1.5,
             label=f'线性拟合  斜率={result["slope_ms_per_min"]:+.2f}ms/min  R²={result["r2"]:.5f}')
    ax1.set_xlabel('经过时间 (秒)')
    ax1.set_ylabel('偏移 PC−chip (ms)')
    ax1.set_title('阶段1：漂移测量（原始）')
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # 下图：补偿后偏移
    ax2 = axes[1]
    ax2.scatter(elapsed, comp_offsets, s=1, alpha=0.4, color='green', label='补偿后偏移')
    s = stats(comp_offsets)
    ax2.axhline(s['mean'], color='orange', linewidth=1.5,
                label=f'均值 {s["mean"]:+.1f}ms  std={s["std"]:.2f}ms')
    ax2.axhline(s['mean'] + s['std'], color='orange', linewidth=0.8, linestyle='--')
    ax2.axhline(s['mean'] - s['std'], color='orange', linewidth=0.8, linestyle='--')
    ax2.set_xlabel('经过时间 (秒)')
    ax2.set_ylabel('补偿后偏移 (ms)')
    ax2.set_title('阶段3：补偿后再评估')
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.show()


# ── CSV 输出 ──────────────────────────────────────────────────────────────────

def save_csv(path: str, result: dict, comp_offsets: list[float]):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['elapsed_s', 'chip_time', 'pc_recv_time',
                    'offset_ms', 'fit_ms', 'residual_ms', 'compensated_offset_ms',
                    'acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z'])
        slope  = result['slope_ms_per_s']
        interc = result['intercept']
        for i, r in enumerate(result['rows']):
            e    = r['elapsed_s']
            fit  = slope * e + interc
            res  = result['residuals'][i]
            comp = comp_offsets[i]
            pc_str = datetime.fromtimestamp(r['pc_ms'] / 1000.0).strftime('%Y-%m-%d %H:%M:%S.') + \
                     f'{int(r["pc_ms"]) % 1000:03d}'
            acc  = r['acc']
            gyro = r['gyro']
            w.writerow([f'{e:.3f}', r['chip_str'], pc_str,
                        f'{r["offset_ms"]:.3f}', f'{fit:.3f}', f'{res:.3f}', f'{comp:.3f}',
                        f'{acc[0]:.6f}', f'{acc[1]:.6f}', f'{acc[2]:.6f}',
                        f'{gyro[0]:.6f}', f'{gyro[1]:.6f}', f'{gyro[2]:.6f}'])
    print(f'\n详细数据已保存: {path}')
    print('列说明: elapsed_s=经过秒数  offset_ms=原始PC-chip偏移  '
          'fit_ms=拟合值  residual_ms=残差  compensated_offset_ms=补偿后偏移')


# ── 主逻辑 ───────────────────────────────────────────────────────────────────

async def run(args):
    # ── 阶段1：采集 ─────────────────────────────────────────
    print('【阶段1】开始采集，评估时间漂移...\n')
    rows = await collect(args)
    if len(rows) < 10:
        print('采集到的帧数太少（<10），无法分析。')
        return

    result = analyse(rows)
    print_phase1(result)

    # ── 阶段2：补偿 ─────────────────────────────────────────
    print()
    print('=' * 60)
    print('阶段2：应用线性漂移补偿')
    print('=' * 60)
    comp_offsets = apply_compensation(result)
    print(f'  补偿公式: corrected_chip_elapsed += {result["slope_ms_per_s"]:+.6f}ms/s × t '
          f'+ {result["intercept"]:+.3f}ms')
    print(f'  已对 {result["n"]} 帧应用补偿。')

    # ── 阶段3：再评估 ────────────────────────────────────────
    print_phase3(comp_offsets, result['res_stats'].get('std', 1.0))

    # ── 可选输出 ─────────────────────────────────────────────
    if args.output:
        save_csv(args.output, result, comp_offsets)

    if args.plot:
        plot_results(result, comp_offsets)


def main():
    ap = argparse.ArgumentParser(
        description='WitMotion WT901 时间漂移分析：测量漂移 → 线性补偿 → 验证效果',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--name', default='WT901',
                    help='按名称关键字查找设备（忽略大小写），默认 "WT901"')
    ap.add_argument('--address', default=None,
                    help='直接按 MAC 地址连接（优先于 --name）')
    ap.add_argument('--scan-timeout', type=float, default=8.0,
                    help='扫描超时（秒），默认 8')
    ap.add_argument('--duration', type=float, default=60.0,
                    help='采集时长（秒），越长漂移估算越准，建议 ≥60 秒，默认 60')
    ap.add_argument('--notify-uuid', default=None,
                    help='手动指定 Notify 特征值 UUID（不指定则自动尝试）')
    ap.add_argument('-o', '--output', default=None,
                    help='把逐帧详细数据保存到 CSV 文件（含原始偏移、拟合值、补偿后偏移）')
    ap.add_argument('--plot', action='store_true',
                    help='采集结束后弹出漂移趋势图（需要 matplotlib）')
    args = ap.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
