#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立版"抓挠"行为离线推理脚本 —— 不依赖 imu_train 项目本身，只需要把训练好的
模型文件（{name}.pkl + 同名的 {name}.json 元数据）拷贝过来就能跑。

这是 https://github.com/zhuyetuo/imu_train 的 src/infer_csv_scratch.py 的独立
移植版：特征提取（时域+频域，每个通道9个时域特征+4个频域特征）和重力对齐算法
跟原版逐行一致（分别对应原项目的 src/ml/features.py::extract_features 和
src/data/gravity_align.py::gravity_align），这样预测结果才会跟 imu_train 那边
跑出来的完全一样，不是"看起来差不多"的近似实现。

依赖:
    pip install numpy pandas scipy scikit-learn joblib tqdm

用法（参数跟 imu_train 的 infer_csv_scratch.py 保持一致，可以直接照抄以前的命令）:
    # 单个 CSV
    python infer_scratch.py --csv data/xxx_resampled16hz.csv --model ml_rf.pkl

    # 目录批量
    python infer_scratch.py --csv_dir data/multicam_multiimu --pattern "*_resampled16hz.csv" \\
        --model ml_rf.pkl --device_hz 16 --confidence_threshold 0.7 --scratch_only --quiet --workers 8

    # 一次推理、同时对多个"待复核"置信度区间分别出报告（避免每个区间都重新跑
    # 一遍特征提取+预测——这是最耗时的部分，只算一次，区间越多省得越多）：
    python infer_scratch.py --csv_dir data/xxx --pattern "*_resampled16hz.csv" \\
        --model ml_rf.pkl --device_hz 16 --confidence_threshold 0.7 --scratch_only --quiet \\
        --review_bins 0.0-0.3 0.3-0.4 0.4-0.5 0.5-0.6 0.6-0.7 0.7-0.8 0.8-0.9 0.9-1.0 \\
        --out_dir .
    # 会生成 scratch_log_review_<lo>-<hi>.txt（每个区间一份，格式跟单独跑一样，
    # clip_scratch_segments.py 可以直接读）。

模型文件要求:
    --model 指向的 {name}.pkl 用 joblib.load 能直接读出一个有 predict_proba 的
    sklearn 分类器（imu_train 训练出来的模型就是这种格式）。同目录如果有同名
    的 {name}.json（把 .pkl 换成 .json），会自动读取里面的训练参数（采样率、
    窗口/步长秒数、类别列表、是否重力对齐），跟 imu_train 训练脚本导出的格式
    一致；没有这个 json 就退化为用模型自带的 model.classes_，窗口/步长/是否
    重力对齐用默认值（16Hz、2秒窗口、1秒步长、开重力对齐）。

输出格式（"── 文件名 ──"、"【汇总】"、"【片段】"、"【合并】"）保持跟 imu_train
一致，方便这个仓库里的 clip_scratch_segments.py 直接解析这份输出去剪视频，
不用再额外转换格式。
"""

import argparse
import glob
import io
import json
import os
import sys
import urllib.request
from math import gcd

import numpy as np
import pandas as pd
from scipy import stats, signal

try:
    import joblib
except ImportError:
    print('缺少 joblib，请先安装: pip install joblib scikit-learn')
    sys.exit(1)

ACC_CANDIDATES = [['acc_x', 'acc_y', 'acc_z'], ['AccX', 'AccY', 'AccZ'],
                   ['AX', 'AY', 'AZ'], ['ax', 'ay', 'az']]
GYRO_CANDIDATES = [['gyro_x', 'gyro_y', 'gyro_z'], ['gyr_x', 'gyr_y', 'gyr_z'],
                    ['GyroX', 'GyroY', 'GyroZ'], ['GX', 'GY', 'GZ']]
TS_KEYWORDS = ['time', 'timestamp', 'datetime', 'chip_time']


# ── 重力对齐（跟 imu_train 的 src/data/gravity_align.py 逐行一致） ───────────

def gravity_align(window: np.ndarray) -> np.ndarray:
    """
    window: (T, C)，前3列是加速度，接下来3列是角速度，超过6列的通道原样透传。
    用整个窗口加速度的均值估计重力方向，把窗口整体旋转到重力对准 +Z 轴
    （同一个旋转也应用到角速度上），这样收集数据时项圈朝向不同也不影响特征。
    """
    acc = window[:, :3]
    gyr = window[:, 3:6] if window.shape[1] >= 6 else None

    g_est = acc.mean(axis=0)
    g_norm = np.linalg.norm(g_est)
    if g_norm < 1e-6:
        return window  # 没有有效的重力信号，跳过

    g_unit = g_est / g_norm
    ref = np.array([0.0, 0.0, 1.0])

    dot = float(np.clip(np.dot(g_unit, ref), -1.0, 1.0))

    if dot > 0.9999:
        return window  # 已经对齐

    if dot < -0.9999:
        R = np.diag(np.array([1.0, -1.0, -1.0]))
    else:
        axis = np.cross(g_unit, ref)
        axis /= np.linalg.norm(axis)
        angle = np.arccos(dot)
        K = np.array([
            [0.0, -axis[2], axis[1]],
            [axis[2], 0.0, -axis[0]],
            [-axis[1], axis[0], 0.0],
        ])
        R = np.eye(3) + np.sin(angle) * K + (1.0 - np.cos(angle)) * (K @ K)

    out = window.copy()
    out[:, :3] = (R @ acc.T).T
    if gyr is not None:
        out[:, 3:6] = (R @ gyr.T).T
    return out


# ── 特征提取（跟 imu_train 的 src/ml/features.py 逐行一致） ─────────────────

def _time_features(window: np.ndarray) -> np.ndarray:
    feats = []
    for ch in range(window.shape[1]):
        x = window[:, ch]
        feats.extend([
            np.mean(x), np.std(x), np.min(x), np.max(x), np.max(x) - np.min(x),
            np.sqrt(np.mean(x ** 2)),
            stats.skew(x) if np.std(x) > 1e-8 else 0.0,
            stats.kurtosis(x) if np.std(x) > 1e-8 else 0.0,
            np.sum(np.diff(np.sign(x)) != 0),
        ])
    return np.array(feats, dtype=np.float32)


def _freq_features(window: np.ndarray, hz: int) -> np.ndarray:
    feats = []
    for ch in range(window.shape[1]):
        x = window[:, ch]
        freqs, psd = signal.welch(x, fs=hz, nperseg=min(len(x), 32))
        psd_norm = psd / (psd.sum() + 1e-8)
        feats.extend([
            np.sum(freqs * psd_norm),
            np.sqrt(np.sum((freqs - np.sum(freqs * psd_norm)) ** 2 * psd_norm)),
            freqs[np.argmax(psd)],
            -np.sum(psd_norm * np.log(psd_norm + 1e-8)),
        ])
    return np.array(feats, dtype=np.float32)


def extract_features(X: np.ndarray, hz: int, show_progress: bool = True) -> np.ndarray:
    if len(X) == 0:
        n_ch = X.shape[2] if X.ndim == 3 else 6
        dummy = np.zeros((1, X.shape[1] if X.ndim == 3 else 10, n_ch), dtype=np.float32)
        n_feat = len(np.concatenate([_time_features(dummy[0]), _freq_features(dummy[0], hz)]))
        return np.empty((0, n_feat), dtype=np.float32)
    features = []
    it = range(len(X))
    if show_progress and len(X) > 10:
        from tqdm import tqdm
        it = tqdm(it, desc='提取特征', unit='窗口')
    for i in it:
        features.append(np.concatenate([_time_features(X[i]), _freq_features(X[i], hz)]))
    return np.stack(features)


# ── CSV 加载 / 降采样 / 滑窗（跟 imu_train 的 infer_csv_scratch.py 一致） ────

def find_cols(cols, candidates):
    for g in candidates:
        if all(c in cols for c in g):
            return g
    return None


def find_ts_col(cols):
    low = [c.lower() for c in cols]
    for kw in TS_KEYWORDS:
        for i, cl in enumerate(low):
            if kw in cl:
                return cols[i]
    return None


def load_csv(path):
    if path.startswith('http://') or path.startswith('https://'):
        with urllib.request.urlopen(path) as resp:
            df = pd.read_csv(io.BytesIO(resp.read()))
    else:
        df = pd.read_csv(path)

    df.columns = [c.strip().lstrip('﻿') for c in df.columns]

    acc_cols = find_cols(df.columns.tolist(), ACC_CANDIDATES)
    gyro_cols = find_cols(df.columns.tolist(), GYRO_CANDIDATES)
    ts_col = find_ts_col(df.columns.tolist())

    if acc_cols is None:
        raise ValueError(f'找不到加速度列: {list(df.columns)}')

    valid_mask = df[acc_cols].notnull().all(axis=1).values
    null_ratio = 1 - valid_mask.mean()

    acc = df[acc_cols].ffill().bfill().values.astype(np.float32)
    gyro = df[gyro_cols].ffill().bfill().values.astype(np.float32) if gyro_cols \
        else np.zeros((len(df), 3), dtype=np.float32)

    ts = pd.to_datetime(df[ts_col], errors='coerce') if ts_col else None

    return acc, gyro, ts, valid_mask, null_ratio


def downsample(data, device_hz, model_hz):
    if device_hz == model_hz:
        return data
    g = gcd(device_hz, model_hz)
    up, down = model_hz // g, device_hz // g
    if up == 1:
        return data[::down]
    from scipy.signal import resample_poly
    return resample_poly(data, up, down, axis=0).astype(np.float32)


def sliding_windows(data, window_size, stride):
    windows, indices = [], []
    for start in range(0, len(data) - window_size + 1, stride):
        windows.append(data[start:start + window_size])
        indices.append(start)
    return np.stack(windows) if windows else np.empty((0, window_size, data.shape[1])), indices


def compute_predictions(path, model, classes, window_size, stride, device_hz, model_hz,
                         gravity_aligned, show_feature_progress=True):
    """
    只做一次（最耗时的）计算：加载 CSV → 降采样 → 滑窗 → 重力对齐 → 特征提取 →
    model.predict_proba。返回的都是普通可 pickle 的数据（numpy 数组/list/
    pandas Series），可以直接跨进程传回（配合 joblib 的 loky 后端），后面按不同
    的 --review_min/--review_max 区间生成报告时不用重新算这些。
    返回 None 表示这个文件没有任何有效窗口（比如全部缺失或行数不够一个窗口）。
    """
    display_name = os.path.basename(path).split('?')[0]

    acc, gyro, ts, valid_mask, null_ratio = load_csv(path)

    acc_ds = downsample(acc, device_hz, model_hz)
    gyro_ds = downsample(gyro, device_hz, model_hz)
    valid_mask_ds = downsample(valid_mask.astype(np.float32).reshape(-1, 1),
                                device_hz, model_hz).reshape(-1) > 0.5

    ratio = device_hz / model_hz

    data6 = np.concatenate([acc_ds, gyro_ds], axis=1)
    X, start_indices = sliding_windows(data6, window_size, stride)

    n_skipped = 0
    if len(X) > 0:
        valid_windows = [
            valid_mask_ds[s:s + window_size].mean() >= 0.7
            for s in start_indices
        ]
        X = X[valid_windows]
        start_indices = [s for s, v in zip(start_indices, valid_windows) if v]
        n_skipped = sum(not v for v in valid_windows)

    if len(X) == 0:
        return {
            'display_name': display_name, 'n_rows': len(acc), 'null_ratio': null_ratio,
            'n_skipped': n_skipped, 'ts': ts, 'ratio': ratio, 'window_size': window_size,
            'start_indices': [], 'preds': np.array([]), 'probs': np.empty((0, len(classes))),
            'confs': np.array([]),
        }

    if gravity_aligned:
        X_aligned = np.stack([gravity_align(X[i]) for i in range(len(X))])
    else:
        X_aligned = X

    feats = extract_features(X_aligned, model_hz, show_progress=show_feature_progress)
    probs = model.predict_proba(feats)
    preds = np.argmax(probs, axis=1)
    confs = np.max(probs, axis=1)

    return {
        'display_name': display_name, 'n_rows': len(acc), 'null_ratio': null_ratio,
        'n_skipped': n_skipped, 'ts': ts, 'ratio': ratio, 'window_size': window_size,
        'start_indices': start_indices, 'preds': preds, 'probs': probs, 'confs': confs,
    }


def generate_report(computed, classes, device_hz, model_hz, confidence_threshold=0.0,
                     quiet=False, scratch_only=False, merge_gap_s=10,
                     review_min=None, review_max=None, emit=print):
    """
    用 compute_predictions() 算好的数据，针对某一组参数（置信度阈值 + 待复核区间）
    生成人类可读的报告，通过 emit() 逐行输出（默认 print，多区间批量跑时可以传
    一个往文件里写的函数）。这里不做任何重新计算，纯格式化，所以同一份 computed
    可以反复调用不同的 (review_min, review_max) 组合，而不用重跑推理。
    返回值：(preds, n_scratch) 供上层汇总用。
    """
    display_name = computed['display_name']
    ts = computed['ts']
    ratio = computed['ratio']
    window_size = computed['window_size']
    start_indices = computed['start_indices']
    preds = computed['preds']
    probs = computed['probs']
    confs = computed['confs']

    if not scratch_only:
        emit(f'\n── {display_name} ──')
        emit(f" 行数={computed['n_rows']} device_hz={device_hz} model_hz={model_hz}")

    if computed['null_ratio'] > 0.1:
        emit(f" [警告] 数据缺失率={computed['null_ratio']*100:.1f}%（蓝牙断联？），将跳过含缺失的窗口")

    if computed['n_skipped'] and not scratch_only:
        emit(f" [过滤] 跳过 {computed['n_skipped']} 个缺失率>30% 的窗口")

    if len(preds) == 0:
        return preds, 0

    def idx_to_ts(i):
        orig = min(int(i * ratio), len(ts) - 1) if ts is not None else -1
        return ts.iloc[orig] if ts is not None and orig >= 0 else None

    scratch_idx = classes.index('抓挠') if '抓挠' in classes else None
    do_review = scratch_idx is not None and (review_min is not None or review_max is not None)
    review_lo = review_min if review_min is not None else 0.0
    review_hi = review_max if review_max is not None else 1.0
    scratch_probs = probs[:, scratch_idx] if scratch_idx is not None else None

    scratch_segs = []
    in_scratch = False
    seg_start_ts = None
    seg_start_i = None

    review_segs = []
    in_review = False
    review_start_ts = None
    review_start_i = None

    if not quiet:
        header = f" {'时间':<22} {'预测':<6} {'置信度':>6}"
        if do_review:
            header += f"  {'P(抓挠)':>8}"
        emit(header)
        emit(f" {'-'*38}")

    for i, (pred_id, conf, start_i) in enumerate(zip(preds, confs, start_indices)):
        label = classes[pred_id]

        if label == '抓挠' and conf < confidence_threshold:
            label = f'({classes[pred_id]}?)'

        t = idx_to_ts(start_i)
        is_review = do_review and (review_lo <= scratch_probs[i] <= review_hi)

        if not quiet:
            t_str = t.strftime('%Y-%m-%d %H:%M:%S') if t is not None else f'帧{start_i}'
            marker = ' ⬅ 抓挠' if label == '抓挠' else ''
            line = f' {t_str:<22} {label:<6} {conf:>6.2f}{marker}'
            if do_review:
                line += f'  {scratch_probs[i]:>8.2f}'
                if is_review:
                    line += '  ★待复核'
            emit(line)

        if label == '抓挠' and not in_scratch:
            in_scratch = True
            seg_start_ts = t
            seg_start_i = start_i
        elif label != '抓挠' and in_scratch:
            in_scratch = False
            seg_end_ts = idx_to_ts(start_i)
            scratch_segs.append((seg_start_ts, seg_end_ts, seg_start_i, start_i))

        if is_review and not in_review:
            in_review = True
            review_start_ts = t
            review_start_i = start_i
        elif not is_review and in_review:
            in_review = False
            review_end_ts = idx_to_ts(start_i)
            review_segs.append((review_start_ts, review_end_ts, review_start_i, start_i))

    if in_scratch:
        seg_end_ts = idx_to_ts(start_indices[-1] + window_size)
        scratch_segs.append((seg_start_ts, seg_end_ts, seg_start_i, start_indices[-1]))
    if in_review:
        review_end_ts = idx_to_ts(start_indices[-1] + window_size)
        review_segs.append((review_start_ts, review_end_ts, review_start_i, start_indices[-1]))

    n_scratch = int((preds == classes.index('抓挠')).sum()) if '抓挠' in classes else 0

    if scratch_only and not scratch_segs and not review_segs:
        return preds, n_scratch

    def _merge(segs):
        merged = []
        for t0, t1, i0, i1 in segs:
            if merged and t0 is not None and merged[-1][1] is not None:
                gap = (t0 - merged[-1][1]).total_seconds()
                if gap <= merge_gap_s:
                    merged[-1] = (merged[-1][0], t1, merged[-1][2], i1)
                    continue
            merged.append([t0, t1, i0, i1])
        return merged

    merged = _merge(scratch_segs)
    review_merged = _merge(review_segs)

    def fmt(t, i):
        return t.strftime('%H:%M:%S') if t else f'帧{i}'

    seg_str = ' '.join(fmt(t0, i0) + '→' + fmt(t1, i1) for t0, t1, i0, i1 in scratch_segs) \
        if scratch_segs else '未检测到抓挠'
    merged_str = ' '.join(fmt(t0, i0) + '→' + fmt(t1, i1) for t0, t1, i0, i1 in merged) \
        if merged else '未检测到抓挠'
    review_str = ' '.join(fmt(t0, i0) + '→' + fmt(t1, i1) for t0, t1, i0, i1 in review_merged) \
        if review_merged else '无'

    if scratch_only:
        emit(f'\n── {display_name} ──')
        emit(f' 【汇总】总窗口={len(preds)} 抓挠窗口={n_scratch} ({n_scratch/len(preds)*100:.1f}%)')
        emit(f' 【片段】{seg_str}')
        emit(f' 【合并】{merged_str}')
        if do_review:
            emit(f' 【待复核 P(抓挠)∈[{review_lo:.2f},{review_hi:.2f}]】{review_str}')

    return preds, n_scratch


def parse_bin(s):
    lo, hi = s.split('-')
    return float(lo), float(hi)


def main():
    ap = argparse.ArgumentParser(description='离线 CSV 抓挠识别（独立版，不依赖 imu_train 项目本身）')
    ap.add_argument('--csv', default='', help='单个 CSV 文件路径')
    ap.add_argument('--csv_dir', default='', help='CSV 目录（批量处理）')
    ap.add_argument('--pattern', default='*.csv', help='文件名通配符（默认 *.csv）')
    ap.add_argument('--model', required=True, help='模型路径（.pkl），同目录下同名 .json 会自动读取训练参数')
    ap.add_argument('--device_hz', type=int, default=0, help='CSV 采样率（0=自动等于模型采样率）')
    ap.add_argument('--model_hz', type=int, default=0, help='模型训练采样率（0=从模型元数据读取，默认16）')
    ap.add_argument('--window_s', type=float, default=0, help='窗口秒数（0=从模型元数据读取，默认2.0）')
    ap.add_argument('--stride_s', type=float, default=0, help='步长秒数（0=从模型元数据读取，默认1.0）')
    ap.add_argument('--confidence_threshold', type=float, default=0.0,
                     help='置信度阈值，低于此值的抓挠预测视为非抓挠（默认0=不过滤，建议0.65~0.75）')
    ap.add_argument('--review_min', type=float, default=None,
                     help='标出"抓挠"预测概率落在 [review_min, review_max] 区间内的窗口，方便人工'
                          '复核挑错——argmax判成抓挠但概率贴着下限的，可能是误识别；argmax判成'
                          '别的类但抓挠概率贴着上限的，可能是漏识别，都适合补进训练数据做主动学习。'
                          '跟 --confidence_threshold 是两回事，不冲突，不影响最终抓挠片段的判定。'
                          '比如 --review_min 0.7 --review_max 0.8')
    ap.add_argument('--review_max', type=float, default=None,
                     help='见 --review_min 说明；只设一个的话另一个默认取 0.0/1.0（即单边界限）')
    ap.add_argument('--review_bins', nargs='+', default=None, metavar='LO-HI',
                     help='一次推理、同时对多个待复核区间分别出报告，每个区间写一个'
                          'scratch_log_review_<lo>-<hi>.txt（配合 --out_dir）。跟 '
                          '--review_min/--review_max 二选一，比如: '
                          '--review_bins 0.0-0.3 0.3-0.4 0.5-0.6 0.9-1.0')
    ap.add_argument('--out_dir', default='.', help='--review_bins 时报告文件的输出目录（默认当前目录）')
    ap.add_argument('--quiet', action='store_true', help='只输出每个文件的汇总行，不打印逐窗口详情')
    ap.add_argument('--scratch_only', action='store_true', help='只输出检测到抓挠的文件，忽略无抓挠的文件')
    ap.add_argument('--merge_gap', type=float, default=10.0, help='合并相邻抓挠片段的最大间隔秒数（默认10s）')
    ap.add_argument('--workers', type=int, default=-1, help='并行进程数（默认-1=用全部CPU核，1=单进程）')
    ap.add_argument('--no_gravity_align', action='store_true')
    args = ap.parse_args()

    model = joblib.load(args.model)
    meta_path = args.model.rsplit('.', 1)[0] + '.json'
    classes, gravity_aligned, t_hz, t_window_s, t_stride_s = [], True, 16, 2.0, 1.0

    if os.path.exists(meta_path):
        with open(meta_path, encoding='utf-8') as f:
            meta = json.load(f)
            classes = meta.get('classes', [])
            gravity_aligned = meta.get('gravity_aligned', True)
            t_hz = int(meta.get('hz', 16))
            t_window_s = float(meta.get('window_s', 2.0))
            t_stride_s = float(meta.get('stride_s', 1.0))
        print(f'[模型] 训练参数: 采样率={t_hz}Hz  窗口={t_window_s}s  步长={t_stride_s}s  重力对齐={gravity_aligned}')
        print(f'[模型] 类别: {classes}')
    else:
        classes = list(model.classes_) if hasattr(model, 'classes_') else []
        print(f'[模型] 未找到元数据 JSON（{meta_path}），类别: {classes}（窗口/步长/采样率用默认值，'
              f'如果这跟训练时不一致，预测会不准，建议把 {os.path.basename(meta_path)} 也拷贝过来）')

    if args.no_gravity_align:
        gravity_aligned = False

    model_hz = args.model_hz or t_hz
    device_hz = args.device_hz or model_hz
    window_s = args.window_s or t_window_s
    stride_s = args.stride_s or t_stride_s
    window_size = int(window_s * model_hz)
    stride = int(stride_s * model_hz)

    print(f'[推理] 设备Hz={device_hz}  模型Hz={model_hz}  窗口={window_s}s  步长={stride_s}s')

    if '抓挠' not in classes:
        print(f"[警告] 模型类别中没有'抓挠': {classes}")

    files = []
    if args.csv:
        files = [args.csv]
    elif args.csv_dir:
        files = sorted(glob.glob(os.path.join(args.csv_dir, args.pattern)))

    if not files:
        print('[错误] 请指定 --csv 或 --csv_dir')
        return

    print(f'\n共 {len(files)} 个文件')

    from tqdm import tqdm
    from joblib import Parallel, delayed

    def _compute_one(path):
        try:
            return compute_predictions(path, model, classes, window_size, stride,
                                        device_hz, model_hz, gravity_aligned,
                                        show_feature_progress=not args.quiet and not args.scratch_only
                                        and args.review_bins is None)
        except Exception as e:
            tqdm.write(f' [错误] {os.path.basename(path)}: {e}')
            return None

    n_jobs = args.workers if args.workers > 0 else -1
    computed_list = Parallel(n_jobs=n_jobs, backend='loky')(
        delayed(_compute_one)(p) for p in tqdm(files, desc='推理进度', unit='文件')
    )

    def _run_bin(review_min, review_max, emit):
        all_scratch = 0
        all_total = 0
        for computed in computed_list:
            if computed is None:
                continue
            preds, n_scratch = generate_report(
                computed, classes, device_hz, model_hz,
                confidence_threshold=args.confidence_threshold,
                quiet=args.quiet, scratch_only=args.scratch_only,
                merge_gap_s=args.merge_gap,
                review_min=review_min, review_max=review_max, emit=emit)
            all_scratch += n_scratch
            all_total += len(preds)
        if len(files) > 1:
            emit(f"\n{'='*50}")
            if all_total:
                emit(f'批量汇总: 总窗口={all_total} 抓挠窗口={all_scratch} ({all_scratch/all_total*100:.1f}%)')
            else:
                emit('无有效数据')

    if args.review_bins:
        os.makedirs(args.out_dir, exist_ok=True)
        for bin_str in args.review_bins:
            lo, hi = parse_bin(bin_str)
            out_path = os.path.join(args.out_dir, f'scratch_log_review_{lo}-{hi}.txt')
            print(f'════ 生成阈值段 {lo}~{hi} → {out_path} ════')
            with open(out_path, 'w', encoding='utf-8') as f:
                def emit(line, _f=f):
                    _f.write(str(line) + '\n')
                _run_bin(lo, hi, emit)
    else:
        _run_bin(args.review_min, args.review_max, print)


if __name__ == '__main__':
    main()
