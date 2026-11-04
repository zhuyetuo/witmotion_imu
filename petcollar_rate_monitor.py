# -*- coding: utf-8 -*-
"""
自制项圈（HICC_PetCollar 行为识别固件，协议 v2.0）实时打印 IMU 原始采样，并实时评估采样率
=====================================================================================

要回答两个问题：
  1. 采样率到底是不是 31.25 Hz（算法按 31.25 Hz 训的，差了窗口时长就不对）
  2. 每一帧的间隔稳不稳——是均匀的 32 ms，还是一会长一会短（比如 0 / 64 ms 交替、
     几帧挤在同一时刻然后空一大截）

数据来源：固件 PB_RAW_STREAM=1 时，TX2（0203）上的 cmd 0x08 原始采样镜像——
交给 libpetbehavior 的同一份样本：DP 0x0A 时间戳（设备 ms）+ 0x17/0x18/0x19 三轴加速度（g×1e6）。
正式固件（协议 v2.0 文档）不发 0x08，这个工具连上会提示收不到。

**两个时间，分开看**：
  · 设备时间戳的间隔 = 传感器真正的采样节奏（这才是「是不是 31.25 Hz、均不均匀」）
  · 主机收到的时间 = 蓝牙送达节奏。BLE 按连接间隔（15~30 ms）成批送，主机这边本来就是
    一会挤一会空，**不代表采样不均匀**，只用来看有没有丢、送达速率跟得上不

跟老的 hicc_ble_live.py（协议 v1.1，cmd 0x05 六轴 25 Hz、RX 是 0203）是两套固件，互不影响。

用法:
    python petcollar_rate_monitor.py                       # 按名字扫描连接，逐帧打印 + 每 5 秒一份评估
    python petcollar_rate_monitor.py --address EA:CB:30:12:30:25
    python petcollar_rate_monitor.py --quiet               # 不逐帧打印，只看评估
    python petcollar_rate_monitor.py --every 10            # 每 10 帧打印一行
    python petcollar_rate_monitor.py --duration 120 --csv rate_test.csv
    python petcollar_rate_monitor.py --expect-hz 31.25 --window 10

依赖: pip install bleak
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import statistics
import sys
import time
from datetime import datetime, timezone

DEVICE_NAME = "HICC_PetCollar"
UUID_TX = "a6ed0202-d344-460a-8075-b9e8ec90d71b"    # Notify: 0x06 / 0x07 / 0x09
UUID_TX2 = "a6ed0203-d344-460a-8075-b9e8ec90d71b"   # Notify: 0x08 原始采样镜像（调试固件才有）
UUID_RX = "a6ed0204-d344-460a-8075-b9e8ec90d71b"    # Write : 校时应答 / 0x0A 确认

HEAD0, HEAD1, VER = 0x55, 0xAA, 0x00
CMD_TIME, CMD_BEHAVIOR, CMD_RAW, CMD_SEGMENT, CMD_ACK = 0x06, 0x07, 0x08, 0x09, 0x0A
DP_TS, DP_CLS, DP_CONF, DP_AX, DP_AY, DP_AZ = 0x0A, 0x30, 0x31, 0x17, 0x18, 0x19
BEHAV_NAMES = {0: "睡觉", 1: "未佩戴", 2: "活动", 3: "抓挠"}


# ── 协议 ──────────────────────────────────────────────────────────────────

def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def parse_frame(b: bytes):
    """校验帧头 / 版本 / 长度 / 校验和，通过返回 (cmd, payload)，否则 None"""
    if len(b) < 7 or b[0] != HEAD0 or b[1] != HEAD1 or b[2] != VER:
        return None
    dlen = (b[4] << 8) | b[5]
    if len(b) < 7 + dlen or checksum(b[:6 + dlen]) != b[6 + dlen]:
        return None
    return b[3], b[6:6 + dlen]


def parse_dps(payload: bytes) -> dict:
    """DP 单元：dpid(1) type(1) len(2,BE) value。0x02 = 大端有符号整数，0x04 = 单字节枚举"""
    out, pos = {}, 0
    while pos + 4 <= len(payload):
        dpid, dtype = payload[pos], payload[pos + 1]
        dl = (payload[pos + 2] << 8) | payload[pos + 3]
        raw = payload[pos + 4:pos + 4 + dl]
        pos += 4 + dl
        if dtype == 0x02 and raw:
            out[dpid] = int.from_bytes(raw, "big", signed=True)
        elif dtype == 0x04 and raw:
            out[dpid] = raw[0]
    return out


def build_frame(cmd: int, body: bytes) -> bytes:
    f = bytes([HEAD0, HEAD1, VER, cmd, (len(body) >> 8) & 0xFF, len(body) & 0xFF]) + body
    return f + bytes([checksum(f)])


def build_time_frame(now: datetime | None = None) -> bytes:
    """校时应答：按 UTC 下发（协议 v2.0 §6.3）"""
    n = now or datetime.now(timezone.utc)
    return build_frame(CMD_TIME, bytes([0x01, (n.year - 2000) & 0xFF, n.month, n.day, n.hour, n.minute, n.second]))


def build_ack_frame(seq: int) -> bytes:
    return build_frame(CMD_ACK, seq.to_bytes(4, "big"))


# ── 采样率 / 间隔评估（纯函数，单测用）────────────────────────────────────

def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def assess(dev_ts_ms: list[int], host_ts_s: list[float], expect_hz: float, tol: float = 0.02) -> dict:
    """一段样本的评估。dev_ts_ms：设备时间戳（毫秒）；host_ts_s：主机收到的时间（单调秒）。

    采样率按「(样本数-1) / 首尾时长」算，不是间隔倒数的平均（一个 0 ms 间隔会让后者爆掉）。
    间隔的稳定性看：标准差、p5/p95、落在期望间隔 ±10% 里的占比、重复时间戳（0 ms）、
    大缝（> 1.5 倍期望）、长短交替（相邻两个间隔一个偏长一个偏短的次数）。
    """
    exp_ms = 1000.0 / expect_hz
    n = len(dev_ts_ms)
    out: dict = {"n": n, "expect_hz": expect_hz, "expect_ms": exp_ms}
    if n < 3:
        out["verdict"] = "样本太少"
        return out
    iv = [b - a for a, b in zip(dev_ts_ms, dev_ts_ms[1:])]
    span = (dev_ts_ms[-1] - dev_ts_ms[0]) / 1000.0
    rate = (n - 1) / span if span > 0 else float("nan")
    pos = [x for x in iv if x > 0]
    within = sum(1 for x in iv if abs(x - exp_ms) <= exp_ms * 0.10)
    dup = sum(1 for x in iv if x <= 0)
    back = sum(1 for x in iv if x < 0)
    gaps = [x for x in iv if x > exp_ms * 1.5]
    # 长短交替：相邻两个间隔一个 > 期望 +15%，另一个 < 期望 -15%
    alt = sum(1 for a, b in zip(iv, iv[1:])
              if (a > exp_ms * 1.15 and b < exp_ms * 0.85) or (a < exp_ms * 0.85 and b > exp_ms * 1.15))
    out.update({
        "rate_hz": rate, "span_s": span,
        "iv_mean": statistics.fmean(iv), "iv_std": statistics.pstdev(iv) if len(iv) > 1 else 0.0,
        "iv_min": min(iv), "iv_max": max(iv), "iv_p5": pct(iv, 0.05), "iv_p95": pct(iv, 0.95),
        "iv_median": statistics.median(iv),
        "within10_pct": within / len(iv) * 100, "dup": dup, "backwards": back,
        "gaps": len(gaps), "gap_ms_max": max(gaps) if gaps else 0, "alternating": alt,
        "distinct_iv": sorted({int(round(x)) for x in iv}),
        "pos_mean": statistics.fmean(pos) if pos else float("nan"),
    })
    if len(host_ts_s) >= 2 and host_ts_s[-1] > host_ts_s[0]:
        hiv = [(b - a) * 1000 for a, b in zip(host_ts_s, host_ts_s[1:])]
        out["host_rate_hz"] = (len(host_ts_s) - 1) / (host_ts_s[-1] - host_ts_s[0])
        out["host_iv_std"] = statistics.pstdev(hiv) if len(hiv) > 1 else 0.0

    problems = []
    if abs(rate - expect_hz) > expect_hz * tol:
        problems.append(f"采样率 {rate:.2f} Hz，跟 {expect_hz:g} 差 {(rate / expect_hz - 1) * 100:+.1f}%")
    if out["within10_pct"] < 95:
        problems.append(f"只有 {out['within10_pct']:.0f}% 的间隔在 {exp_ms:.1f}±10% ms 内")
    if dup:
        problems.append(f"{dup} 次时间戳没走（0 ms 间隔）{'，其中倒退 %d 次' % back if back else ''}")
    if gaps:
        problems.append(f"{len(gaps)} 处大缝（最长 {max(gaps):.0f} ms）")
    if alt >= max(3, len(iv) * 0.05):
        problems.append(f"长短交替 {alt} 次（一会长一会短）")
    out["problems"] = problems
    out["verdict"] = "正常" if not problems else "有问题"
    return out


def fmt_assess(a: dict, title: str) -> str:
    if "rate_hz" not in a:
        return f"── {title}：{a.get('verdict')}（{a.get('n', 0)} 个样本）"
    lines = [
        f"── {title}：{a['n']} 个样本 / {a['span_s']:.1f} s ── {'✓ ' if a['verdict'] == '正常' else '✗ '}{a['verdict']}",
        f"   采样率（设备时间戳）  {a['rate_hz']:.3f} Hz   期望 {a['expect_hz']:g} Hz（间隔 {a['expect_ms']:.2f} ms）",
        f"   间隔 ms  均值 {a['iv_mean']:.2f}  中位 {a['iv_median']:.1f}  标准差 {a['iv_std']:.2f}  "
        f"p5 {a['iv_p5']:.1f}  p95 {a['iv_p95']:.1f}  最小 {a['iv_min']:.0f}  最大 {a['iv_max']:.0f}",
        f"   在 ±10% 内 {a['within10_pct']:.1f}%   0ms {a['dup']}   大缝 {a['gaps']}   长短交替 {a['alternating']}   "
        f"出现过的间隔 {a['distinct_iv'][:12]}{'…' if len(a['distinct_iv']) > 12 else ''}",
    ]
    if "host_rate_hz" in a:
        lines.append(f"   主机收到 {a['host_rate_hz']:.2f} Hz（蓝牙成批送，间隔标准差 {a['host_iv_std']:.1f} ms 是正常的，不代表采样不匀）")
    for p in a["problems"]:
        lines.append(f"   ! {p}")
    return "\n".join(lines)


# ── BLE ───────────────────────────────────────────────────────────────────

async def find_device(address: str | None, rounds: int = 3):
    from bleak import BleakScanner

    for i in range(rounds):
        print(f"扫描 {address or DEVICE_NAME}（{i + 1}/{rounds}）…")
        found = await BleakScanner.discover(timeout=8.0, return_adv=True)
        cand = [(d, adv) for d, adv in found.values()
                if (address and d.address.upper() == address.upper())
                or (not address and (adv.local_name or d.name or "") == DEVICE_NAME)]
        if cand:
            d, adv = max(cand, key=lambda t: t[1].rssi)
            print(f"  找到 {d.address}  rssi {adv.rssi} dBm")
            return d
    return None


async def run(args) -> int:
    from bleak import BleakClient

    dev = await find_device(args.address)
    if dev is None:
        print("没找到设备：确认上电在广播；信号弱时用 --address 指定 MAC")
        return 2

    dev_ts: list[int] = []        # 当前评估窗口
    host_ts: list[float] = []
    all_dev: list[int] = []       # 整次会话（结束时总评）
    all_host: list[float] = []
    state = {"n": 0, "last_ts": None, "behav": 0, "segs": 0, "last_seq": 0}
    writer = None
    fcsv = None
    if args.csv:
        fcsv = open(args.csv, "w", newline="", encoding="utf-8")
        writer = csv.writer(fcsv)
        writer.writerow(["t_dev_ms", "dt_ms", "ax_g", "ay_g", "az_g", "t_host_ms"])

    client = BleakClient(dev, timeout=30.0)

    async def send(frame: bytes):
        try:
            await client.write_gatt_char(UUID_RX, frame, response=False)
        except Exception as e:  # noqa: BLE001
            print(f"  ! 写 RX 失败：{e}")

    async def on_notify(_c, data: bytearray):
        p = parse_frame(bytes(data))
        if not p:
            return
        cmd, payload = p
        if cmd == CMD_TIME:
            await send(build_time_frame())
            print("  -> 回了校时（UTC）")
            return
        if cmd == CMD_RAW:
            d = parse_dps(payload)
            ts = d.get(DP_TS, 0)
            ax, ay, az = d.get(DP_AX, 0) / 1e6, d.get(DP_AY, 0) / 1e6, d.get(DP_AZ, 0) / 1e6
            mono = time.monotonic()
            dt = ts - state["last_ts"] if state["last_ts"] is not None else None
            # 时间戳大跳（校时把时钟拨了）：评估重新开始，不把这一下算成缝
            if dt is not None and abs(dt) > 10_000:
                print(f"  （设备时间跳了 {dt / 1000:+.1f} s，多半是刚校时，重新开始统计）")
                dev_ts.clear()
                host_ts.clear()
                dt = None
            state["last_ts"] = ts
            state["n"] += 1
            dev_ts.append(ts)
            host_ts.append(mono)
            all_dev.append(ts)
            all_host.append(mono)
            if writer:
                writer.writerow([ts, "" if dt is None else dt, f"{ax:.4f}", f"{ay:.4f}", f"{az:.4f}", int(time.time() * 1000)])
            if not args.quiet and state["n"] % args.every == 0:
                exp = 1000.0 / args.expect_hz
                mark = "" if dt is None or abs(dt - exp) <= exp * 0.10 else ("  ← 0ms" if dt == 0 else "  ← 偏长" if dt > exp else "  ← 偏短")
                tstr = datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S.%f")[:-3] if ts > 1e12 else f"{ts} ms"
                print(f"#{state['n']:<7} {tstr}  Δ{'   -  ' if dt is None else f'{dt:5d}'} ms  "
                      f"ax {ax:+.3f}  ay {ay:+.3f}  az {az:+.3f} g{mark}")
            return
        if cmd == CMD_BEHAVIOR and args.show_behavior:
            d = parse_dps(payload)
            print(f"  [当前行为] {BEHAV_NAMES.get(d.get(DP_CLS, -1), '?')}  {d.get(DP_CONF, 0)}%")
            return
        if cmd == CMD_SEGMENT and payload:
            n = payload[0]
            seqs = [int.from_bytes(payload[1 + i * 13:5 + i * 13], "big") for i in range(n) if 1 + i * 13 + 13 <= len(payload)]
            if seqs:
                state["segs"] += len(seqs)
                state["last_seq"] = max(state["last_seq"], *seqs)
                await send(build_ack_frame(state["last_seq"]))   # 确认掉，免得一直补发刷屏

    try:
        await client.connect()
        print(f"已连接 {dev.address}  MTU {getattr(client, 'mtu_size', 0)}")
        await client.start_notify(UUID_TX, on_notify)
        try:
            await client.start_notify(UUID_TX2, on_notify)
        except Exception as e:  # noqa: BLE001
            print(f"订阅 TX2（0203）失败：{e}——这个固件可能没开原始采样镜像（PB_RAW_STREAM）")
        await send(build_time_frame())
        print(f"等 0x08 原始采样（期望 {args.expect_hz:g} Hz），每 {args.window:g} 秒评估一次，Ctrl+C 结束\n")

        t0 = time.monotonic()
        next_report = t0 + args.window
        warned = False
        while client.is_connected:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            if args.duration and now - t0 >= args.duration:
                break
            if now >= next_report:
                next_report = now + args.window
                if not dev_ts:
                    if not warned and now - t0 > 6:
                        print("  还没收到 0x08：正式固件（协议 v2.0）不发原始采样，要用 PB_RAW_STREAM=1 编的调试固件")
                        warned = True
                    continue
                print(fmt_assess(assess(dev_ts, host_ts, args.expect_hz, args.tol), f"最近 {args.window:g} 秒"))
                print()
                dev_ts.clear()
                host_ts.clear()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        if fcsv:
            fcsv.close()
            print(f"CSV：{args.csv}")
    if all_dev:
        print()
        print(fmt_assess(assess(all_dev, all_host, args.expect_hz, args.tol), "整次会话"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="自制项圈：实时打印 IMU 原始采样 + 评估采样率 / 间隔均匀性")
    ap.add_argument("--address", help="按 MAC 连（信号弱时比按名字稳）")
    ap.add_argument("--expect-hz", type=float, default=31.25, help="期望采样率，默认 31.25")
    ap.add_argument("--tol", type=float, default=0.02, help="采样率允许偏差，默认 0.02（±2%%）")
    ap.add_argument("--window", type=float, default=5.0, help="每隔多少秒评估一次，默认 5")
    ap.add_argument("--every", type=int, default=1, help="每 N 帧打印一行，默认每帧都打")
    ap.add_argument("--quiet", action="store_true", help="不逐帧打印，只看评估")
    ap.add_argument("--show-behavior", action="store_true", help="顺便打印 0x07 当前行为")
    ap.add_argument("--duration", type=float, default=0, help="跑多少秒自动结束（0 = 一直跑）")
    ap.add_argument("--csv", help="原始采样落盘（含设备时间戳和间隔）")
    args = ap.parse_args()
    args.every = max(1, args.every)
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
