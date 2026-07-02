# witmotion_imu

IMU 数据采集工具集，支持 WitMotion WT901SDCL-BT50 和 HICC_PetCollar 自制设备的 BLE 实时采集、离线文件解析与摄像头同步录制。

## 文件结构

| 文件 | 说明 |
|------|------|
| `ble_utils.py` | 共享 BLE 工具：`HzCounter`、`scan_devices`、`find_device`、`list_services` |
| `wit_parse.py` | WitMotion 协议解析（离线 + BLE）：`parse_packets`、`StreamingByteBuffer`、`parse_one_packet`、`DEFAULT_NOTIFY_CANDIDATES`、`fmt_chip_time_dotms` 等 |
| `hicc_parse.py` | HICC_PetCollar 协议解析：GATT UUID、帧常量、DP 解析、`FrameBuffer`、校时帧构造、`find_tx_uuid`/`find_rx_uuid`/`send_timesync` |
| `wit_ble_live.py` | WitMotion BLE 实时采集主程序，导入 `ble_utils` + `wit_parse` |
| `hicc_ble_live.py` | HICC BLE 实时采集主程序，导入 `ble_utils` + `hicc_parse` |
| `wit_drift_analysis.py` | WitMotion 时间漂移分析与线性补偿验证 |
| `hicc_drift_analysis.py` | HICC 时间漂移分析与线性补偿验证 |
| `imu_camera_sync.py` | IMU + 摄像头同步采集（BLE 后台线程 + 主线程 OpenCV） |
| `data/` | 采集输出文件目录（CSV、MP4） |

### 模块依赖关系

```
ble_utils.py          wit_parse.py          hicc_parse.py
     │                     │                      │
     ├──────────────────────┤          ┌───────────┤
     │                     │          │           │
wit_ble_live.py      wit_drift_analysis.py    hicc_ble_live.py
                                               hicc_drift_analysis.py
                                               imu_camera_sync.py
```

## 依赖安装

```bash
pip install bleak
pip install opencv-python        # 仅 imu_camera_sync.py 需要
pip install matplotlib           # 可选，用于 *_drift_analysis.py --plot
```

## 输出文件命名规则

| 场景 | 文件名格式 |
|------|-----------|
| WitMotion CSV | `data/wit_eacb3ecf001b_20260626_143000.csv` |
| HICC 六轴 | `data/hicc_eacb3ecf001b_20260626_143000_6axis.csv` |
| HICC 温湿度 | `data/hicc_eacb3ecf001b_20260626_143000_env.csv` |
| 摄像头同步视频 | `data/hicc_eacb3ecf001b_20260626_143000.mp4` |
| 摄像头同步 IMU | `data/hicc_eacb3ecf001b_20260626_143000.csv` |

MAC 地址中的冒号会被去掉并转为小写，例如 `EA:CB:3E:CF:00:1B` → `eacb3ecf001b`。

## 使用说明

### WitMotion WT901SDCL-BT50

```bash
# 扫描附近 BLE 设备
python wit_ble_live.py --scan

# 按名称关键字连接，只打印，不保存文件
python wit_ble_live.py --name WTSDCL --print-only

# 连接并持续采集（Ctrl+C 停止），自动生成 data/wit_xxMAC_时间戳.csv
python wit_ble_live.py --name WTSDCL

# 采集 60 秒后自动停止
python wit_ble_live.py --name WTSDCL --duration 60

# 时间漂移评估（需先用官方上位机校准设备时间）
python wit_ble_live.py --name WTSDCL --calibrate

# 查看设备 GATT 服务/特征值（用于核实 UUID）
python wit_ble_live.py --name WTSDCL --list-services
```

### HICC_PetCollar

```bash
# 扫描附近 BLE 设备
python hicc_ble_live.py --scan

# 连接并只打印（不保存 CSV）
python hicc_ble_live.py --address EA:CB:3E:CF:00:1B

# 连接并保存 CSV（-o 参数为任意字符串即可触发保存，实际文件名由 MAC+时间戳自动生成）
python hicc_ble_live.py --address EA:CB:3E:CF:00:1B -o any

# 采集 60 秒后自动停止
python hicc_ble_live.py --address EA:CB:3E:CF:00:1B -o any --duration 60

# 时间漂移评估
python hicc_ble_live.py --address EA:CB:3E:CF:00:1B --calibrate

# 查看 GATT 服务/特征值
python hicc_ble_live.py --address EA:CB:3E:CF:00:1B --list-services
```

### 离线文件解析（WitMotion）

```bash
# 解析原始日志，输出标准 CSV
python wit_parse.py data/test/WIT16.TXT -o out.csv

# 输出 Label Studio 格式 CSV（时间戳格式 %Y-%m-%d %H:%M:%S.%L）
python wit_parse.py data/test/WIT16.TXT -o labelstudio.csv
```

**Label Studio 配置说明**：在 Time Series 标注配置的 `timeFormat` 填：
```
%Y-%m-%d %H:%M:%S.%L
```
（D3 不支持 `%f` 微秒格式，必须用 `%L` 三位毫秒，分隔符须与数据中的 `.` 一致。）

### IMU + 摄像头同步采集

```bash
# HICC 设备，录制 25fps，不限时（Ctrl+C 停止）
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --fps 25

# HICC 设备，录制 60 秒后自动保存视频 + CSV
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --fps 25 --duration 60

# WitMotion 设备，按名称查找，20fps
python imu_camera_sync.py --device wit --name WTSDCL --fps 20

# 指定摄像头编号（默认 0）
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --camera 1

# 保存不带叠加信息的原始视频（默认叠加 IMU/帧率/延迟信息，方便数据标注）
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --no-save-overlay

# 关闭事件驱动同步，改用固定定时器抓帧（不推荐，仅调试用）
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --no-imu-sync

# 常用：WitMotion 设备，指定摄像头1，录制180秒
python imu_camera_sync.py --device wit --name WTSDCL --fps 20 --duration 180 --camera 1
```

视频默认叠加 IMU 数值、帧率、imu_lag 等信息（标注时可直观判断数据质量）。

**输出文件（每次录制生成 3 个文件）：**

| 文件 | 内容 |
|------|------|
| `{base}.mp4` | 视频（默认带叠加信息） |
| `{base}.csv` | Label Studio 兼容格式：`timestamp, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z` |
| `{base}_meta.csv` | 全量对齐信息：`frame_idx, cam_timestamp, imu_timestamp, imu_lag_ms, imu_missing, acc/gyro, cam_fps, imu_hz` |

**同步模式**：默认摄像头会等待新的 IMU 样本到达后才抓帧（事件驱动），使两条独立时间线（摄像头定时器 vs BLE 到达时间）天然对齐，避免同一个 IMU 样本被多帧复用。`--no-imu-sync` 可切回旧的固定定时器模式（仅供调试对比）。

视频文件第 N 帧与 `{base}_meta.csv` 里 `frame_idx=N` 那一行严格一一对应（按写入顺序保证），与视频总时长、fps 元数据无关。录制结束后会用真实平均帧率修正视频容器的 fps 元数据（需要系统装有 `ffmpeg`），让播放时长与实际录制时间一致，纯粹是回放体验，不影响帧对齐。

**关于重复复用 IMU 样本**：如果 IMU 采样率低于摄像头目标帧率，个别帧会拿到与上一帧相同的 IMU 值（不是对齐错误，是当时 IMU 数据确实没变）。想减少这种情况，可以把设备采样率调高于摄像头帧率（比如采集阶段用 50Hz），采集到的高频数据之后可以重采样降到最终部署速率；部署时训练和推理仍应使用统一的目标采样率。

**采样率建议**：最终设备用多少 Hz（如 16Hz），采集、训练、推理三端都应保持一致，避免因采样率不一致导致特征分布偏移。

### 时间漂移分析

```bash
# WitMotion 漂移分析（三阶段：采集 → 线性补偿 → 再评估）
python wit_drift_analysis.py --name WTSDCL
python wit_drift_analysis.py --name WTSDCL --duration 120 --plot

# HICC 漂移分析
python hicc_drift_analysis.py --address EA:CB:3E:CF:00:1B
python hicc_drift_analysis.py --address EA:CB:3E:CF:00:1B --duration 120 --plot
```

## 注意事项

- **BLE 一次只能连一个**：同一台设备不能同时被两个程序连接。如果连接失败，先确认没有其他程序（官方上位机、手机 App）占用连接。
- **WitMotion 校时**：芯片时间需用官方上位机软件校准，本工具不提供写入校时的功能（WT901 系列协议只读）。
- **HICC 校时**：连接后会自动下发当前北京时间，无需手动操作。`--no-timesync` 可跳过（设备时钟已准确时使用）。
- **Label Studio 时间戳**：`timeFormat` 必须填 `%Y-%m-%d %H:%M:%S.%L`，用 `.` 分隔毫秒而非 `:`，否则 Label Studio 会报解析错误。
