# -*- coding: utf-8 -*-
"""
WT901SDCL-BT50 原始IMU数据解析脚本
=====================================

功能:
    读取维特智能(WitMotion) WT901SDCL-BT50 设备记录的原始二进制日志文件
    (例如 WIT12.TXT)，解析出 0x55 0x61 数据包（加速度 + 角速度 + 角度 +
    芯片时间戳），并生成与官方回放软件输出（如 data_.txt）格式一致的
    Tab 分隔文本文件（GBK 编码）。

数据包格式 (0x55 0x61，定长 28 字节):
    偏移  长度  内容
    0     1    包头 0x55
    1     1    类型 0x61
    2     2    AccX   (int16, LSB), 量程 ±16g  -> 物理值 = raw/32768*16  (g)
    4     2    AccY   (int16, LSB)
    6     2    AccZ   (int16, LSB)
    8     2    GyroX  (int16, LSB), 量程 ±2000°/s -> 物理值 = raw/32768*2000
    10    2    GyroY  (int16, LSB)
    12    2    GyroZ  (int16, LSB)
    14    2    AngleX (int16, LSB), 量程 ±180°    -> 物理值 = raw/32768*180
    16    2    AngleY (int16, LSB)
    18    2    AngleZ (int16, LSB)
    20    1    年 (year - 2000)
    21    1    月
    22    1    日
    23    1    时
    24    1    分
    25    1    秒
    26    2    毫秒 (uint16, LSB)

    (该设备未启用磁场/温度/四元数输出，故这些字段在回放表里为空。)

关于官方回放工具 (data_.txt) 中 AccX 列错位的还原:
    经与官方回放结果 data_.txt 逐字节核对发现，官方回放工具在导出时，
    AccX 这一列相对于同一行的其它列（芯片时间、AccY/AccZ、角速度、角度）
    整体错后了一个采样点：
        第 i 行(数据行, 从1开始) 的时间戳/AccY/AccZ/角速度/角度 取自第 (i-1) 个数据包，
        而该行的 AccX 取自第 i 个数据包。
    第一行只有 AccX（取自第 0 个数据包），其它列为空；
    最后一个数据包的 AccX 出现在最后一行，不会再产生额外的行。
    本脚本默认完全复刻这一行为，以便与 data_.txt 逐行对齐
    (可通过 --no-quirk 关闭，直接输出严格对齐的正确数据)。

用法:
    python parse_wit.py WIT12.TXT -o out.txt
    python parse_wit.py WIT12.TXT -o out.txt --no-quirk   # 不复刻错位 bug，AccX 与其它字段严格同包对齐
    python parse_wit.py WIT12.TXT -o out.csv              # 输出后缀为 .csv 时自动导出标准CSV（逗号分隔, utf-8-sig编码）
    python parse_wit.py WIT12.TXT -o out.txt --format csv # 也可用 --format 强制指定格式，与输出文件后缀无关
    python parse_wit.py WIT12.TXT -o labelstudio.csv      # 文件名含 "labelstudio" 自动导出 Label Studio 格式
    python parse_wit.py WIT12.TXT -o out.csv --format labelstudio  # 也可用 --format 强制指定

Label Studio 格式说明:
    导出列为: timestamp, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z
    timestamp 使用芯片时间，格式 'YYYY-MM-DD HH:MM:SS.fff'（毫秒前用'.'分隔，
    例如 2026-06-22 11:35:19.011）。
    该格式始终按数据包严格对齐输出（不应用官方工具的 AccX 错位 quirk），
    编码为 UTF-8（无BOM），逗号分隔，便于直接拖入 Label Studio 做时间序列标注。

    在 Label Studio 的 Time Series 标注配置（labeling config）里，timeColumn
    对应的 timeFormat 要填:
        %Y-%m-%d %H:%M:%S.%L
    （D3 不支持 %f 微秒，必须用 %L 三位毫秒，且分隔符要和数据里的 '.' 一致，
    否则会报错 "timeColumn (timestamp) cannot be parsed"。）

    异常坏帧处理:
        部分采集过程中如果设备蓝牙短暂断连重连，芯片时钟会被重置，导致某一帧
        的时间戳出现"整点归零"式的大幅度回退（例如 13:47:05.661 后突然跳到
        13:00:00.000，然后下一帧又跳回 13:47:xx 附近），这会让 Label Studio
        报错 "timeColumn (timestamp) must be incremental and sequentially
        ordered"。labelstudio 格式默认会自动检测并剔除这种孤立坏帧，保证导出
        的 timestamp 严格单调递增；可用 --keep-bad-frames 关闭这一行为。
"""

import argparse
import struct
import sys
from datetime import datetime, timedelta

PACKET_LEN = 28
HEADER = 0x55
TYPE_61 = 0x61

ACC_RANGE = 16.0       # g
GYRO_RANGE = 2000.0    # deg/s
ANGLE_RANGE = 180.0    # deg


def parse_packets(raw: bytes):
    """从原始字节流中解析所有 0x55 0x61 数据包，返回字典列表。"""
    packets = []
    i = 0
    n = len(raw)
    while i + PACKET_LEN <= n:
        if raw[i] == HEADER and raw[i + 1] == TYPE_61:
            pkt = raw[i:i + PACKET_LEN]
            vals = struct.unpack('<9h', pkt[2:20])
            acc = [v / 32768.0 * ACC_RANGE for v in vals[0:3]]
            gyro = [v / 32768.0 * GYRO_RANGE for v in vals[3:6]]
            angle = [v / 32768.0 * ANGLE_RANGE for v in vals[6:9]]
            yy, mm, dd, hh, mi, ss = pkt[20:26]
            ms = struct.unpack('<H', pkt[26:28])[0]
            try:
                chip_time = datetime(2000 + yy, mm, dd, hh, mi, ss) + timedelta(milliseconds=ms)
            except ValueError:
                chip_time = None
            packets.append({
                'acc': acc,
                'gyro': gyro,
                'angle': angle,
                'year': 2000 + yy,
                'month': mm,
                'day': dd,
                'hour': hh,
                'minute': mi,
                'second': ss,
                'ms': ms,
                'chip_time': chip_time,
            })
            i += PACKET_LEN
        else:
            # 不是合法包头，逐字节前进，寻找下一个同步点
            i += 1
    return packets


def fmt_chip_time(p):
    if p['chip_time'] is None:
        return ''
    return '{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}:{:03d}'.format(
        p['year'], p['month'], p['day'], p['hour'], p['minute'], p['second'], p['ms']
    )


def fmt_chip_time_dotms(p):
    """
    Label Studio (D3 timeFormat) 兼容的时间格式：毫秒前用 '.' 分隔，而不是 ':'。
    对应的 D3 timeFormat 字符串为: %Y-%m-%d %H:%M:%S.%L
    （D3 不支持 %f 微秒格式，必须用 %L 三位毫秒，且分隔符要跟实际数据一致）。
    """
    if p['chip_time'] is None:
        return ''
    return '{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}.{:03d}'.format(
        p['year'], p['month'], p['day'], p['hour'], p['minute'], p['second'], p['ms']
    )


def fmt_num(x):
    """与官方回放工具一致的数值格式：去掉多余小数位，但不强制定长。"""
    r = round(x, 3)
    if r == int(r):
        return str(int(r))
    s = ('%.3f' % r).rstrip('0').rstrip('.')
    return s


HEADER_ROW = [
    '时间', '设备名称', '片上时间()',
    '加速度X(g)', '加速度Y(g)', '加速度Z(g)',
    '角速度X(°/s)', '角速度Y(°/s)', '角速度Z(°/s)',
    '角度X(°)', '角度Y(°)', '角度Z(°)',
    '磁场X(?t)', '磁场Y(?t)', '磁场Z(?t)',
    '温度(℃)',
    '四元数0()', '四元数1()', '四元数2()', '四元数3()',
]


def build_rows(packets, device_name, reproduce_quirk=True, wallclock_start='10:49:57.376'):
    """
    生成与 data_.txt 完全相同结构的行（字符串列表的列表）。

    reproduce_quirk=True: 复刻官方回放工具的 AccX 错位现象（默认，便于逐行对照 data_.txt）。
    reproduce_quirk=False: 严格按同一数据包输出 AccX/AccY/AccZ/Gyro/Angle，不做错位处理。
    """
    rows = []
    n = len(packets)

    # 第一列"时间"是回放/处理时的墙钟时间，并非传感器数据，这里用一个
    # 简单递增的占位时间戳来模拟（不影响后面18列的真实IMU数据）。
    wallclock_base = datetime.strptime(wallclock_start, '%H:%M:%S.%f')

    def wallclock_str(row_idx):
        # 模拟 data_.txt 中观察到的速度（约每80行增加1毫秒），仅用于格式还原
        extra_ms = row_idx // 80
        t = wallclock_base + timedelta(milliseconds=extra_ms)
        return t.strftime('%H:%M:%S.%f')[:-3]

    if reproduce_quirk:
        # 第0行：只有 AccX（来自 packet[0]），其它列为空
        p0 = packets[0]
        row0 = [wallclock_str(0), device_name, '', fmt_num(p0['acc'][0])] + [''] * 16
        rows.append(row0)

        # 第 i 行 (i=1..n-1)：时间戳/AccY/AccZ/Gyro/Angle 取自 packet[i-1]
        #                     AccX 取自 packet[i]
        for i in range(1, n):
            prev = packets[i - 1]
            cur = packets[i]
            row = [
                wallclock_str(i),
                device_name,
                fmt_chip_time(prev),
                fmt_num(cur['acc'][0]),
                fmt_num(prev['acc'][1]),
                fmt_num(prev['acc'][2]),
                fmt_num(prev['gyro'][0]),
                fmt_num(prev['gyro'][1]),
                fmt_num(prev['gyro'][2]),
                fmt_num(prev['angle'][0]),
                fmt_num(prev['angle'][1]),
                fmt_num(prev['angle'][2]),
            ] + [''] * 8  # 磁场x3 + 温度 + 四元数x4
            rows.append(row)
    else:
        # 严格模式：每一行都来自同一个数据包，AccX 不错位
        for i, p in enumerate(packets):
            row = [
                wallclock_str(i),
                device_name,
                fmt_chip_time(p),
                fmt_num(p['acc'][0]),
                fmt_num(p['acc'][1]),
                fmt_num(p['acc'][2]),
                fmt_num(p['gyro'][0]),
                fmt_num(p['gyro'][1]),
                fmt_num(p['gyro'][2]),
                fmt_num(p['angle'][0]),
                fmt_num(p['angle'][1]),
                fmt_num(p['angle'][2]),
            ] + [''] * 8
            rows.append(row)

    return rows


LABELSTUDIO_HEADER = ['timestamp', 'acc_x', 'acc_y', 'acc_z', 'gyro_x', 'gyro_y', 'gyro_z']


def apply_drift_correction(packets, pc_start: datetime, pc_end: datetime):
    """
    对离线采集的数据做线性时间漂移补偿。

    原理（两端锚点线性插值）：
        采集前后各记录一次 PC 时间和芯片时间，以 PC 时间为真值，
        按帧序号在两个锚点之间线性插值，把每帧的芯片时间校正到真实时刻。

        corrected[i] = pc_start + (chip[i] - chip_start) × scale
        scale = (pc_end - pc_start) / (chip_end - chip_start)

    参数：
        pc_start  : 采集开始时 PC 侧的真实时刻（datetime，需与 chip_start 同时记录）
        pc_end    : 取回数据时 PC 侧的真实时刻（datetime，需与 chip_end 同时记录）

    注意：
        - 补偿后的时间轴以 PC 时间为准，不再依赖芯片晶振精度。
        - 若芯片时间戳非单调（坏帧），建议先调用 fix_nonmonotonic_packets()。
        - 温度剧变（>10°C）可能导致漂移非线性；如有条件，中途多记录几个锚点
          做分段线性插值效果更好。

    返回：
        新的 packets 列表，chip_time / hour / minute / second / ms 字段已替换为校正值。
    """
    if not packets:
        return packets

    # 过滤出有效时间戳的帧
    valid = [(i, p) for i, p in enumerate(packets) if p['chip_time'] is not None]
    if len(valid) < 2:
        print('警告: 有效时间戳帧数不足，无法做漂移补偿。', file=sys.stderr)
        return packets

    chip_start = valid[0][1]['chip_time']
    chip_end   = valid[-1][1]['chip_time']
    chip_span  = (chip_end - chip_start).total_seconds()
    pc_span    = (pc_end - pc_start).total_seconds()

    if chip_span <= 0:
        print('警告: 芯片时间跨度为0，无法做漂移补偿。', file=sys.stderr)
        return packets

    scale = pc_span / chip_span
    drift_ppm = (pc_span - chip_span) / pc_span * 1_000_000
    print(f'漂移补偿: chip跨度={chip_span:.3f}s  PC跨度={pc_span:.3f}s  '
          f'scale={scale:.6f}  漂移={drift_ppm:+.1f}ppm')

    corrected = []
    for p in packets:
        p = dict(p)  # 浅拷贝，避免修改原始数据
        if p['chip_time'] is not None:
            elapsed_chip = (p['chip_time'] - chip_start).total_seconds()
            new_dt = pc_start + timedelta(seconds=elapsed_chip * scale)
            p['chip_time'] = new_dt
            p['year']   = new_dt.year
            p['month']  = new_dt.month
            p['day']    = new_dt.day
            p['hour']   = new_dt.hour
            p['minute'] = new_dt.minute
            p['second'] = new_dt.second
            p['ms']     = new_dt.microsecond // 1000
        corrected.append(p)
    return corrected


def fix_nonmonotonic_packets(packets):
    """
    检测并剔除时间戳/数据异常的坏帧，保证返回的序列时间戳严格单调递增。

    现象背景（实测两种典型情况）：
        1. 孤立复位帧：设备短暂蓝牙断连重连，芯片时钟被重新初始化，导致
           某一帧的时间戳出现"整点归零"式的大幅度回退（例如从 13:47:05.661
           直接跳到 13:00:00.000），只影响这一帧本身。
        2. 整段重传：紧跟在复位帧之后，设备会把断连前一小段时间的数据
           完整重新发送一遍——这一段帧的时间戳和加速度/角速度数值跟之前
           已经处理过的某一段完全相同（逐字节重复），属于重复数据而不是
           新采样点。
        此外还可能出现个别时间戳字段本身无法解析（年月日超出合法范围）的
        坏帧，直接丢弃。

    处理策略：
        从前向后扫描，维护当前已确认正常的最后时间戳 last_good_time：
        - 时间戳无法解析 -> 丢弃。
        - 时间戳早于或等于 last_good_time -> 说明是复位帧或者重传段的一部分
          （重传段的时间戳会重新从某个更早的时刻起步，自然 <= last_good_time），
          丢弃，且不更新 last_good_time，继续用它比较后续帧，直到时间戳
          重新追上并超过 last_good_time 为止——这样无论坏段长度是1帧还是
          连续35帧，都会被完整跳过，且不会误删后面真正的新数据。
        - 否则视为正常帧，保留，并更新 last_good_time。

    返回:
        (good_packets, dropped_list)
        dropped_list 是 [(原始索引, 时间戳字符串或'?'), ...]，用于打印提示。
    """
    if not packets:
        return packets, []

    good = []
    dropped = []
    last_good_time = None

    for idx, p in enumerate(packets):
        t = p['chip_time']
        if t is None:
            dropped.append((idx, '?'))
            continue
        if last_good_time is not None and t <= last_good_time:
            dropped.append((idx, fmt_chip_time(p)))
            continue
        good.append(p)
        last_good_time = t

    return good, dropped


def build_labelstudio_rows(packets):
    """
    生成 Label Studio 时序标注可直接识别的数据行：
        timestamp, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z

    每一行严格来自同一个数据包（不应用 AccX 错位 quirk，标注/训练数据要保证对齐正确）。
    timestamp 使用芯片时间，格式为 'YYYY-MM-DD HH:MM:SS.fff'（毫秒前用'.'分隔，
    对应 Label Studio/D3 的 timeFormat: %Y-%m-%d %H:%M:%S.%L）。
    注意：D3 不支持 %f（微秒），必须用 %L（3位毫秒），且分隔符必须跟数据中的
    实际字符一致——如果用 ':' 分隔毫秒会导致 Label Studio 无法解析该列。

    调用前应先用 fix_nonmonotonic_packets() 剔除时间戳非单调递增的坏帧，
    否则 Label Studio 会报错 "timeColumn (timestamp) must be incremental
    and sequentially ordered"。
    """
    rows = []
    for p in packets:
        ts = fmt_chip_time_dotms(p)
        row = [
            ts,
            fmt_num(p['acc'][0]),
            fmt_num(p['acc'][1]),
            fmt_num(p['acc'][2]),
            fmt_num(p['gyro'][0]),
            fmt_num(p['gyro'][1]),
            fmt_num(p['gyro'][2]),
        ]
        rows.append(row)

    return rows


def write_labelstudio_csv(path, rows, encoding='utf-8'):
    """
    写出 Label Studio 标准 CSV。Label Studio 的时间序列(Time Series)标注
    通常要求 UTF-8 编码 + 逗号分隔 + 数值型 timestamp 列，这里不加 BOM，
    以保证最大兼容性（Label Studio 自身用 Python csv 模块读取，BOM 容易
    被当成第一列名的一部分导致 timestamp 列名识别失败）。
    """
    import csv
    with open(path, 'w', encoding=encoding, newline='') as f:
        writer = csv.writer(f)
        writer.writerow(LABELSTUDIO_HEADER)
        writer.writerows(rows)


def write_output(path, rows, fmt='txt', encoding=None):
    """
    输出文件。

    fmt='txt': Tab 分隔，默认 GBK 编码（与官方回放工具 data_.txt 完全一致格式）。
    fmt='csv': 逗号分隔，默认 UTF-8 with BOM 编码（utf-8-sig，双击在 Excel/WPS 中
               打开不会乱码；字段中如包含逗号/引号/换行会自动加引号转义）。
    encoding: 显式指定编码，覆盖上面两种格式各自的默认编码。
    """
    if fmt == 'csv':
        enc = encoding or 'utf-8-sig'
        import csv
        with open(path, 'w', encoding=enc, newline='') as f:
            writer = csv.writer(f)
            writer.writerow(HEADER_ROW)
            writer.writerows(rows)
    else:
        enc = encoding or 'gbk'
        with open(path, 'w', encoding=enc, newline='\r\n') as f:
            f.write('\t'.join(HEADER_ROW) + '\n')
            for row in rows:
                f.write('\t'.join(row) + '\n')


def detect_format(path, explicit_fmt):
    """根据 --format 参数或输出文件后缀名自动判断导出格式。"""
    if explicit_fmt:
        return explicit_fmt
    lower = path.lower()
    if 'labelstudio' in lower or 'label_studio' in lower or 'label-studio' in lower:
        return 'labelstudio'
    if lower.endswith('.csv'):
        return 'csv'
    return 'txt'


def main():
    ap = argparse.ArgumentParser(description='解析 WT901SDCL-BT50 原始IMU日志文件')
    ap.add_argument('input', help='原始二进制日志文件路径，如 WIT12.TXT')
    ap.add_argument('-o', '--output', default='parsed_output.txt',
                     help='输出文件路径，后缀 .csv 自动导出CSV；文件名含 "labelstudio" 自动导出Label Studio格式')
    ap.add_argument('--format', choices=['txt', 'csv', 'labelstudio'], default=None,
                     help='强制指定输出格式（默认根据 -o 的文件名自动判断：.csv -> csv，'
                          '含 labelstudio -> labelstudio，其它 -> txt）')
    ap.add_argument('--device-name', default=None, help='设备名称列内容，默认使用输入文件名')
    ap.add_argument('--no-quirk', action='store_true',
                     help='不复刻官方回放工具的 AccX 错位现象，输出严格对齐的数据')
    ap.add_argument('--encoding', default=None,
                     help='输出文件编码，默认 txt 用 gbk（与官方工具一致），csv 用 utf-8-sig（Excel友好），'
                          'labelstudio 用 utf-8（无BOM）')
    ap.add_argument('--keep-bad-frames', action='store_true',
                     help='labelstudio 格式默认会自动剔除时间戳非单调递增的孤立坏帧'
                          '（例如设备蓝牙重连导致时钟瞬间回退），加此参数则保留所有帧不做剔除')
    ap.add_argument('--drift-start', default=None,
                     help='漂移补偿起始锚点：采集开始时 PC 的真实时刻，格式 "YYYY-MM-DD HH:MM:SS"。'
                          '需与 --drift-end 同时指定才生效。')
    ap.add_argument('--drift-end', default=None,
                     help='漂移补偿结束锚点：取回数据时 PC 的真实时刻，格式 "YYYY-MM-DD HH:MM:SS"。')
    args = ap.parse_args()

    with open(args.input, 'rb') as f:
        raw = f.read()

    packets = parse_packets(raw)
    if not packets:
        print('未解析到任何 0x55 0x61 数据包，请检查输入文件。', file=sys.stderr)
        sys.exit(1)

    # 漂移补偿（需同时指定两个锚点）
    if args.drift_start and args.drift_end:
        try:
            pc_start = datetime.strptime(args.drift_start, '%Y-%m-%d %H:%M:%S')
            pc_end   = datetime.strptime(args.drift_end,   '%Y-%m-%d %H:%M:%S')
        except ValueError:
            print('错误: --drift-start / --drift-end 格式应为 "YYYY-MM-DD HH:MM:SS"', file=sys.stderr)
            sys.exit(1)
        packets = apply_drift_correction(packets, pc_start, pc_end)
    elif args.drift_start or args.drift_end:
        print('警告: --drift-start 和 --drift-end 需同时指定，漂移补偿已跳过。', file=sys.stderr)

    device_name = args.device_name or args.input.split('/')[-1].split('\\')[-1]

    out_fmt = detect_format(args.output, args.format)

    if out_fmt == 'labelstudio':
        if args.keep_bad_frames:
            ls_packets = packets
            dropped = []
        else:
            ls_packets, dropped = fix_nonmonotonic_packets(packets)
        rows = build_labelstudio_rows(ls_packets)
        write_labelstudio_csv(args.output, rows, encoding=args.encoding or 'utf-8')
        if dropped:
            print(f'警告: 检测到 {len(dropped)} 个时间戳非单调递增的坏帧，已自动剔除（--keep-bad-frames 可保留）:')
            for idx, ts in dropped[:20]:
                print(f'  - 原始序号 {idx}: {ts}')
            if len(dropped) > 20:
                print(f'  ... 还有 {len(dropped) - 20} 个未列出')
    else:
        rows = build_rows(packets, device_name, reproduce_quirk=not args.no_quirk)
        write_output(args.output, rows, fmt=out_fmt, encoding=args.encoding)

    print(f'共解析 {len(packets)} 个数据包，输出 {len(rows)} 行 -> {args.output} (格式: {out_fmt})')
    if out_fmt == 'labelstudio':
        print('提示: 在 Label Studio 的 Time Series 标注配置里，timeFormat 请填: %Y-%m-%d %H:%M:%S.%L')


if __name__ == '__main__':
    main()


# ── BLE 实时采集相关 ────────────────────────────────────────────────────────

# 常见的 WitMotion BLE 特征值 UUID（小写）
DEFAULT_NOTIFY_CANDIDATES = [
    '0000ffe4-0000-1000-8000-00805f9a34fb',
    '0000ffe1-0000-1000-8000-00805f9a34fb',
    '0000ffe5-0000-1000-8000-00805f9a34fb',
]


class StreamingByteBuffer:
    """累积 BLE notify 推送的字节，按 0x55 0x61 同步头切出完整28字节包。"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf.extend(data)
        packets = []
        i = 0
        n = len(self.buf)
        while i + PACKET_LEN <= n:
            if self.buf[i] == HEADER and self.buf[i + 1] == TYPE_61:
                packets.append(bytes(self.buf[i:i + PACKET_LEN]))
                i += PACKET_LEN
            else:
                i += 1
        del self.buf[:i]
        return packets


def parse_one_packet(pkt: bytes):
    """解析单个28字节 0x55 0x61 数据包，返回字典（结构与 parse_packets 一致）。"""
    if len(pkt) != PACKET_LEN or pkt[0] != HEADER or pkt[1] != TYPE_61:
        return None
    vals = struct.unpack('<9h', pkt[2:20])
    acc = [v / 32768.0 * ACC_RANGE for v in vals[0:3]]
    gyro = [v / 32768.0 * GYRO_RANGE for v in vals[3:6]]
    angle = [v / 32768.0 * ANGLE_RANGE for v in vals[6:9]]
    yy, mm, dd, hh, mi, ss = pkt[20:26]
    ms = struct.unpack('<H', pkt[26:28])[0]
    try:
        chip_time = datetime(2000 + yy, mm, dd, hh, mi, ss) + timedelta(milliseconds=ms)
    except ValueError:
        chip_time = None
    return {
        'acc': acc, 'gyro': gyro, 'angle': angle,
        'year': 2000 + yy, 'month': mm, 'day': dd,
        'hour': hh, 'minute': mi, 'second': ss,
        'ms': ms, 'chip_time': chip_time,
    }
