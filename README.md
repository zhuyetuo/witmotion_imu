# witmotion_imu

IMU 数据采集与解析工具集，目前包含两套设备的支持：

- **WitMotion WT901SDCL-BT50**（维特智能 9轴 IMU，BLE 5.0，`0x55 0x61` 二进制协议）
- **HICC_PetCollar**（自制宠物项圈 IMU，BLE，`0x55 0xAA` 自定义协议 v1.1）

| 脚本 | 设备 | 用途 |
|---|---|---|
| `parse_wit.py` | WitMotion | 解析离线二进制日志（如 `WIT12.TXT`），导出官方格式 / CSV / Label Studio 格式 |
| `wit_ble_live.py` | WitMotion | 通过 BLE 直连，实时接收数据，写入 Label Studio CSV 或打印到终端 |
| `hicc_ble_debug.py` | HICC_PetCollar | 通过 BLE 连接自制设备，实时解析 0x55AA 帧，自动校时，打印数据或写入 CSV |

`wit_ble_live.py` 与 `parse_wit.py` 必须放在**同一个文件夹**下——前者会 import 后者的协议解析逻辑。

---

## 环境准备

```bash
pip install bleak          # 仅 wit_ble_live.py 需要，parse_wit.py 无第三方依赖
```

`bleak` 是跨平台 BLE 库，Windows 上基于系统自带的 WinRT 蓝牙 API，**不需要先在 Windows 蓝牙设置里手动配对**，代码直接扫描/连接即可。

---

## 一、离线文件解析：`parse_wit.py`

把设备记录的原始二进制日志（28字节一帧的 `0x55 0x61` 数据包）解析成可读格式。

### 数据包格式

```
偏移  长度  内容
0     1    包头 0x55
1     1    类型 0x61
2     2    AccX   (int16)  量程 ±16g     物理值 = raw/32768*16
4     2    AccY
6     2    AccZ
8     2    GyroX  (int16)  量程 ±2000°/s 物理值 = raw/32768*2000
10    2    GyroY
12    2    GyroZ
14    2    AngleX (int16)  量程 ±180°    物理值 = raw/32768*180
16    2    AngleY
18    2    AngleZ
20    1    年 (year - 2000)
21    1    月
22    1    日
23    1    时
24    1    分
25    1    秒
26    2    毫秒 (uint16)
```

该设备未启用磁场/温度/四元数输出，对应字段在导出表里为空。

### 用法

```bash
# 还原成跟官方上位机回放结果(data_.txt)逐字节一致的Tab分隔文本
python parse_wit.py WIT12.TXT -o out.txt

# 严格按数据包对齐输出（不复刻官方工具的 AccX 错位 bug，推荐用于后续分析）
python parse_wit.py WIT12.TXT -o out.txt --no-quirk

# 导出标准CSV（逗号分隔，utf-8-sig编码，Excel/WPS双击打开不乱码）
python parse_wit.py WIT12.TXT -o out.csv

# 导出 Label Studio 时间序列标注可识别的CSV
python parse_wit.py WIT12.TXT -o labelstudio.csv
```

`--format` 可以无视文件名后缀强制指定格式：

```bash
python parse_wit.py WIT12.TXT -o out.txt --format csv
python parse_wit.py WIT12.TXT -o out.csv  --format labelstudio
```

### 离线采集的时间漂移补偿

WitMotion 设备单独采集（不连 PC）时，芯片时间会按自身晶振漂移累积误差（实测约 +7389 ppm，30分钟误差约 13 秒）。可在采集前后各记录一次 PC 真实时刻，解析时做**两端锚点线性插值**补偿：

```
采集操作流程：
  ① 开始前：连接设备，观察芯片时间，同时记下 PC 时刻（两者配对即为起始锚点）
  ② 现场采集（设备离开 PC 独立工作）
  ③ 取回后：连接设备，再次记录芯片时间和 PC 时刻（结束锚点）
  ④ 解析时加 --drift-start / --drift-end 参数
```

```bash
# 采集开始时 PC 时刻是 2026-06-23 10:00:00，取回时 PC 时刻是 2026-06-23 11:00:00
python parse_wit.py WIT12.TXT -o labelstudio.csv \
    --drift-start "2026-06-23 10:00:00" \
    --drift-end   "2026-06-23 11:00:00"
```

补偿输出：
```
漂移补偿: chip跨度=3587.2s  PC跨度=3600.0s  scale=1.003567  漂移=+7389.4ppm
共解析 71744 个数据包，输出 71744 行 -> labelstudio.csv (格式: labelstudio)
```

> **锚点精度建议**：起始锚点用官方上位机校时后立刻记录；结束锚点直接用 PC 系统时间。两端精度到秒级即可，补偿后误差远低于原始漂移。温度剧变的场景（如动物从室内跑到室外）漂移可能非线性，但对宠物项圈的正常使用影响极小。

### 关于官方回放工具的 AccX 错位现象

逐字节核对官方上位机回放结果 `data_.txt` 后发现：官方工具导出时，AccX 这一列相对于同一行的其它列（芯片时间、AccY/AccZ、角速度、角度）整体错后了一个采样点。`parse_wit.py` 默认完全复刻这个行为，方便逐行对照官方导出结果；加 `--no-quirk` 则关闭复刻，直接输出严格对齐的正确数据。**做后续分析/训练用的数据建议始终加 `--no-quirk`**（或者直接用 `labelstudio` 格式，该格式不受此参数影响，始终严格对齐）。

### Label Studio 格式

导出列：`timestamp, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z`

`timestamp` 直接用设备芯片时间，格式为 `YYYY-MM-DD HH:MM:SS.fff`（毫秒前用 **点** 分隔，例如 `2026-06-22 11:35:19.011`）。

在 Label Studio 的 Time Series 标注配置里，`timeFormat` 字段要填：

```
%Y-%m-%d %H:%M:%S.%L
```

> D3（Label Studio 底层时间解析库）不支持 `%f` 微秒格式，必须用 `%L` 三位毫秒，且分隔符要跟数据里的字符完全一致（点号），否则会报错 `timeColumn (timestamp) cannot be parsed`。

#### 自动坏帧检测与剔除

设备采集过程中如果发生过短暂的蓝牙断连重连，芯片时钟会被重新初始化，可能出现两种异常：

1. **孤立复位帧**：某一帧时间戳突然"整点归零"式回退（例如从 `13:47:05.661` 跳到 `13:00:00.000`）。
2. **整段重传**：复位帧之后，设备会把断连前一小段数据完整重发一遍——这些帧的时间戳和数值跟之前某一段完全重复。

这两种情况都会导致 Label Studio 报错 `timeColumn (timestamp) must be incremental and sequentially ordered`。

`labelstudio` 格式默认会自动检测并剔除这类坏帧/重复段（不管坏段是1帧还是连续多帧），保证导出的 `timestamp` 严格单调递增，终端会打印具体丢弃了哪些原始序号方便核对。如果想保留原始数据不做任何剔除，加 `--keep-bad-frames`。

---

## 二、实时BLE采集：`wit_ble_live.py`

直接通过 BLE 连接设备，实时接收数据。

### 标准使用流程

```bash
# 第一步：先扫描，确认能看到设备
python wit_ble_live.py --scan

# 第二步（可选）：用官方上位机软件给设备校准时间，再断开上位机连接
# 第三步：时间漂移评估（评估芯片时钟精度，默认 30 秒，可加 --cal-duration 改时长）
python wit_ble_live.py --name WTSDCL --calibrate
python wit_ble_live.py --name WTSDCL --calibrate --cal-duration 60

# 第四步：正式采集
python wit_ble_live.py --name WTSDCL -o live_labelstudio.csv

# 也可以用MAC地址直连
python wit_ble_live.py --address AA:BB:CC:DD:EE:FF -o live_labelstudio.csv

# 只在终端实时打印数据，不创建/写入任何文件
python wit_ble_live.py --name WTSDCL --print-only

# 如果默认UUID订阅不到数据，先列出设备真实的服务/特征值
python wit_ble_live.py --name WTSDCL --list-services
```

按 `Ctrl+C` 停止采集，已写入的CSV文件会被正常关闭保存。

实时采集同样会自动剔除蓝牙重连导致的坏帧（行为跟离线脚本一致），可用 `--keep-bad-frames` 关闭。

### 时间校准与漂移评估

WitMotion 设备**没有自动校时**，需要先用官方上位机软件手动同步时间，再断开上位机。用 `--calibrate` 模式评估校准后的漂移情况：

```bash
# 1. 打开官方上位机软件 → 连接设备 → 校准时间 → 断开连接（关闭软件）
# 2. 评估时间漂移（30秒，约750帧）
python wit_ble_live.py --name WTSDCL --calibrate --cal-duration 30
```

输出示例：

```
  [   1] PC=14:30:24.312  片上=14:30:24.000  PC-chip=+312.0ms  elapsed=0.0s
  [   2] PC=14:30:24.332  片上=14:30:24.020  PC-chip=+312.1ms  elapsed=0.0s
  ...
── 漂移评估结果（750 帧，30.0 秒）──
  平均偏移 (PC-chip):  +315.2 ms  ← BLE 传输延迟
  偏移范围:            +310.1 ~ +320.8 ms
  漂移率:              +0.12 ms/min  (+2.0 ppm)
  推算 1小时误差:      +7 ms
  推算 1天误差:        +0.17 秒
  ✓ 晶振精度良好（<1 ms/min）
```

| 指标 | 说明 |
|---|---|
| 平均偏移 | BLE 传输 + 缓冲延迟，正常约 300~500ms，与数据质量无关 |
| 漂移率 | 片上晶振精度，决定长时采集的时间轴准确度 |
| <1 ms/min | 晶振精度良好，数小时内误差可接受 |
| >10 ms/min | 漂移严重，长时采集需要定期重新校时 |

### 已知GATT UUID（按优先级自动尝试，无需手动指定）

```
0000ffe4-0000-1000-8000-00805f9a34fb   Notify（推荐，默认优先尝试）
0000ffe1-0000-1000-8000-00805f9a34fb   备用 Notify（部分批次/固件）
0000ffe5-0000-1000-8000-00805f9a34fb   备用/读写特征值
```

如果以上UUID都订阅失败，用 `--list-services` 打印出设备实际暴露的服务/特征值列表，照实际输出用 `--notify-uuid` 指定即可，不需要改代码。

### ⚠️ 重要：BLE 只能同时维持一个连接

**如果 WitMotion 官方上位机软件正在连着设备，`wit_ble_live.py` 会扫描不到这个设备。**

这不是脚本的bug，是 BLE 协议本身的限制——设备一旦被某个程序连接，就会停止广播（因为已经"被占用"，没必要再让别人发现），跟蓝牙耳机不能同时配对两台手机是一个道理。

解决方法：

1. 在官方上位机软件里把该设备**断开连接**（或直接关闭软件）
2. 等几秒钟让设备恢复 BLE 广播
3. 再运行 `python wit_ble_live.py --scan`，应该就能扫到了

---

## 常见问题排查

| 现象 | 原因 / 解决方法 |
|---|---|
| `--scan` 扫不到任何设备 | 确认 Windows 蓝牙已打开；电脑需支持 BLE 5.0 或插了 BLE 适配器 |
| `--scan` 扫到了别的设备，但扫不到这台IMU | 大概率是官方上位机软件还连着它，参见上面"BLE 只能同时维持一个连接" |
| 按地址 `--address` 连接报"未找到设备" | 同上；也可能是地址大小写问题（一般无影响），优先用 `--name` 重试 |
| 连接成功但订阅特征值失败 / 收不到数据 | 用 `--list-services` 看设备真实UUID，再用 `--notify-uuid` 手动指定 |
| 导入 Label Studio 报 `cannot be parsed` | 确认 timeFormat 填的是 `%Y-%m-%d %H:%M:%S.%L`（注意是点不是冒号分隔毫秒） |
| 导入 Label Studio 报 `must be incremental and sequentially ordered` | 一般是用了 `--keep-bad-frames` 导致坏帧没被过滤；去掉该参数重新导出即可 |

---

---

## 三、HICC_PetCollar BLE 调试：`hicc_ble_debug.py`

连接自制宠物项圈设备，解析 `0x55 0xAA` 自定义协议（v1.1）。

### 协议概要

| 帧类型 | 频率 | 总长 | 内容 |
|---|---|---|---|
| 六轴帧 (cmd=0x05) | 25 Hz | 67 字节 | Unix 毫秒时间戳 + 加速度XYZ (m/s²) + 陀螺仪XYZ (rad/s) |
| 温湿度帧 (cmd=0x05) | 1 Hz | 43 字节 | Unix 毫秒时间戳 + 室温 + 湿度 + 体温 + 电池电压 |
| 校时请求 (cmd=0x06) | 上电后 | 7 字节 | 设备请求 App 下发当前时间 |

BLE GATT UUID：

| 特征 | UUID | 方向 |
|---|---|---|
| TX (Notify) | `A6ED0202-D344-460A-8075-B9E8EC90D71B` | 设备 → App（订阅接收） |
| RX (Write)  | `A6ED0203-D344-460A-8075-B9E8EC90D71B` | App → 设备（写校时帧） |

### 标准使用流程

```bash
# 先安装依赖（如果尚未安装）
pip install bleak

# 第一步：扫描附近所有 BLE 设备，确认能看到 HICC_PetCollar
python hicc_ble_debug.py --scan

# 第二步：时间校准与漂移评估（连接后自动下发北京时间，采集30秒）
python hicc_ble_debug.py --address EA:CB:3E:CF:00:1B --calibrate
python hicc_ble_debug.py --address EA:CB:3E:CF:00:1B --calibrate --cal-duration 10

# 第三步：实时打印，观察时间漂移
python hicc_ble_debug.py --address EA:CB:3E:CF:00:1B

# 连接并同时写入 CSV 文件（自动生成 _6axis.csv 和 _env.csv 两个文件）
python hicc_ble_debug.py --address EA:CB:3E:CF:00:1A -o hicc_data.csv

# 连接后只列出 GATT 服务/特征值，不接收数据（UUID 核对用）
python hicc_ble_debug.py --address EA:CB:3E:CF:00:1A --list-services

# 跳过校时（设备时钟已准确时使用）
python hicc_ble_debug.py --address EA:CB:3E:CF:00:1A --no-timesync
```

### 时间校准与漂移评估

HICC_PetCollar **脚本连接时自动下发北京时间**，无需手动操作。`--calibrate` 模式评估校准后的漂移：

```bash
python hicc_ble_debug.py --address EA:CB:3E:CF:00:1B --calibrate --cal-duration 30
```

输出示例（实测）：

```
  [   1] chip=1750686624000ms  PC-chip=+12.3ms  elapsed=0.0s
  ...
── 校准结果（750 帧，30.0 秒）──
  平均偏移 (PC-chip): +12.5 ms     ← BLE 传输延迟（比 WitMotion 低，因固件帧更小）
  偏移变化范围:       +10.1 ~ +15.8 ms
  漂移率:             -6.50 ms/min  (-108.3 ppm)
  ⚠ 晶振有轻微漂移，长时采集建议重新校时
```

> 实测该设备晶振漂移约 **-6.5 ms/min（-108 ppm）**，1小时累计误差约 390ms，1天约 9.4秒。每次连接脚本都会自动重新校时，因此正常使用影响不大。

按 `Ctrl+C` 停止，CSV 文件会被正常关闭保存。

### 终端输出格式

```
[    1][6轴] 2026-06-23 10:00:00.040  acc=(+7.7431,+0.9338,+6.1844)m/s²  gyro=(+0.025635,+0.054542,+0.006545)rad/s
[    2][6轴] 2026-06-23 10:00:00.080  ...
[   25][环境] 2026-06-23 10:00:00.980  室温=26.4°C  湿度=57.2%RH  体温=38.5°C  电池=3800mV
```

### CSV 输出格式

`_6axis.csv`（六轴，25Hz）：

```
timestamp,acc_x_ms2,acc_y_ms2,acc_z_ms2,gyro_x_rads,gyro_y_rads,gyro_z_rads
2026-06-23 10:00:00.040,7.743124,0.933772,6.184443,0.025635,0.054542,0.006545
```

`_env.csv`（温湿度，1Hz）：

```
timestamp,temp_in_c,hum_in_pct,temp_body_c,batt_mv
2026-06-23 10:00:00.980,26.4,57.2,38.5,3800
```

### 已知设备 MAC 地址

```
EA:CB:3E:CF:00:1A
EA:CB:3E:CF:00:1B
EA:CB:3E:CF:00:1D
```

### 注意事项

- 脚本连接后会**立刻主动下发校时帧**（北京时间），不需要等设备请求。收到设备的校时请求时也会自动响应。
- BLE 分包重组：设备请求 MTU=247，脚本内置 `FrameBuffer` 按帧头+长度字段自动重组跨包分片。
- 校验失败的帧会打印原始 hex 并丢弃，正常帧不受影响。

---

## 文件清单

```
witmotion_imu/
├── parse_wit.py        # WitMotion 离线文件解析（必需）
├── wit_ble_live.py     # WitMotion 实时 BLE 采集（依赖 parse_wit.py 同目录存在）
├── hicc_ble_debug.py   # HICC_PetCollar 自制设备 BLE 调试（独立，无额外依赖）
└── README.md           # 本文档
```
