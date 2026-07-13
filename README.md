# witmotion_imu

IMU 数据采集工具集，支持 WitMotion WT901SDCL-BT50 和 HICC_PetCollar 自制设备的 BLE 实时采集、离线文件解析与摄像头同步录制。

## 文件结构

| 文件 | 说明 |
|------|------|
| `ble_utils.py` | 共享 BLE 工具：`HzCounter`、`scan_devices`、`find_device`、`list_services` |
| `wit_parse.py` | WitMotion 协议解析（离线 + BLE）：`parse_packets`、`StreamingByteBuffer`、`parse_one_packet`、`DEFAULT_NOTIFY_CANDIDATES`、`fmt_chip_time_dotms` 等 |
| `hicc_parse.py` | HICC_PetCollar 协议解析：GATT UUID、帧常量、DP 解析、`FrameBuffer`、校时帧构造、`find_tx_uuid`/`find_rx_uuid`/`send_timesync` |
| `hicc_offline_to_labelstudio.py` | HICC 离线日志（`HH:MM:SS.MS,AX,AY,AZ,GX,GY,GZ`）转 Label Studio 格式 CSV |
| `csv_time_slice.py` | 按时间范围截取 Label Studio 格式 CSV 的一段数据 |
| `wit_ble_live.py` | WitMotion BLE 实时采集主程序，导入 `ble_utils` + `wit_parse` |
| `hicc_ble_live.py` | HICC BLE 实时采集主程序，导入 `ble_utils` + `hicc_parse` |
| `wit_drift_analysis.py` | WitMotion 时间漂移分析与线性补偿验证 |
| `hicc_drift_analysis.py` | HICC 时间漂移分析与线性补偿验证 |
| `imu_camera_sync.py` | IMU + 摄像头同步采集（BLE 后台线程 + 主线程 OpenCV） |
| `check_alignment.py` | 校验录制的视频与 CSV 是否严格对齐（帧数/时长/起止时间）；`imu_camera_sync.py` 录制结束会自动调用 |
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

# ffmpeg（系统级，非 pip 包）：imu_camera_sync.py 用于精确对齐的视频写入，
# 需确保在 PATH 中，未安装会自动退化为固定 fps 写入并打印警告
```

```bash
# Windows: choco install ffmpeg  或从 https://ffmpeg.org/download.html 下载后加入 PATH
# macOS:   brew install ffmpeg
# Linux:   sudo apt install ffmpeg
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

### 离线文件解析（HICC_PetCollar）

HICC 设备导出的离线日志是逗号分隔文本，只有"时:分:秒.毫秒"没有日期（例如 `26060314.TXT`）：
```
HH:MM:SS.MS,AX,AY,AZ,GX,GY,GZ
14:23:48.000,1.124950,-7.168772,4.831635,0.092175,0.862847,0.139626
```

```bash
# 输出文件默认跟输入同名同目录，只把扩展名换成 .csv（26060314.TXT -> 26060314.csv）
python hicc_offline_to_labelstudio.py data/26060314.TXT

# 文件名无法自动识别日期时，用 --date 显式指定
python hicc_offline_to_labelstudio.py data/26060314.TXT --date 2026-06-03
```

日期识别规则：文件名形如 `YYMMDDHH`（8位数字，末两位是小时，会跟数据第一行的小时数交叉验证，比如 `26060314.TXT` → 2026-06-03，`14` 与第一行 `14:23:48` 对上）；识别不到就用今天日期并打印警告（相对时间顺序依然正确，只是绝对日期可能不对）。

**时间戳倒退的处理**：只有倒退幅度接近一整天（≥12小时，比如 23:59 → 00:00）才判定为真正跨午夜、日期 +1；如果只是小幅倒退（比如同一分钟内秒数从 59 突然跳回 01，分钟数没变——这是部分 HICC 离线日志里实际出现过的设备端记录异常），会判定为设备日志自身的毛刺而不是跨天，直接丢弃这些行以保证 timestamp 严格递增（Label Studio 的硬性要求），并打印丢弃了多少行。

**真实数据缺口检测**：脚本还会用中位数采样间隔估算正常节奏，把明显超出正常间隔的地方（默认阈值：中位间隔的 5 倍）识别为"设备本身没有记录到数据"的真实缺口并打印出来（区别于上面"脚本主动丢弃的倒退行"）。如果发现大量周期性缺口（比如每隔几秒就丢一小段），说明是设备采集本身不稳定，需要反馈给硬件/固件排查，转换脚本无法凭空补全本来就不存在的数据。

### 按时间范围截取 CSV

从一份 Label Studio 格式的 CSV（`timestamp, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z`）里截取某一段时间的数据，格式不变，方便只标注/训练某个时间段：

```bash
python csv_time_slice.py 26071009.csv "2026-07-10 09:22:57" "2026-07-10 09:24:01"
```

输出文件默认跟输入同目录、同名，只加一个时间范围后缀：`26071009_092257-092401.csv`。起止时间闭区间（含边界），支持带或不带毫秒。也可以用 `-o` 指定输出路径。

### IMU + 摄像头同步采集

**正式采集前建议先探测硬件能力**：摄像头能跑多大分辨率/多高 fps（驱动声称的 vs 实测能跑的），以及 IMU 设备当前实际输出的频率：

```bash
python imu_camera_sync.py --device wit --name WTSDCL --probe
```

输出示例：

```
── 摄像头 0 能力探测 ──
  请求 3840x2160  →  实际 1920x1080
  请求 1920x1080  →  实际 1920x1080
  请求 1280x720   →  实际 1280x720
  请求 640x480    →  实际 640x480
  最大可用分辨率（约）: 1920x1080
  请求  60fps  →  驱动声称  30.0fps  实测真实  29.8fps
  请求  30fps  →  驱动声称  30.0fps  实测真实  29.9fps
  请求  20fps  →  驱动声称  20.0fps  实测真实  19.8fps
  ...

── IMU 设备能力探测（连接 5 秒测量实际频率）──
  当前设备实际输出频率: 约 99.2 Hz
  （这是设备当前配置的频率，不是"最大支持频率"；WitMotion 设备的具体可选档位
  需要在官方上位机软件里查看/修改，一般为 0.2/0.5/1/2/5/10/20/50/100/125/200Hz 等
  离散值，不一定支持任意频率如 16Hz。）
```

用它来判断：如果最终产品的目标频率（比如 16Hz）刚好是 IMU 设备能直接设置的档位，可以直接按该频率采集，训练时不用再降采样；如果不是，就采一个更高的可用档位，训练前再降采样对齐到目标频率。摄像头帧率与 IMU 采样率互相独立，摄像头只要能跑到你需要的观察精度即可，不需要和 IMU 一致。

```bash
# HICC 设备，录制 25fps，不限时（Ctrl+C 停止）
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --cam-fps 25

# HICC 设备，录制 60 秒后自动保存视频 + CSV
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --cam-fps 25 --duration 60

# WitMotion 设备，按名称查找，20fps
python imu_camera_sync.py --device wit --name WTSDCL --cam-fps 20

# 指定摄像头编号（默认 0）
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --camera 1

# 保存不带叠加信息的原始视频（默认叠加 IMU/帧率/延迟信息，方便数据标注）
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --no-save-overlay

# 关闭事件驱动同步，改用固定定时器抓帧（不推荐，仅调试用）
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --no-imu-sync

# 常用：WitMotion 设备，指定摄像头1，录制180秒
python imu_camera_sync.py --device wit --name WTSDCL --cam-fps 20 --duration 180 --camera 1

# 默认预热5秒
python imu_camera_sync.py --device wit --name WTSDCL --duration 60

# 改成8秒
python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --warmup-sec 8

# 关闭预热
python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --warmup-sec 0

# WitMotion 设备，降采样到16Hz，指定摄像头0
python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --resample-hz 16 --camera 0

# HICC 设备，降采样到16Hz，指定摄像头0
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1A --duration 60 --resample-hz 16 --camera 0
```

视频默认叠加 IMU 数值、帧率、imu_lag 等信息（标注时可直观判断数据质量）。

**`--cam-fps`（旧名 `--fps`，仍兼容）只控制摄像头的目标帧率，与 IMU 采样率无关**：IMU 实际采样率完全由设备自身配置决定（比如 WitMotion 上位机设成 100Hz，这里就是 100Hz），跟摄像头帧率是两个独立的东西。采集阶段可以让 IMU 跑得比摄像头快（比如摄像头 20fps + IMU 100Hz），有利于更精确对齐；训练模型前再把 IMU 数据统一降采样到最终产品的实际频率（比如 16Hz）。

**`--warmup-sec`（默认 5 秒）**：摄像头刚打开时自动曝光/白平衡还没收敛，帧率容易不稳定；IMU 刚连接也可能有积压或抖动。录制模式下会先预热这么多秒（持续抓帧丢弃、不写入任何文件），确认摄像头/IMU 都稳定后再正式开始计时，文件名时间戳也是预热结束后的真实时刻。想关闭预热设 `--warmup-sec 0`。

**输出文件（每次录制生成 6 个文件）：**

| 文件 | 内容 |
|------|------|
| `{base}.mp4` | 视频（默认带叠加信息） |
| `{base}.csv` | Label Studio 兼容格式，**按视频帧对齐**：`timestamp, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z`（行数=视频帧数） |
| `{base}_meta.csv` | 全量对齐信息：`frame_idx, cam_timestamp, imu_timestamp, imu_lag_ms, imu_missing, acc/gyro, cam_fps, imu_hz` |
| `{base}_raw.csv` | **原始 IMU 全量流水**，不受摄像头帧率影响，每条真实到达的样本都记录：`pc_ms, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z` |
| `{base}_resampled{HZ}hz.csv` | 用 `--resample-hz` 指定的目标频率对 `_raw.csv` 降采样后的结果，Label Studio 兼容格式，**与摄像头帧率无关**，起止时间已裁到与视频一致 |
| `{base}_resampled{HZ}hz.mp4` | `{base}.mp4` 的原样复制，**文件名（去掉扩展名）与上面的 resampled CSV 完全一致**——Label Studio 靠同名文件配对视频和时间序列，这样才能把这一对文件传上去标注 |

**如果要用降采样版本标注**（标完直接对应训练要用的数据，不用再等 `{base}.csv` 转换）：把 `{base}_resampled{HZ}hz.mp4` 和 `{base}_resampled{HZ}hz.csv` 一起传到 Label Studio，两者时间轴严格对齐（首尾时刻完全一致，中间每个时刻的插值也来自同一台电脑同一个时钟源，不会有画面和数据错位的问题）。

**降采样模式（`--resample-hz`，默认 25Hz）**：录制结束后自动对 `{base}_raw.csv`（IMU 真实到达的完整数据流）做低通滤波 + 线性插值，生成任意目标频率的等间隔 CSV，跟摄像头帧率完全独立。输出会裁到视频第一帧/最后一帧的真实时刻范围内，所以 `{base}_resampled{HZ}hz.csv` 的起止时间、总时长与 `{base}.mp4` 是严格对齐的（`_raw.csv` 本身因为 BLE 数据流启停时刻跟视频帧采集不完全同步，会比视频略宽一点，但裁剪后输出不会带出这部分多余的头尾）。比如这次想要 20Hz 数据、下次想要 15Hz，只需要改这一个参数：

```bash
python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --resample-hz 16
python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --resample-hz 20
```

`{base}.csv`（按视频帧对齐的版本）保留不变，仍然是默认输出，两种模式互不影响，可以按需选用。

**同步模式**：默认摄像头会等待新的 IMU 样本到达后才抓帧（事件驱动），使两条独立时间线（摄像头定时器 vs BLE 到达时间）天然对齐，避免同一个 IMU 样本被多帧复用。`--no-imu-sync` 可切回旧的固定定时器模式（仅供调试对比）。

视频文件第 N 帧与 `{base}_meta.csv` 里 `frame_idx=N` 那一行严格一一对应（按写入顺序保证）。视频默认通过 `ffmpeg` 管道以可变帧率（VFR）写入，每帧的时间戳直接采用写入时刻的真实系统时间，因此**视频总时长天然精确等于真实录制时长**（误差通常 <10ms），与 CSV 完全一致，不需要也无法事后修正。未安装 `ffmpeg` 时会打印警告并退化为固定 fps 写入，此时播放时长可能有 0.1s 级别误差（帧与 CSV 行的对应关系依然准确，只是播放时长显示有偏差）。建议安装 `ffmpeg` 并确保它在 `PATH` 中。

**关于重复复用 IMU 样本**：如果 IMU 采样率低于摄像头目标帧率，个别帧会拿到与上一帧相同的 IMU 值（不是对齐错误，是当时 IMU 数据确实没变）。想减少这种情况，可以把设备采样率调高于摄像头帧率（比如采集阶段用 50Hz），采集到的高频数据之后可以重采样降到最终部署速率；部署时训练和推理仍应使用统一的目标采样率。

**采样率建议**：最终设备用多少 Hz（如 16Hz），采集、训练、推理三端都应保持一致，避免因采样率不一致导致特征分布偏移。

### 校验视频与 CSV 是否对齐

`imu_camera_sync.py` 每次录制（`--duration`）结束都会自动跑两遍校验并打印结果，无需手动运行：
1. **按帧对齐版**（`{base}.mp4` + `{base}.csv`）：要求帧数与 CSV 行数严格相等
2. **降采样版**（`{resampled_base}.mp4` + `{resampled_base}.csv`）：行数由 `--resample-hz` 决定，不要求等于帧数，只检查时长和起止时间

也可以事后单独对某次录制手动复查（脚本会自动识别文件名里是否含 `_resampled`，选用对应的校验标准）：

```bash
python check_alignment.py data/wit_d534e2b96f32_20260703_105514
python check_alignment.py data/wit_d534e2b96f32_20260703_105514_resampled25hz
# 或直接传 .mp4 / .csv 路径，会自动推导 base
python check_alignment.py data/wit_d534e2b96f32_20260703_105514.mp4
```

输出示例：

```
【视频】data/wit_d534e2b96f32_20260703_105514.mp4
  帧数:   545
  fps:    18.86
  时长:   28.90s
  起始:   2026-07-03 10:55:14.123
  结束:   2026-07-03 10:55:43.021
  （起止时间来源: meta.csv (逐帧真实时间戳)）

【CSV】data/wit_d534e2b96f32_20260703_105514.csv
  行数:   545
  起始:   2026-07-03 10:55:14.123
  结束:   2026-07-03 10:55:43.021
  时长:   28.90s

── 对齐结果 ──
✔ 帧数与 CSV 行数一致: 545
✔ 时长基本一致: 视频 28.90s vs CSV 28.90s (差 0.001s)
✔ 起止时间基本一致（起始差 0.000s，结束差 0.000s）
```

视频文件本身不含绝对时间戳，脚本会优先读取同目录的 `{base}_meta.csv` 拿到每帧真实的系统时间作为视频起止时间（最准确）；如果没有 `_meta.csv`，退化为从文件名解析录制起始时间 + 视频时长估算（仅精确到秒）。

判定标准：**帧数与 CSV 行数必须严格相等**（一帧一行是硬性对齐条件）；时长、起止时间允许有 ≤0.5s 的误差（正常安装 ffmpeg 的情况下通常在 10ms 以内；若 ffmpeg 不可用会退化为固定 fps 写入，此时时长可能有 0.1s 级别误差，不代表帧错位）。

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
