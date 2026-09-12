#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蓝牙容量测试：一次连上 N 个 IMU，看适配器扛不扛得住
================================================

要回答的问题只有一个：「每只狗的两个 IMU 一起采」到底行不行——
狗场 12 个、影棚 8 个同时连着，还能不能各自稳稳跑到 50Hz。

为什么不直接开一次录制来测：录制会同时占用 7 路 USB 摄像头，测出来的
掉帧/掉包分不清是蓝牙不行还是 USB 带宽被摄像头挤掉了。这个工具只连蓝牙，
不开摄像头、不写视频、不落 NAS 形状的文件名，测完 Ctrl-C 就走，
不会留下任何会被误传到 NAS 的数据。

用法:
    # 按场地一次测全（当班 + 备用），这是最常用的
    SITE=狗场 ./check_ble_capacity.sh                # 12 个
    SITE=影棚 ./check_ble_capacity.sh --duration 900 # 8 个，跑 15 分钟

    # 也可以手动指定设备
    python check_ble_capacity.py --imu wit=WT1 --imu wit=WT2 --duration 600

    # 想知道「同时发起」和「错开发起」差多少（有些适配器只是受不了同时握手）
    python check_ble_capacity.py --imu ... --stagger 0    # 全部同时连
    python check_ble_capacity.py --imu ... --stagger 2    # 每隔 2 秒连一个

看什么:
    - 连上几个：连不上的那几个是硬上限，不是调参能解决的
    - 实测 Hz：每个设备各自的采样率。12 条链路抢同一个射频的连接间隔，
      掉率是共享的——如果只有一两个掉，那是那两个设备/距离的问题；
      如果普遍掉到 40Hz 以下，那就是适配器到顶了
    - 每分钟 Hz：只看平均值会漏掉「前 5 分钟好好的、第 6 分钟开始崩」这种，
      而无人值守是要跑一整夜的
    - 掉线次数：连上是一回事，连一整夜是另一回事

时长建议至少 10 分钟（--duration 600）。跑 1 分钟只能测出「连不连得上」，
测不出稳定性；真要放心，找个晚上跑一小时。

注意: 只支持 wit 设备（两个场地的配置目前都是 wit）。hicc 项圈要测的话用
check_device_worn.py，那边两种都支持。
"""

import argparse
import asyncio
import re
import sys
import time

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print('缺少 bleak，请先安装: pip install bleak')
    sys.exit(1)

from ble_utils import match_by_name
from wit_parse import DEFAULT_NOTIFY_CANDIDATES, StreamingByteBuffer, parse_one_packet

_MAC_RE = re.compile(r'^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$')

# 超过这个间隔没收到包就算一次「空档」。50Hz 正常间隔 20ms，BLE 连接间隔
# 抖动到 100~200ms 都算正常，1 秒是明显不对劲了。
GAP_THRESHOLD_S = 1.0


class DevStat:
    """一个设备整场测试的原始记录。所有统计都是事后从 stamps 算出来的。"""

    def __init__(self, ident: str, label: str):
        self.ident = ident
        self.label = label
        self.stamps: list[float] = []   # 每个数据包到达的时刻（相对 t0 的秒）
        self.connected_at: float | None = None
        self.disconnects = 0
        self.error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.stamps) > 0

    def hz(self, t_end: float) -> float:
        """实测采样率 = 包数 / 从第一包到结束的时长。

        分母用「第一包到结束」而不是整场时长：设备是错开发起连接的，
        用整场时长算会把还没连上的那段也算进分母，晚连的设备会被冤枉。
        """
        if len(self.stamps) < 2:
            return 0.0
        span = t_end - self.stamps[0]
        return len(self.stamps) / span if span > 0 else 0.0

    def per_minute_hz(self, t_end: float) -> list[float]:
        """每分钟的 Hz。只看平均值会漏掉「跑着跑着开始崩」，而这正是无人值守最怕的。"""
        if not self.stamps:
            return []
        buckets: dict[int, int] = {}
        for s in self.stamps:
            buckets[int(s // 60)] = buckets.get(int(s // 60), 0) + 1
        out = []
        for m in sorted(buckets):
            # 最后一分钟多半不满 60 秒，按实际长度折算，否则会显得低
            lo, hi = m * 60.0, min((m + 1) * 60.0, t_end)
            span = hi - max(lo, self.stamps[0])
            if span > 5.0:  # 太短的桶（刚连上那一小截）算出来没意义，跳过
                out.append(buckets[m] / span)
        return out

    def gaps(self) -> list[float]:
        """所有超过阈值的空档。同一次 notify 里的多个包时刻相同，diff=0，不影响。"""
        return [b - a for a, b in zip(self.stamps, self.stamps[1:]) if b - a > GAP_THRESHOLD_S]


def parse_imu_spec(spec: str) -> str:
    """跟其它脚本的 --imu 格式保持一致：类型=标识。这里只收 wit。"""
    if '=' not in spec:
        raise ValueError(f'--imu 格式应为 wit=标识，例如 wit=WT1 或 wit=D1:FD:A8:C7:1A:EF，收到: {spec!r}')
    dev_type, ident = spec.split('=', 1)
    dev_type = dev_type.strip().lower()
    if dev_type != 'wit':
        raise ValueError(
            f'这个工具只支持 wit 设备（两个场地的配置都是 wit），收到 {dev_type!r}。'
            f'hicc 项圈请用 check_device_worn.py。')
    return ident.strip()


async def scan_all(idents: list[str], timeout: float) -> dict[str, object]:
    """
    一次扫描把所有目标设备都解析出来，而不是每个设备各扫一次。

    12 个设备各扫一次，光扫描就要好几分钟，而且并发扫描本身就在抢适配器，
    会污染这次测试要测的东西。
    """
    seen: dict[str, object] = {}

    def _cb(dev, _adv):
        seen[dev.address.upper()] = dev

    def _resolve_all() -> dict[str, object]:
        out = {}
        for ident in idents:
            if _MAC_RE.match(ident):
                hit = [d for a, d in seen.items() if a == ident.upper()]
                if hit:
                    out[ident] = hit[0]
            else:
                target, _err = match_by_name(ident, [(d, 0.0) for d in seen.values()])
                if target is not None:
                    out[ident] = target
        return out

    print(f'扫描中，找这 {len(idents)} 个设备（最多 {timeout:.0f} 秒）...')
    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            if len(_resolve_all()) == len(idents):
                break
    finally:
        await scanner.stop()
    return _resolve_all()


async def run_one(ble_device, st: DevStat, t0: float, t_end_wall: float, delay: float):
    """连一个设备，订阅 notify，记录每个包的到达时刻，一直跑到全局收工时刻。"""
    await asyncio.sleep(delay)

    def _on_disconnect(_client):
        st.disconnects += 1

    buf = StreamingByteBuffer()

    def on_data(_sender, data: bytearray):
        now = time.monotonic() - t0
        for pkt in buf.feed(bytes(data)):
            if parse_one_packet(pkt) is not None:
                st.stamps.append(now)

    try:
        async with BleakClient(ble_device, disconnected_callback=_on_disconnect) as client:
            subscribed = None
            for uuid in DEFAULT_NOTIFY_CANDIDATES:
                try:
                    await client.start_notify(uuid, on_data)
                    subscribed = uuid
                    break
                except Exception:
                    continue
            if subscribed is None:
                st.error = '订阅 Notify 失败'
                return
            st.connected_at = time.monotonic() - t0
            print(f'  {st.label:<8} 已连接  +{st.connected_at:.1f}s')
            # 所有设备跑到同一个收工时刻，Hz 才好横向比
            remaining = t_end_wall - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            try:
                await client.stop_notify(subscribed)
            except Exception:
                pass
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 一个设备连不上不该把整场测试带崩
        st.error = f'{type(e).__name__}: {e}'
        print(f'  {st.label:<8} ✗ {st.error}')


async def monitor(stats: list[DevStat], t0: float, t_end_wall: float, target_hz: float):
    """每分钟报一次当前状态。跑一小时的话，总不能等到最后才知道第 7 分钟就崩了。"""
    last_counts = {st.label: 0 for st in stats}
    while time.time() < t_end_wall - 1:
        await asyncio.sleep(min(60.0, max(1.0, t_end_wall - time.time())))
        elapsed = time.monotonic() - t0
        live, rates = 0, []
        for st in stats:
            n = len(st.stamps)
            got = n - last_counts[st.label]
            last_counts[st.label] = n
            if got > 0:
                live += 1
                rates.append((got / 60.0, st.label))
        if rates:
            lo_hz, lo_label = min(rates)
            flag = '' if lo_hz >= target_hz * 0.9 else '   ← 偏低'
            print(f'  t={elapsed:>4.0f}s  在传 {live}/{len(stats)}   '
                  f'本分钟最低 {lo_hz:.1f}Hz ({lo_label}){flag}')
        else:
            print(f'  t={elapsed:>4.0f}s  在传 0/{len(stats)}   ← 全部没数据')


def _pad(s: str, width: int) -> str:
    """按终端显示宽度左对齐补空格。中文是双宽，用 len() padding 会歪一格。"""
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1 for ch in s)
    return s + ' ' * max(0, width - w)


def report(stats: list[DevStat], t_end: float, target_hz: float) -> int:
    """打结果表和判定。返回进程退出码：0 = 顶得住。"""
    # 列宽跟着实际标签走。狗场是 imu13/xiaojinmao 这种长标签，写死宽度会被撑歪
    lw = max([len(st.label) for st in stats] + [8]) + 2
    line = '═' * (lw + 52)
    print()
    print(line)
    print(_pad('设备', lw) + f'{"连上用时":>10}{"实测Hz":>9}{"最低分钟":>10}{"最长空档":>10}{"掉线":>6}  判定')
    print('─' * (lw + 52))

    problems = []
    for st in stats:
        if st.error is not None:
            print(_pad(st.label, lw) + f'{"—":>8}{"—":>9}{"—":>10}{"—":>10}{"—":>6}  ✗ {st.error}')
            problems.append(f'{st.label}（没连上）')
            continue
        if not st.stamps:
            print(_pad(st.label, lw) + f'{"—":>8}{"0.0":>9}{"—":>10}{"—":>10}{st.disconnects:>6}  ✗ 连上了但一个包都没收到')
            problems.append(f'{st.label}（无数据）')
            continue

        hz = st.hz(t_end)
        per_min = st.per_minute_hz(t_end)
        lo = min(per_min) if per_min else hz
        gaps = st.gaps()
        max_gap = max(gaps) if gaps else 0.0

        verdicts = []
        if hz < target_hz * 0.9:
            verdicts.append(f'Hz 偏低({hz:.1f})')
        if lo < target_hz * 0.8:
            verdicts.append(f'有分钟掉到 {lo:.0f}Hz')
        if st.disconnects:
            verdicts.append(f'掉线 {st.disconnects} 次')
        if max_gap > 5.0:
            verdicts.append(f'空档 {max_gap:.0f}s')

        mark = 'OK' if not verdicts else '!! ' + '、'.join(verdicts)
        if verdicts:
            # 摘要里写真正的毛病，不要一律显示 Hz——掉线的那台 Hz 可能是好的
            problems.append(f'{st.label}（{"、".join(verdicts)}）')
        print(_pad(st.label, lw) + f'{st.connected_at or 0:>7.1f}s{hz:>9.1f}{lo:>10.1f}'
                                   f'{max_gap:>9.2f}s{st.disconnects:>6}  {mark}')

    print(line)
    connected = sum(1 for st in stats if st.ok)
    print(f'连上 {connected}/{len(stats)}，目标 {target_hz:.0f}Hz（判定门槛：全程 ≥{target_hz * 0.9:.0f}Hz、不掉线）')
    print()

    if connected == len(stats) and not problems:
        print(f'判定：顶得住。{len(stats)} 个设备全部连上并稳定在 {target_hz:.0f}Hz 附近。')
        print('      → 可以考虑「每只狗两个 IMU 一起采」，换班就不用再改配置了。')
        print('      → 狗场正式录制加 ALL_DEVICES=1（影棚见 sites/影棚.env 里的说明）。')
        return 0

    print(f'判定：顶不住。有问题的：{"、".join(problems)}')
    print()
    print('接下来可以试的，按代价从低到高：')
    print('  1. 调 --stagger（错开发起连接）。有些适配器只是受不了同时握手，')
    print('     错开之后能连上。--stagger 0 和 --stagger 2 各跑一次对比。')
    print('  2. 把设备分两批，每批测一次，确认单批是好的——排除某个设备本身有问题。')
    print('  3. 换/加蓝牙适配器。注意 Windows 上 bleak 用的是系统默认适配器，')
    print('     插两个不等于能同时用两个，要先确认。')
    print('  4. 放弃「全采」，改成「开录前逐台探测、只录戴着的那 6 个」——')
    print('     蓝牙负载和今天完全一样。')
    return 1


async def main_async(args) -> int:
    try:
        idents = [parse_imu_spec(s) for s in args.imu]
    except ValueError as e:
        print(e)
        return 2
    labels = list(args.label) if args.label else []
    if labels and len(labels) != len(idents):
        print(f'--label 给了 {len(labels)} 个，--imu 给了 {len(idents)} 个，数量要一一对应')
        return 2
    if not labels:
        labels = [i for i in idents]

    print(f'蓝牙容量测试：{len(idents)} 个设备，跑 {args.duration:.0f} 秒')
    print()
    found = await scan_all(idents, args.scan_timeout)
    missing = [lab for ident, lab in zip(idents, labels) if ident not in found]
    print(f'  找到 {len(found)}/{len(idents)}' + (f'  ——没找到: {"、".join(missing)}' if missing else ''))
    if not found:
        print('\n一个都没扫到。确认设备开着、在范围内，或者名字/MAC 写对了。')
        return 2
    if missing:
        print('  （没扫到的这几个不参与测试。它们可能没开机、不在范围内，或者配置里的名字/MAC 过期了）')
    print()

    stats = [DevStat(ident, lab) for ident, lab in zip(idents, labels) if ident in found]
    t0 = time.monotonic()
    # 全局收工时刻：错开发起要花 stagger×N 秒，这段时间不该从测试时长里扣
    t_end_wall = time.time() + args.stagger * len(stats) + args.duration

    print(f'依次发起连接（间隔 {args.stagger:.1f} 秒）...')
    tasks = [asyncio.ensure_future(run_one(found[st.ident], st, t0, t_end_wall, i * args.stagger))
             for i, st in enumerate(stats)]
    mon = asyncio.ensure_future(monitor(stats, t0, t_end_wall, args.target_hz))
    print()
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        mon.cancel()

    return report(stats, time.monotonic() - t0, args.target_hz)


def main():
    ap = argparse.ArgumentParser(
        description='一次连上多个 IMU，测蓝牙适配器扛不扛得住（不开摄像头、不落盘）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='按场地一次测全：SITE=狗场 ./check_ble_capacity.sh')
    ap.add_argument('--imu', action='append', default=[], metavar='wit=标识',
                    help='设备，可重复。标识是名字（WT1）或 MAC')
    ap.add_argument('--label', action='append', default=[], metavar='名称',
                    help='报表里显示的名字，跟 --imu 一一对应（比如 imu9）。不给就显示标识本身')
    ap.add_argument('--duration', type=float, default=600.0,
                    help='测试时长（秒），默认 600。少于 600 只能测出连不连得上，测不出稳定性')
    ap.add_argument('--stagger', type=float, default=1.0,
                    help='错开发起连接的间隔（秒），默认 1。设 0 就是全部同时连')
    ap.add_argument('--scan-timeout', type=float, default=20.0, help='扫描超时（秒），默认 20')
    ap.add_argument('--target-hz', type=float, default=50.0, help='目标采样率，默认 50')
    args = ap.parse_args()

    if not args.imu:
        ap.error('至少要给一个 --imu。按场地一次测全用 ./check_ble_capacity.sh')

    try:
        sys.exit(asyncio.run(main_async(args)))
    except KeyboardInterrupt:
        print('\n\n中断了。已经收到的数据没有统计——要结果的话让它自己跑完。')
        sys.exit(130)


if __name__ == '__main__':
    main()
