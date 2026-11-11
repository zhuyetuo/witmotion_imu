#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蓝牙信号强度（RSSI）调试：这台电脑「听」到每个项圈有多响
========================================================

要回答的问题：狗在户外 / 在室内单间时，三台电脑各自收到的信号强度分别是多少。
量出来之后才能定「户外那台信号弱到多少就该放手、让电脑1/2 接回去」的门槛。

用法（一般走 check_rssi.sh，按场地自动带上全部项圈）：
    SITE=佛山博朗旺_狗场3 ./check_rssi.sh                     # 一直跑，Ctrl-C 停
    SITE=佛山博朗旺_狗场3 ./check_rssi.sh --note 户外          # 给这次测量打个标记
    SITE=佛山博朗旺_狗场1 ./check_rssi.sh --note 室内 --duration 300

    python check_rssi.py --imu wit=DC:32:39:07:0A:3D --label imu9/dongde

⚠ 只看得到「没被任何电脑连着」的项圈。
    项圈被连上之后就不再广播，谁也测不到它的信号强度（包括连着它的那台）。
    所以测的时候**三台电脑的录制都先停掉**（按 q），测完再开。
    屏幕上显示「没听到」的，多半是还被哪台连着，或者真的够不着。

看什么：
    - RSSI 单位 dBm，是负数，**越接近 0 越强**：-50 很强，-70 一般，-85 以下快够不着了。
    - 信号会跳，看「最近 10 秒的中位数」，别看单次。
    - 同一时刻在三台电脑上一起跑（各自 --note 写上自己在哪），对比同一个项圈
      在户外那台和单间那台上差多少，门槛就定在两者中间。

每一条都记进 logs/rssi/ 下的 CSV（时间、项圈、MAC、RSSI、note），回头可以再看。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import socket
import statistics
import sys
import time
from collections import deque
from datetime import datetime

_MAC_RE = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')

WINDOW_SEC = 10.0


class RssiStat:
    """一个项圈的信号记录：最近 WINDOW_SEC 秒滑窗 + 全程。"""

    def __init__(self, mac: str, label: str):
        self.mac = mac.upper()
        self.label = label
        self.recent: deque = deque()     # (t, rssi)
        self.all: list[int] = []
        self.last_t: float | None = None

    def add(self, t: float, rssi: int):
        self.recent.append((t, rssi))
        self.all.append(rssi)
        self.last_t = t
        self.trim(t)

    def trim(self, now: float):
        while self.recent and now - self.recent[0][0] > WINDOW_SEC:
            self.recent.popleft()

    def recent_median(self) -> float | None:
        vals = [r for _, r in self.recent]
        return statistics.median(vals) if vals else None


def summarize(vals: list[int]) -> dict | None:
    """全程统计：中位数、10%/90% 分位、最强最弱。"""
    if not vals:
        return None
    s = sorted(vals)

    def pct(p: float) -> int:
        return s[min(len(s) - 1, max(0, round(p * (len(s) - 1))))]

    return {'n': len(s), 'median': statistics.median(s), 'p10': pct(0.10), 'p90': pct(0.90),
            'min': s[0], 'max': s[-1]}


def bar(rssi: float | None) -> str:
    """-100 → 空，-40 → 满。终端里一眼看强弱。"""
    if rssi is None:
        return ''
    n = int(max(0, min(20, (rssi + 100) / 3)))
    return '#' * n


def strength_word(rssi: float | None) -> str:
    if rssi is None:
        return ''
    if rssi >= -60:
        return '很强'
    if rssi >= -70:
        return '强'
    if rssi >= -80:
        return '一般'
    if rssi >= -88:
        return '弱'
    return '快够不着'


def render(stats: list[RssiStat], now: float, t0: float, note: str) -> str:
    lines = [f'蓝牙信号强度  {datetime.now():%H:%M:%S}  已跑 {now - t0:.0f}s'
             + (f'  [{note}]' if note else '') + '   （Ctrl-C 停止并出汇总）',
             f'{"项圈":<24s}{"10秒中位":>8s}  {"最近":>5s}  {"多久前":>6s}  强弱',
             '-' * 72]
    for st in stats:
        st.trim(now)
        med = st.recent_median()
        if st.last_t is None:
            lines.append(f'{st.label:<24s}{"--":>8s}  {"--":>5s}  {"--":>6s}  没听到（被某台连着？或够不着）')
            continue
        last = st.recent[-1][1] if st.recent else None
        ago = now - st.last_t
        if med is None:
            lines.append(f'{st.label:<24s}{"--":>8s}  {"--":>5s}  {ago:5.0f}s  这 {WINDOW_SEC:.0f} 秒没听到（刚被连上？走远了？）')
        else:
            lines.append(f'{st.label:<24s}{med:8.0f}  {last:5d}  {ago:5.0f}s  {bar(med):<20s} {strength_word(med)}')
    return '\n'.join(lines)


def final_report(stats: list[RssiStat], note: str) -> str:
    lines = ['', '=' * 72, '汇总（全程，单位 dBm，越接近 0 越强）' + (f'  [{note}]' if note else ''),
             f'{"项圈":<24s}{"次数":>6s}{"中位":>7s}{"10%":>7s}{"90%":>7s}{"最弱":>7s}{"最强":>7s}']
    for st in stats:
        s = summarize(st.all)
        if s is None:
            lines.append(f'{st.label:<24s}  一次都没听到')
        else:
            lines.append(f'{st.label:<24s}{s["n"]:6d}{s["median"]:7.0f}{s["p10"]:7d}{s["p90"]:7d}'
                         f'{s["min"]:7d}{s["max"]:7d}')
    return '\n'.join(lines)


async def run(stats: list[RssiStat], duration: float, refresh: float, note: str, log_path: str):
    from bleak import BleakScanner

    by_mac = {st.mac: st for st in stats}
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    new_file = not os.path.exists(log_path)
    f = open(log_path, 'a', newline='', encoding='utf-8-sig')
    w = csv.writer(f)
    if new_file:
        w.writerow(['time', 'host', 'label', 'mac', 'rssi', 'note'])
    host = socket.gethostname()

    def _cb(dev, adv):
        st = by_mac.get((dev.address or '').upper())
        if st is None:
            return
        rssi = getattr(adv, 'rssi', None)
        if rssi is None:
            return
        st.add(time.monotonic(), int(rssi))
        w.writerow([datetime.now().isoformat(timespec='milliseconds'), host, st.label, st.mac, int(rssi), note])

    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    t0 = time.monotonic()
    try:
        while duration <= 0 or time.monotonic() - t0 < duration:
            await asyncio.sleep(refresh)
            f.flush()
            # 清屏重画（Git Bash / Windows Terminal 都认 ANSI）
            sys.stdout.write('\033[H\033[J' + render(stats, time.monotonic(), t0, note) + '\n')
            sys.stdout.flush()
    finally:
        await scanner.stop()
        f.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--imu', action='append', default=[], metavar='wit=MAC', help='要看的项圈（MAC），可重复')
    ap.add_argument('--label', action='append', default=[], metavar='名称', help='跟 --imu 一一对应的显示名')
    ap.add_argument('--duration', type=float, default=0, help='跑多少秒，默认 0 = 一直跑到 Ctrl-C')
    ap.add_argument('--refresh', type=float, default=1.0, help='屏幕刷新间隔（秒），默认 1')
    ap.add_argument('--note', default='', help='这次测量的标记，比如 户外 / 室内 / 狗在1号单间，会写进 CSV')
    ap.add_argument('--log-dir', default='logs/rssi', help='CSV 存哪，默认 logs/rssi')
    args = ap.parse_args()

    stats: list[RssiStat] = []
    for i, spec in enumerate(args.imu):
        ident = spec.split('=', 1)[-1].strip()
        if not _MAC_RE.match(ident):
            print(f'信号强度调试只认 MAC（wit=AA:BB:..），收到: {spec!r}')
            sys.exit(2)
        label = args.label[i] if i < len(args.label) else ident
        stats.append(RssiStat(ident, label))
    if not stats:
        print('没有要看的项圈：用 --imu wit=MAC，或者 SITE=... ./check_rssi.sh')
        sys.exit(2)

    log_path = os.path.join(args.log_dir, f'rssi_{socket.gethostname()}_{datetime.now():%Y%m%d}.csv')
    print(f'记录写到 {log_path}')
    print('提醒：项圈被任何一台电脑连着就不广播，测不到。三台的录制先停掉再测。')
    try:
        asyncio.run(run(stats, args.duration, args.refresh, args.note, log_path))
    except KeyboardInterrupt:
        pass
    print(final_report(stats, args.note))
    print(f'\n明细：{log_path}')


if __name__ == '__main__':
    main()
