# witmotion_imu

IMU 数据采集工具集，支持 WitMotion WT901SDCL-BT50 和 HICC_PetCollar 自制设备的 BLE 实时采集、离线文件解析与摄像头同步录制。

## 文件结构

| 文件 | 说明 |
|------|------|
| `ble_utils.py` | 共享 BLE 工具：`HzCounter`、`scan_devices`、`find_device`、`list_services` |
| `wit_parse.py` | WitMotion 协议解析（离线 + BLE）：`parse_packets`、`StreamingByteBuffer`、`parse_one_packet`、`DEFAULT_NOTIFY_CANDIDATES`、`fmt_chip_time_dotms` 等 |
| `hicc_parse.py` | HICC_PetCollar 协议解析：GATT UUID、帧常量、DP 解析、`FrameBuffer`、校时帧构造、`find_tx_uuid`/`find_rx_uuid`/`send_timesync` |
| `hicc_offline_to_labelstudio.py` | HICC 离线日志（`HH:MM:SS.MS,AX,AY,AZ,GX,GY,GZ`）转 Label Studio 格式 CSV |
| `check_periodic_gaps.py` | 检测 HICC 离线 TXT / Label Studio CSV 数据里是否存在周期性缺口 |
| `csv_time_slice.py` | 按时间范围截取 Label Studio 格式 CSV 的一段数据 |
| `wit_ble_live.py` | WitMotion BLE 实时采集主程序，导入 `ble_utils` + `wit_parse`；支持 `--hourly` 长期采集（整点自动切换CSV文件） |
| `hicc_ble_live.py` | HICC BLE 实时采集主程序，导入 `ble_utils` + `hicc_parse`；支持 `--hourly` 长期采集（整点自动切换CSV文件） |
| `wit_drift_analysis.py` | WitMotion 时间漂移分析与线性补偿验证 |
| `hicc_drift_analysis.py` | HICC 时间漂移分析与线性补偿验证 |
| `imu_camera_sync.py` | IMU + 摄像头同步采集（BLE 后台线程 + 主线程 OpenCV） |
| `imu_camera_sync_multi.py` | 一个摄像头 + 多个 IMU 设备同步采集，功能已跟 `imu_camera_sync.py` 对齐（含 `--loop`/`--resample-hz`/`--probe`/`--resample-only`） |
| `imu_camera_sync_multicam.py` | 多个摄像头 + 多个 IMU 设备同步采集，功能已跟 `imu_camera_sync.py`/`imu_camera_sync_multi.py` 对齐（含 `--loop`/`--resample-hz`/`--probe`/`--resample-only`），复用 `imu_camera_sync_multi.py` 的 BLE 部分 |
| `imu_camera_sync_rtsp.py` | IMU + RTSP摄像头（[micam_dev](https://github.com/zhuyetuo/micam_dev)/go2rtc 转出来的小米摄像头流）同步采集，跟 `imu_camera_sync.py` 功能一致（含 `--loop`/`--resample-hz`/`--probe`/`--resample-only`），直接复用 `imu_camera_sync.py` 的 BLE/CSV/降采样/对齐校验逻辑，只是摄像头来源换成 RTSP |
| `imu_camera_sync_rtsp_multicam.py` | 多路RTSP摄像头 + 多个IMU设备同步采集，`imu_camera_sync_rtsp.py`（单路RTSP）+ `imu_camera_sync_multicam.py`（多本地摄像头）的结合体，每路RTSP流可以有各自不同的延迟补偿值 |
| `check_multi_imu_quality.py` | 统计 `imu_camera_sync_multi.py` 生成的 `_meta.csv` 里各设备的 lag/missing/hz 质量 |
| `check_alignment.py` | 校验录制的视频与 CSV 是否严格对齐（帧数/时长/起止时间）；`imu_camera_sync.py` 录制结束会自动调用 |
| `cleanup_resampled_pairs.sh` | 清理 multicam 系列脚本生成的 `{base}_camX_imuY_resampled{HZ}hz.mp4/.csv` 配对文件，只保留指定的几组组合，其余全删 |
| `merge_hourly_segments.py` | 把 `--loop` 循环录制产生的一堆按小段切分的 resampled mp4/csv，按文件名时间戳合并成每小时一份，合并前逐段诊断视频/CSV时长是否一致，带进度条（推荐用这个） |
| `merge_hourly_segments.sh` | 同上功能的 shell 版，逻辑更简单（没有逐段诊断），依然保留可用 |
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

# 长期采集模式：每到整点自动切换新CSV文件，文件名 YYYYMMDDHH_设备名.csv（如 2026071309_WT901BLE68.csv）
python wit_ble_live.py --name WTSDCL --hourly

# 长期采集，指定输出目录（默认 data/）
python wit_ble_live.py --name WTSDCL --hourly --hourly-dir data/wit_hourly

# 状态提示打印间隔改成 5 分钟（默认 60 秒）
python wit_ble_live.py --name WTSDCL --hourly --status-interval 300

# 完全不打印"已接收 N 帧"状态提示
python wit_ble_live.py --name WTSDCL --hourly --quiet
```

**长期采集模式（`--hourly`）**：适合挂机长时间采集。每到整点（PC 系统时间）自动关闭当前文件、新开一个文件，文件名 `YYYYMMDDHH_设备名.csv`（比如 `2026071309_WT901BLE68.csv` 表示 2026-07-13 09点这一小时、设备名为 WT901BLE68 的数据；设备名后缀方便同时挂多台设备采集时文件不冲突），避免单个文件过大，也避免程序意外中断导致这一整段时间的数据全部丢失（顶多丢当前这一小时还没切换的部分）。此模式下 `-o/--output` 不生效。

**常见问题**

- **上位机设置采样率 200Hz，实际收到只有 ~100Hz**：这不是脚本的 bug，而是 BLE 传输带宽的限制。上位机软件里设置的采样率通常只决定设备内部传感器/SD卡本地记录的频率，实际通过 BLE notify 推送到电脑的频率还受 BLE 连接间隔（connection interval）限制——Windows 协商的连接间隔一般在 10~30ms 左右，如果设备每个连接间隔只发一包，理论上限大约就是 ~100Hz，配置成 200Hz 时设备内部会按 200Hz 采样但传输时隔帧丢弃/合并，或者干脆传输跟不上。这是 WitMotion BLE 模组的共性限制，不同型号（含 BWT901BLECL5.0）都可能遇到，不是解析出错。用 `--calibrate` 或者观察打印出来的 Hz 数值可以确认脚本本身收发是正常工作的。
- **CSV 文件写入是空的（0 帧）**：如果设备芯片时钟从来没有被官方上位机校准过（year/month/day 字段无效），`chip_time` 会恒为 `None`。旧版本代码把"`chip_time` 无法解析"和"蓝牙重连导致的时钟复位坏帧"混在一起处理，导致这类设备的所有数据都被当坏帧丢弃，写入 0 帧。已修复：CSV 实际写入用的时间戳始终是 PC 系统时间，与 `chip_time` 是否有效无关；`chip_time` 现在只用于检测重连坏帧（值非单调递增才丢弃），为 `None` 时正常写入。

**状态提示打印**：默认每 60 秒打印一次"已接收 N 帧"状态（`--status-interval` 可调整间隔）；采样率高（比如 100Hz）时按帧数打印会刷屏太快，改成按时间间隔打印。完全不想看到这类提示可以加 `--quiet`（连接、丢帧、整点切换等关键信息仍会打印）。

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

# 长期采集模式：每到整点自动切换新文件对，文件名 YYYYMMDDHH_6axis.csv / _env.csv
python hicc_ble_live.py --address EA:CB:3E:CF:00:1B --hourly

# 长期采集，指定输出目录、调整状态打印间隔、或完全静默
python hicc_ble_live.py --address EA:CB:3E:CF:00:1B --hourly --hourly-dir data/hicc_hourly
python hicc_ble_live.py --address EA:CB:3E:CF:00:1B --hourly --status-interval 300
python hicc_ble_live.py --address EA:CB:3E:CF:00:1B --hourly --quiet
```

**长期采集模式（`--hourly`）**：跟 `wit_ble_live.py` 的 `--hourly` 设计一致。每到整点自动关闭当前的六轴/环境两个文件、新开一对文件（`YYYYMMDDHH_6axis.csv` / `YYYYMMDDHH_env.csv`），此模式下 `-o/--output` 不生效，且写文件时不再逐帧打印（HICC 六轴帧 25Hz，逐帧打印刷屏太快），改成按 `--status-interval`（默认60秒）打印一次状态摘要，`--quiet` 可完全关闭状态提示。不写文件的纯打印模式（不加 `-o`/`--hourly`）行为不变，仍然逐帧打印详情。

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

# 批量模式：传一个目录，转换该目录下所有 .TXT 文件（输出跟输入同目录）
python hicc_offline_to_labelstudio.py data/hicc/pp

# 批量模式，指定统一的输出目录
python hicc_offline_to_labelstudio.py data/hicc/pp -o data/hicc/pp_csv
```

日期识别规则：文件名形如 `YYMMDDHH`（8位数字，末两位是小时，会跟数据第一行的小时数交叉验证，比如 `26060314.TXT` → 2026-06-03，`14` 与第一行 `14:23:48` 对上）；识别不到就用今天日期并打印警告（相对时间顺序依然正确，只是绝对日期可能不对）。

**时间戳倒退的处理**：只有倒退幅度接近一整天（≥12小时，比如 23:59 → 00:00）才判定为真正跨午夜、日期 +1；如果只是小幅倒退（比如同一分钟内秒数从 59 突然跳回 01，分钟数没变——这是部分 HICC 离线日志里实际出现过的设备端记录异常），会判定为设备日志自身的毛刺而不是跨天，直接丢弃这些行以保证 timestamp 严格递增（Label Studio 的硬性要求），并打印丢弃了多少行。

**真实数据缺口检测**：脚本还会用中位数采样间隔估算正常节奏，把明显超出正常间隔的地方（默认阈值：中位间隔的 5 倍）识别为"设备本身没有记录到数据"的真实缺口并打印出来（区别于上面"脚本主动丢弃的倒退行"）。如果发现大量周期性缺口（比如每隔几秒就丢一小段），说明是设备采集本身不稳定，需要反馈给硬件/固件排查，转换脚本无法凭空补全本来就不存在的数据。

### 单独检测数据缺口是否呈周期性

`hicc_offline_to_labelstudio.py` 转换时会顺带报一次缺口，但如果只是想单独检查某份数据（HICC 离线 TXT 或已生成的 Label Studio CSV 都可以），或者想调整判定阈值，用 `check_periodic_gaps.py`：

```bash
python check_periodic_gaps.py data/26071009.TXT
python check_periodic_gaps.py data/26071009.csv
python check_periodic_gaps.py data/26071009.TXT --gap-ratio 3 --max-print 30
```

会自动按文件表头识别是 HICC 离线 TXT 还是 Label Studio CSV 格式，统计缺口数量/时长分布，并进一步分析这些缺口的"复发间隔"是否有规律（用中位数+绝对中位差而不是均值/标准差，避免个别超大缺口把统计结果带偏），给出强周期性/有一定规律/不规律三档判断。发现强周期性缺口通常意味着设备采集端本身有规律性卡顿，值得反馈给硬件/固件排查。

**验收判定**：脚本最后会给出正式的验收结论（阈值可调），厂家交样机测试时可以直接用这个结论判断能不能过：

| 丢包率（缺口总时长 / 总采集时长） | 判定 |
|---|---|
| < 1%（`--loss-excellent`） | 优秀，通过 |
| 1% ~ 3%（`--loss-warn`） | 合格，通过 |
| 3% ~ 5%（`--loss-fail`） | 有条件通过，建议关注 |
| ≥ 5% | 不通过 |
| 检测到强周期性缺口（稳健变异系数 < `--periodic-cv-threshold`，默认0.15） | **无论丢包率多低都直接不通过** |

默认阈值参考行业惯例：临床级可穿戴设备 QC 常用 5% 数据缺失作为验收线，优化良好的 BLE 可穿戴系统能做到 <1% 丢包率。周期性缺口之所以无条件判不通过，是因为它说明的是固件/硬件系统性问题（比如每隔几秒规律性卡顿丢包），不是偶发噪声，不会因为多测几次就消失，长期使用会持续复现——这种情况不应该靠"丢包率还没超阈值"侥幸通过验收，应该退回厂家整改。

```bash
# 自定义阈值示例：丢包率超过 2% 就判不通过
python check_periodic_gaps.py data/26071009.TXT --loss-fail 2
```

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

# 指定分辨率（默认就是 720p: 1280x720，驱动不支持时会自动退化并打印警告）
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --width 1280 --height 720
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1B --width 1920 --height 1080

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

# 循环录制：每段1分钟，录完自动开始下一段，直到按 Q/ESC 或 Ctrl+C 才停止
python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --loop

# 循环录制，每段3分钟
python imu_camera_sync.py --device wit --name WTSDCL --duration 180 --loop

# 循环录制，每段3分钟，降采样到16Hz，720p
python imu_camera_sync.py --device wit --name WTSDCL --duration 180 --resample-hz 16 --camera 0 --width 1280 --height 720 --loop

# 指定保存目录（默认 data/，不存在会自动创建）
python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --out-dir data/session1

# 只保留降采样版文件（resampled mp4/csv），其余中间文件自动删除
python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --resample-hz 16 --resample-only

# 常用组合：WitMotion，循环录制每段3分钟，降采样到16Hz，720p，只保留降采样文件，指定保存目录
python imu_camera_sync.py --device wit --name WTSDCL --duration 180 --resample-hz 16 --camera 0 --width 1280 --height 720 --loop --resample-only --out-dir data/imu_video

# 常用组合：HICC，同上
python imu_camera_sync.py --device hicc --address EA:CB:3E:CF:00:1A --duration 180 --resample-hz 16 --camera 0 --width 1280 --height 720 --loop --resample-only --out-dir data/imu_video
```

视频默认叠加 IMU 数值、帧率、imu_lag 等信息（标注时可直观判断数据质量）。

**循环录制模式（`--loop`）**：跟 `--duration` 配合使用，每段录制到时自动结束当前片段（各自生成一套完整的 mp4/csv/meta/raw/resampled 文件）、马上开始下一段，一直循环下去，直到你按 `Q`/`ESC` 或 `Ctrl+C` 才真正停止。摄像头和 BLE 连接全程保持不断开、不重连，只在片段边界切换输出文件，适合需要长时间采集但又想按固定时长切片（比如每 1 分钟或 3 分钟一段）方便后续分别标注的场景。

**`--out-dir`（默认 `data`）**：指定录制文件的保存目录，目录不存在会自动创建。

**`--resample-only`**：配合 `--resample-hz` 使用，只保留降采样版的 `{resampled_base}.mp4` / `.csv`，把按帧对齐版的 `{base}.mp4/.csv/_meta.csv/_raw.csv` 全部删掉，省磁盘空间。对齐校验（两个版本）都会先正常跑完再删除文件，不影响校验结果的准确性。

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

**视频体积**：默认优先用 H.264（`libx264`）编码，比旧版默认的 `mpeg4` 压缩率高很多，同画质下体积通常只有 1/5~1/10。用 `--video-crf` 调整压缩质量（默认 28，数值越大文件越小、画质越差，标注用途 23~30 都够用），比如：

```bash
python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --video-crf 32   # 文件更小
python imu_camera_sync.py --device wit --name WTSDCL --duration 60 --video-crf 20   # 画质更好，文件更大
```

如果 ffmpeg 没有 `libx264` 支持（少见），会自动退化用 `mpeg4`（体积明显更大）。

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

### 一个摄像头 + 多个 IMU 设备同步采集

`imu_camera_sync_multi.py` 是 `imu_camera_sync.py` 的多设备版本，同一路摄像头同时对齐多台 IMU（WitMotion 和/或 HICC 混用都可以）。功能已经跟单设备版本对齐，`--loop`/`--resample-hz`/`--probe`/`--resample-only`/`--no-save-overlay`/`--no-imu-sync` 全部支持，用法和含义跟 `imu_camera_sync.py` 一致，只是每个设备各自一份数据。

**自动重连**：如果设备戴在狗脖子上，随着活动项圈会慢慢移位、趴地时天线被压住等情况，BLE 信号变差到一定程度会导致连接被判定为真正断开（不只是丢几个包）。断开后脚本会自动每 2 秒尝试重新扫描/连接，一直重试到 `--duration` 结束或手动停止，信号恢复后会自动续上继续采集，不会因为中途断过一次连接就永远显示 `imu_missing`。（`imu_camera_sync_multicam.py` 复用同一套连接逻辑，同样支持自动重连。）

**断连缺口不会被插值编数据**：`--resample-hz` 降采样输出（`{base}_resampled{HZ}hz.csv`）用的是低通滤波 + 线性插值，但如果 BLE 真的断连了一段时间（比如趴地导致信号丢失超过1分钟），这段时间是真实没有数据的，不能靠插值凭空编出平滑的假动作数据去训练模型。脚本会先算出真实采样点之间的时间间隔中位数，凡是某个降采样目标时间点两侧最近的真实样本间隔明显超出正常间隔（默认超过中位数的5倍），就判定这个点落在真实断连缺口内，直接留空而不是插值，控制台会提示留空的行数。逐帧对齐的 `{base}.csv` 本来就有 `imu_missing` 字段标记缺失，行为不受影响；此改动只影响降采样输出。

```bash
# 一个摄像头 + 2 个 WitMotion 设备
python imu_camera_sync_multi.py --imu wit=WTSDCL1 --imu wit=WTSDCL2 --duration 60

# 1个 WitMotion + 1个 HICC 混用（HICC 必须用 MAC 地址）
python imu_camera_sync_multi.py --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --duration 60

# WitMotion 也可以直接用 MAC 地址指定，不用按名称模糊匹配
python imu_camera_sync_multi.py --imu wit=D5:34:E2:B9:6F:32 --imu hicc=EA:CB:3E:CF:00:1A --duration 60

# 探测硬件能力：摄像头 + 每个 IMU 设备当前实际输出频率
python imu_camera_sync_multi.py --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --probe

# 每个设备都降采样到16Hz，只保留降采样版文件，循环录制每段3分钟
python imu_camera_sync_multi.py --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --duration 180 --resample-hz 16 --resample-only --loop

# 常用组合：WitMotion + HICC，循环录制每段1分钟，降采样到16Hz，720p，只保留降采样文件，指定保存目录
python imu_camera_sync_multi.py --imu wit=WT901BLE68 --imu hicc=EA:CB:3E:CF:00:1A --duration 60 --resample-hz 16 --camera 0 --width 1280 --height 720 --loop --resample-only --out-dir data/multi_imu
```

`--imu` 可重复传，格式为 `类型=标识`：
- `wit=<名称关键字或MAC地址>`（自动识别标识是不是 MAC 格式）
- `hicc=<MAC地址>`（HICC 必须用 MAC 地址）

第一个 `--imu` 对应 `imu1`，第二个对应 `imu2`，以此类推，输出的列名/文件名都用这个编号区分。

**输出文件：**

| 文件 | 内容 |
|---|---|
| `{base}.mp4` | 视频（VFR，含叠加信息，同时显示每个设备的 Hz/lag） |
| `{base}.csv` | 每帧一行：`timestamp, imu1_acc_x...imu1_gyro_z, imu2_acc_x...imu2_gyro_z, ...` |
| `{base}_meta.csv` | 每帧一行，每个设备的 `imu_timestamp/lag_ms/missing/hz` 等对齐信息 |
| `{base}_imu1_raw.csv`、`{base}_imu2_raw.csv`... | 各设备的原始 IMU 全量流水，不受摄像头帧率影响 |
| `{base}_imu1_resampled{HZ}hz.csv/.mp4`... | 每个设备各自降采样后的 CSV + 配对视频副本（`--resample-hz` 指定目标频率） |

录制结束会自动调用 `check_alignment.py` 校验视频帧数与组合 CSV 行数是否一致、起止时间是否对齐（跟 `imu_camera_sync.py` 一样），并对每个设备的降采样版文件对分别再跑一次校验。

**查看各设备的对齐质量**：`check_alignment.py` 只验证视频和 CSV 整体是否对齐，不区分是哪个设备的问题。想单独看每个设备的 lag_ms 分布、missing 比例、hz 是否达标，用 `check_multi_imu_quality.py`：

```bash
python check_multi_imu_quality.py data/multi_20260714_164918_meta.csv
```

输出示例：

```
总帧数: 1201
识别到 2 个设备: imu1, imu2

── imu1 ──
  imu_missing: 12/1201 (1.0%)
  imu_hz: mean=50.0  min=48.0  max=52.0
  lag_ms: min=1.2  max=85.3  mean=22.4  median=18.5
    <=  10 ms:   320 帧 (26.9%)
    <=  20 ms:   612 帧 (51.5%)
    ...

── imu2 ──
  imu_missing: 3/1201 (0.3%)
  ...
```

### 多个摄像头 + 多个 IMU 设备同步采集

`imu_camera_sync_multicam.py` 在 `imu_camera_sync_multi.py`（一个摄像头+多IMU）基础上再扩展一维，支持同时开多路摄像头，每路摄像头独立写视频，所有摄像头 + 所有 IMU 设备共用同一份"每个tick一行"的组合 CSV（同一时刻同时抓取所有摄像头画面 + 匹配所有 IMU 设备最近的样本）。IMU 部分复用 `imu_camera_sync_multi.py` 的 `ImuDevice`/BLE 连接逻辑，`--imu` 用法完全一样。功能已跟单摄像头版本对齐，`--loop`/`--resample-hz`/`--probe`/`--resample-only`/`--no-save-overlay`/`--no-imu-sync` 全部支持。

```bash
# 2个摄像头 + 2个IMU设备
python imu_camera_sync_multicam.py --camera 0 --camera 1 --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --duration 60

# 探测硬件能力：每路摄像头 + 每个 IMU 设备实际输出频率
python imu_camera_sync_multicam.py --camera 0 --camera 1 --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --probe

# 每个设备降采样到16Hz，只保留降采样版文件，循环录制每段3分钟
python imu_camera_sync_multicam.py --camera 0 --camera 1 --imu wit=WTSDCL --imu hicc=EA:CB:3E:CF:00:1A --duration 180 --resample-hz 16 --resample-only --loop

# 常用组合：2路摄像头 + WitMotion + HICC，降采样到16Hz，只保留降采样文件，预热10秒，循环录制每段1分钟，指定保存目录
python imu_camera_sync_multicam.py --imu wit=WT901BLE68 --imu hicc=EA:CB:3E:CF:00:1A --duration 60 --resample-hz 16 --camera 0 --camera 1 --width 1280 --height 720 --loop --resample-only --out-dir data/multicam_multiimu --warmup-sec 10
```

`--camera` 可重复传，第一个对应 `cam1`，第二个对应 `cam2`，以此类推。

**先探测摄像头实际支持的帧率，再决定 `--cam-fps`**：不同摄像头硬件支持的帧率不一样（有的到20fps封顶，有的能到60fps），录制前建议先用 `--probe` 看一眼实际能力（顺带也会连一下每个IMU设备测一下各自实际输出频率，一次性看全）：

```bash
python imu_camera_sync_multicam.py --camera 0 --imu wit=WT901BLE68 --imu wit=WTSDCL --probe
```

确认摄像头能跑到你想要的帧率后，用 `--cam-fps` 指定目标帧率（默认 20，比如改成 25）：

```bash
python imu_camera_sync_multicam.py --imu wit=WT901BLE68 --imu wit=WTSDCL --duration 10 --resample-hz 16 --camera 0 --width 1280 --height 720 --cam-fps 25 --loop --resample-only --out-dir data/multicam_multiimu --warmup-sec 1
```

**输出文件：**

| 文件 | 内容 |
|---|---|
| `{base}_cam1.mp4`、`{base}_cam2.mp4`... | 每路摄像头各自的视频（VFR，含叠加信息） |
| `{base}.csv` | 每个tick一行：`timestamp, imu1_acc_x...imu1_gyro_z, imu2_acc_x...imu2_gyro_z, ...` |
| `{base}_meta.csv` | 每行的对齐信息：各摄像头的 fps，各IMU设备的 lag_ms/missing/hz |
| `{base}_imu1_raw.csv`、`{base}_imu2_raw.csv`... | 各 IMU 设备的原始全量流水 |
| `{base}_cam1_imu1_resampled{HZ}hz.mp4/.csv`... | 每路摄像头 x 每个设备的降采样配对文件（`--resample-hz` 指定目标频率） |

由于每个 tick 都是同时抓取所有摄像头一帧，所以每路视频的帧数理论上都应该严格等于组合 CSV 的行数；录制结束会自动逐个摄像头核对这一点（不复用 `check_alignment.py`，因为它假设视频和 CSV 同名成对，这里是 N 路视频共享 1 份 CSV，命名规则不满足它的假设）。

**降采样配对**：每个 IMU 设备降采样后的 CSV，会跟**每一路摄像头**的视频各配一份（`{base}_camX_imuY_resampled{HZ}hz.mp4/.csv`），比如 2 路摄像头 + 2 个设备会生成 4 组配对文件，方便挑任意一路摄像头画面配任意一个设备的数据去 Label Studio 标注。`--resample-only` 会在生成完所有配对文件后，删除原始的 `{base}_camN.mp4`、`{base}.csv`、`{base}_meta.csv`、`{base}_imuN_raw.csv`。

### IMU + RTSP摄像头（micam_dev/go2rtc）同步采集

`imu_camera_sync_rtsp.py` 跟 `imu_camera_sync.py` 功能完全一样（BLE采集、事件驱动对齐、VFR视频写入、`--resample-hz`降采样、断连缺口留空、录制结束自动跑`check_alignment.py`等），唯一区别是摄像头来源不是本地 USB 摄像头，而是 [micam_dev](https://github.com/zhuyetuo/micam_dev)（go2rtc）转出来的小米摄像头 RTSP 流。IMU/CSV/降采样/对齐校验逻辑直接 `import imu_camera_sync` 复用，不重复实现。

```bash
# 先探测 RTSP 流实际能拿到的分辨率/帧率 + IMU 实际输出频率
python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --probe --device wit --name WTSDCL

# 录制60秒
python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL --duration 60

# 降采样到16Hz，循环录制，只保留降采样版
python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL \
    --duration 60 --resample-hz 16 --loop --resample-only --out-dir data/rtsp

# 画面统一缩放到指定尺寸（RTSP流本身只有几档固定质量，不支持任意分辨率）
python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL --resize 1280x720
```

**关于延迟**：RTSP over TCP + FFmpeg 默认会做内部缓冲，实测常见 1~2秒 延迟（分辨率越高越明显）。本脚本参考 `micam_dev/scripts/capture_frame.py` 的两个做法把延迟降下来：
1. 用 `OPENCV_FFMPEG_CAPTURE_OPTIONS` 环境变量告诉 FFmpeg 后端关闭内部缓冲（`fflags=nobuffer`、`flags=low_delay`），这是延迟的大头；
2. 用后台线程持续读流，主循环永远拿"最新一帧"而不是排队处理堆积的旧帧（`LatestFrameReader`）。

即便这样，RTSP 链路（摄像头编码→网络→go2rtc转发→FFmpeg解码）本身还是会比本地USB摄像头多几十到几百毫秒延迟，这是链路结构决定的，不是脚本能完全消除的；如果还嫌延迟大，可以到 go2rtc 端确认用的是较低分辨率/码率的 subtype（`capture_frame.py` 注释里提到摄像头只提供几档固定质量 `subtype=0-5`，不是任意分辨率）。

**这个延迟会不会影响标注（画面动作 vs 配对的IMU数值对不对得上）**：会，而且是需要处理的问题——跟本地USB摄像头不一样。同步逻辑是拿"收到这一帧的PC时刻"去IMU缓冲区找最近的样本；本地摄像头传输延迟接近0，"收到时刻"≈"画面里动作真实发生的时刻"，没问题。但RTSP有真实的编码+网络+解码延迟，"收到这一帧"的时候，画面内容其实是**之前**发生的，而IMU是BLE直连、近似实时——如果不做任何处理，配对给这一帧的IMU数值会比画面里的动作提前了一个"RTSP延迟"的量，是系统性偏差，不是随机噪声，标注时会看到画面和数值对不上。

**延迟补偿配置文件**：延迟值需要自己测出来（比如对着摄像头把设备晃一下，人工对比视频里动作出现的时刻和IMU数据里加速度突变的时刻差多少毫秒），然后手动写进一个 JSON 配置文件，按 `host:port/stream` 分别配置：

```json
{
  "192.168.2.140:8554/cam0": {"latency_ms": 700}
}
```

默认读脚本同目录下的 `.rtsp_latency_cache.json`（可以用 `--latency-config-file` 指定别的路径），**每次运行会自动读取这个文件、按当前 `--host`/`--port`/`--stream` 匹配对应的延迟值并应用，不用在命令行传任何延迟相关参数**：

```bash
# 配置文件里已经配好 192.168.2.140:8554/cam0 的延迟值，直接录，自动应用
python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL --duration 60
```

也可以用 `--video-latency-ms` 在命令行直接指定一个值，优先级比配置文件高：

```bash
python imu_camera_sync_rtsp.py --host 192.168.2.140 --stream cam0 --device wit --name WTSDCL \
    --duration 60 --video-latency-ms 700
```

**注意：实测RTSP延迟会随网络状况波动，不是固定值**，配置文件里写死的补偿值只在测量那一刻准确，之后可能有偏差；标注精度要求高的场景目前更推荐用本地USB摄像头版本（`imu_camera_sync.py`），延迟接近0更稳定。详见下面"多路 RTSP 摄像头"一节的说明。

### 多路 RTSP 摄像头 + 多个 IMU 设备同步采集

`imu_camera_sync_rtsp_multicam.py` 是 `imu_camera_sync_rtsp.py`（单路RTSP）+ `imu_camera_sync_multicam.py`（多本地摄像头）的结合体：多路 RTSP 流（共用同一个 `--host`/`--port`，`--stream` 重复传）+ 多个 IMU 设备同步采集。IMU 采集逻辑直接复用 `imu_camera_sync_multi.py`，RTSP 低延迟读取复用 `imu_camera_sync_rtsp.py`。

```bash
# 2路RTSP流（cam0/cam1） + 1个IMU设备
python imu_camera_sync_rtsp_multicam.py --host 192.168.2.140 --stream cam0 --stream cam1 \
    --imu wit=WT901BLE68 --duration 60

# 降采样到16Hz，循环录制，只保留降采样版
python imu_camera_sync_rtsp_multicam.py --host 192.168.2.140 --stream cam0 --stream cam1 \
    --imu wit=WT901BLE68 --duration 60 --resample-hz 16 --loop --resample-only \
    --out-dir data/rtsp_multicam --warmup-sec 10

# 探测每路流的分辨率/帧率 + IMU 实际输出频率
python imu_camera_sync_rtsp_multicam.py --host 192.168.2.140 --stream cam0 --stream cam1 \
    --imu wit=WT901BLE68 --probe

# 2个不同IMU设备（比如两只狗各戴一个collar）+ 2路摄像头，画面统一缩放到720p
python imu_camera_sync_rtsp_multicam.py --host 192.168.2.140 --stream cam0 --stream cam1 \
    --imu wit=设备1名称 --imu wit=设备2名称 \
    --duration 10 --resample-hz 16 --loop --resample-only \
    --out-dir data/rtsp_multicam --warmup-sec 10 --resize 1280x720
```

`--stream` 可重复传，第一个对应 `cam1`，第二个对应 `cam2`，以此类推。`--imu` 同理可重复传多个（比如两只狗各戴一个IMU设备），每路摄像头 x 每个设备都会各自生成一对降采样配对文件（N路摄像头 × M个设备 = N×M对）。

**上传到 Label Studio 前先确认项目模板要几个视角/几个IMU**：如果项目的标注模板是"2视角+2个独立IMU"（比如两只狗各自的摄像头角度+各自的collar数据），录制时就要用2个 `--imu`；如果只是"2个摄像头角度拍同一只狗、只有1个IMU"，录制用1个 `--imu` 就够了，但要确保上传时选的项目模板也是"单IMU"的（不然会因为 `csv`/`csv1`+`csv2` 这个key数量对不上而导入失败）——这不是文件命名的问题，是录制时用的设备数量要跟目标项目模板的设计场景匹配。

**每路流各自的延迟补偿**：不同RTSP流的链路延迟可能不一样（不同摄像头/网络路径），所以延迟补偿是按每一路流单独配置的，读取方式和文件跟 `imu_camera_sync_rtsp.py` 完全一样——同一个 `.rtsp_latency_cache.json`，按 `host:port/stream` 分别配置：

```json
{
  "192.168.2.140:8554/cam0": {"latency_ms": 700},
  "192.168.2.140:8554/cam1": {"latency_ms": 900}
}
```

每次运行自动按各自的 `host:port/stream` 读取对应的值，不用传参数。也可以用 `--video-latency-ms` 给**所有流**统一指定同一个值（会覆盖配置文件里每一路的值）。

**补偿只应用在降采样配对文件上**：不同摄像头延迟不一样，没法在"所有摄像头共用一份"的逐帧组合CSV（`{base}.csv`）里同时精确补偿多路不同的延迟，所以那份文件里的对齐用的是未做延迟补偿的原始抓帧时刻匹配，只作调试/参考用；真正标注请用每路摄像头自己的降采样配对文件（`{base}_camN_imuM_resampled{HZ}hz.mp4/.csv`），这些文件在录制结束后会按各自那一路的延迟值分别正确计算。

**实测：RTSP延迟不稳定，跟网络状况有关**：实际测试下来，RTSP链路的延迟会随网络状况波动（有时候延迟大、有时候延迟小），不是一个能长期固定不变的值，配置文件里写死的补偿值（比如 `latency_ms: 700`）只在测量那一刻的网络条件下准确，之后可能会有偏差。如果标注精度要求高，目前更推荐直接用本地USB摄像头（`imu_camera_sync.py`/`imu_camera_sync_multi.py`/`imu_camera_sync_multicam.py`），延迟接近0、稳定可靠，不需要处理这类链路延迟补偿的问题；RTSP版本适合对时间精度要求没那么严格、或者物理条件必须用网络摄像头（比如小米摄像头）的场景。目前配置文件可以先把 `latency_ms` 都设成 `0`（不补偿）：

```json
{
  "192.168.2.140:8554/cam0": {"latency_ms": 0},
  "192.168.2.140:8554/cam1": {"latency_ms": 0}
}
```

### 清理多余的降采样配对文件

N路摄像头 x M个设备会生成 N×M 组 `{base}_camX_imuY_resampled{HZ}hz.mp4/.csv`，实际标注往往只需要其中几组固定的摄像头/设备组合，用 `cleanup_resampled_pairs.sh` 只保留指定的几组，其余全部删除：

```bash
# 保留 cam1_imu1(mp4+csv)、cam2_imu2(mp4+csv)、cam3_imu1(只留mp4)，其余全删
./cleanup_resampled_pairs.sh data/multicam_multiimu cam1_imu1 cam2_imu2 cam3_imu1:mp4
```

### 把小段录制合并成每小时一份

`--loop` 循环录制（比如每段1分钟一直循环）会产生大量小段的 `{前缀}_YYYYMMDD_HHMMSS_camX_imuY_resampled{HZ}hz.mp4/.csv`，文件太多不好管理。有两个版本，**推荐用 Python 版** `merge_hourly_segments.py`：

```bash
# 合并 data/multicam_multiimu 目录下所有能识别的小段文件（按小时+camX_imuY分组）
# 默认不改动、不删除原始文件，合并结果存到 data/multicam_multiimu/merged/ 子目录
python merge_hourly_segments.py data/multicam_multiimu

# 指定合并结果存到别的目录
python merge_hourly_segments.py data/multicam_multiimu --out-dir data/multicam_multiimu_merged

# 确认合并结果没问题之后，再单独加这个参数删除原始小段文件（默认不删）
python merge_hourly_segments.py data/multicam_multiimu --delete-originals

# 只看诊断表，不做合并——先排查哪些1分钟小段本身视频/CSV时长就对不上
python merge_hourly_segments.py data/multicam_multiimu --mismatch-only

# 合并时跳过诊断表里标✘的段（时长明显不一致的），只合并没问题的段
python merge_hourly_segments.py data/multicam_multiimu --skip-mismatched
```

**跳过有问题的段（`--skip-mismatched`）**：实测常见情况是某几分钟的CSV跨度明显比视频短（比如60秒视频只对应20~40秒的CSV数据），这通常不是合并脚本的锅，而是那一分钟IMU设备本身真的断连了一段时间（比如项圈信号差、狗趴地压住天线）——`resample_raw_imu()` 降采样时会把结果范围裁到"真实收到过IMU数据"的区间内，所以那一分钟的CSV自然就比视频短。加 `--skip-mismatched` 会把这些标✘的段整个跳过（视频+CSV都不参与合并），只合并数据完整的段；被跳过的原始文件不受影响，不会被删除（即使同时加了 `--delete-originals`）。

**Python 版比 shell 版多做的事**：

1. **合并前逐段诊断**：用 opencv 读每个视频段的实际帧数/fps算出真实时长，跟对应CSV的时间跨度做对比，打印一张表。如果合并后"视频比CSV短很多"，通常不是合并脚本的问题，而是某一分钟的原始录制本身视频就比CSV短（比如那一分钟摄像头掉帧/被截断了）——这张诊断表能直接标出是哪一段（打✘的那行），不用合并完了才发现问题、还得反推是哪段导致的。
2. **合并后校验**：对比合并后的视频总时长 vs CSV总时长，明显不一致会打印警告。
3. **进度条**：按段数显示总体处理进度。
4. **不会有 Windows 路径问题**：shell 版在 Git Bash 下用 `pwd` 拼路径喂给 ffmpeg concat 列表，实测会因为 `/c/Users/...` 这种 POSIX 路径被 ffmpeg.exe 解析出 `C:/c/Users/...` 双重盘符报错（已在 shell 版里修复过一次，但 Python 版从设计上就用 Python 自带的 `os.path.abspath`，不走 shell 的 `pwd`，天然不会有这个问题）。

依赖 `opencv-python`（可选，没装的话跳过诊断表直接合并，不影响功能）。

shell 版 `merge_hourly_segments.sh` 逻辑更简单（没有逐段诊断/校验），依然保留可用，用法一样：

```bash
./merge_hourly_segments.sh data/multicam_multiimu --out-dir data/multicam_multiimu_merged
```

**默认绝对不会碰原始数据（两个版本都一样）**：只读取源目录里的文件，合并结果默认存到源目录下新建的 `merged/` 子目录（可以用 `--out-dir` 指定别的路径），不会覆盖、不会删除任何原始小段文件——即使合并逻辑有问题，最多是 `merged/` 目录里的结果不对，删掉重跑就行，原始数据始终完好。只有显式加了 `--delete-originals` 才会在合并成功后删除参与合并的原始小段。

mp4 用 `ffmpeg` 的 concat demuxer + `-c copy` 无损拼接（不重新编码，速度快），csv 按文件名时间顺序拼接、只保留一份表头。合并后的文件名去掉了具体的"分:秒"，只保留到小时：`{前缀}_YYYYMMDDHH_camX_imuY_resampled{HZ}hz.mp4/.csv`（比如 `19:43:50`、`19:44:51`、`19:45:51` 这三段会被合并成 `..._2026071619_...`，代表2026-07-16 19点这一小时）。

每个"保留关键字"默认同时保留 mp4 和 csv；只想留其中一种时加后缀 `:mp4` 或 `:csv`。关键字按文件名子串匹配（不用写完整文件名，`camX_imuY` 这段就够）。运行后会先列出打算删除的文件清单，输入 `y` 确认后才会真正删除，避免手滑删错。

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
- **WitMotion 校时**：芯片时间需用官方上位机软件校准，本工具不提供写入校时的功能（WT901 系列协议只读）。未校时的设备芯片时间字段无效（`wit_ble_live.py --print-only` 里"片上="会显示空白），`imu_camera_sync.py` 完全不依赖芯片时间（只用电脑时间做时间戳），未校时也能正常采集；但 `wit_ble_live.py` 写 CSV 时默认会丢弃芯片时间无效的帧（`--keep-bad-frames` 可保留，此时时间戳仍用电脑时间，只是不做"时间戳必须递增"的过滤）。
- **HICC 校时**：连接后会自动下发当前北京时间，无需手动操作。`--no-timesync` 可跳过（设备时钟已准确时使用）。
- **Label Studio 时间戳**：`timeFormat` 必须填 `%Y-%m-%d %H:%M:%S.%L`，用 `.` 分隔毫秒而非 `:`，否则 Label Studio 会报解析错误。
