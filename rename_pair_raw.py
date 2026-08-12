#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把旧格式（cam视频、imu原始csv各自独立命名，没有配对）的录制目录，改成
新格式（camX_imuX_raw.mp4/.csv 配对命名），方便直接拖拽上传标注。

规则:
    每组 base（比如 multicam_20260811_230000253）下：
    - cam 数量决定能配出几对 mp4：cam1<->imu1、cam2<->imu2……直接一一对应，
      各自的视频重命名为 {base}_camX_imuX_raw.mp4，对应的
      {base}_imuX_raw.csv 重命名为 {base}_camX_imuX_raw.csv。
    - imu 编号超过 cam 数量的（比如2个cam时的imu3、imu4...），只重命名csv，
      不产生mp4，cam前缀按轮流分配（imu3轮到cam1，imu4轮到cam2，以此类推：
      cam_idx = ((imu_idx - 1) % num_cams) + 1）。
    - 重命名/配对之后，这组base下没被保留的文件（组合csv、meta.csv、多余没
      配上的cam视频等）全部删除。

用法:
    python rename_pair_raw.py data/multicam_multiimu/2026_8_11/1
    python rename_pair_raw.py data/multicam_multiimu/2026_8_11/1 --dry-run   # 只打印计划，不真的改
"""

import argparse
import os
import re
import sys

# 旧格式cam视频：{base}_camN.mp4 或 {base}_camN_raw.mp4（两种都可能遇到）
CAM_RE = re.compile(r'^(?P<base>.+)_cam(?P<idx>\d+)(?:_raw)?\.mp4$')
# imu原始csv：{base}_imuN_raw.csv
IMU_RE = re.compile(r'^(?P<base>.+)_imu(?P<idx>\d+)_raw\.csv$')
# 已经是配对格式的（避免重复处理/误删已经改好的文件）
ALREADY_PAIRED_RE = re.compile(r'_cam\d+_imu\d+_raw\.(mp4|csv)$')


def scan(directory):
    """返回 { base: {'cams': {idx: fname}, 'imus': {idx: fname}, 'others': [fname,...]} }
    只处理"旧格式"（还没有任何 camX_imuY_raw 配对文件）的 base 分组——如果一个
    base 底下已经能找到配对格式的文件（比如新版录制脚本 --no-resample 生成的、
    配对+原始都保留的目录），说明这组已经处理过/本来就是新格式，直接跳过整组，
    不去动它，避免把故意保留的原始文件当成"其它文件"误删。"""
    groups = {}
    already_paired_bases = set()

    def get_group(base):
        return groups.setdefault(base, {'cams': {}, 'imus': {}, 'others': []})

    all_files = sorted(os.listdir(directory))
    matched = set()

    for fname in all_files:
        pm = ALREADY_PAIRED_RE.search(fname)
        if pm:
            base_guess = fname[:pm.start()]
            already_paired_bases.add(base_guess)
            continue
        m = CAM_RE.match(fname)
        if m:
            get_group(m['base'])['cams'][int(m['idx'])] = fname
            matched.add(fname)
            continue
        m = IMU_RE.match(fname)
        if m:
            get_group(m['base'])['imus'][int(m['idx'])] = fname
            matched.add(fname)

    # 已经有配对文件的base整组跳过（哪怕这个base下同时还有旧格式的残留文件，
    # 也不处理——避免猜错、误删故意保留的原始文件，这种混合情况人工看一眼更保险）。
    for base in list(groups.keys()):
        if any(base.startswith(pb) or pb.startswith(base) for pb in already_paired_bases):
            del groups[base]

    # 第二遍：把同一个base前缀下、其它没被识别成cam/imu的文件也归进"others"
    # （比如 {base}.csv 组合文件、{base}_meta.csv），方便后面统一删除。
    bases = sorted(groups.keys(), key=len, reverse=True)  # 长的优先匹配，避免前缀互相包含误判
    for fname in all_files:
        if fname in matched or ALREADY_PAIRED_RE.search(fname):
            continue
        for base in bases:
            if fname == base or fname.startswith(base + '.') or fname.startswith(base + '_'):
                groups[base]['others'].append(fname)
                break

    return groups


def build_plan(directory, groups):
    """返回 (renames, deletes)：
    renames = [(旧路径, 新路径), ...]
    deletes = [路径, ...]（renames里的"旧路径"不需要再出现在deletes里，rename本身就是"消费掉"了）
    """
    renames = []
    deletes = []

    for base, g in sorted(groups.items()):
        cams = g['cams']
        imus = g['imus']
        num_cams = len(cams)

        used_cam_files = set()
        for imu_idx in sorted(imus):
            imu_fname = imus[imu_idx]
            if num_cams > 0 and imu_idx <= num_cams:
                cam_idx = imu_idx
            elif num_cams > 0:
                cam_idx = ((imu_idx - 1) % num_cams) + 1
            else:
                cam_idx = None

            new_csv = f'{base}_cam{cam_idx}_imu{imu_idx}_raw.csv' if cam_idx else None
            if new_csv:
                renames.append((os.path.join(directory, imu_fname), os.path.join(directory, new_csv)))
            else:
                deletes.append(os.path.join(directory, imu_fname))

            # 只有"直接一一对应"这一档（imu_idx <= num_cams）才配mp4
            if num_cams > 0 and imu_idx <= num_cams and imu_idx in cams:
                cam_fname = cams[imu_idx]
                new_mp4 = f'{base}_cam{imu_idx}_imu{imu_idx}_raw.mp4'
                renames.append((os.path.join(directory, cam_fname), os.path.join(directory, new_mp4)))
                used_cam_files.add(imu_idx)

        # 没被用上的cam视频（比如cam数量比imu数量还多的情况）直接删
        for cam_idx, cam_fname in cams.items():
            if cam_idx not in used_cam_files:
                deletes.append(os.path.join(directory, cam_fname))

        # 组合csv/meta.csv这些"others"全部删除
        for other in g['others']:
            deletes.append(os.path.join(directory, other))

    return renames, deletes


def main():
    ap = argparse.ArgumentParser(description='把旧格式录制目录改成 camX_imuX_raw 配对命名')
    ap.add_argument('directory', help='要处理的目录，比如 data/multicam_multiimu/2026_8_11/1')
    ap.add_argument('--dry-run', action='store_true', help='只打印计划，不真的重命名/删除')
    args = ap.parse_args()

    if not os.path.isdir(args.directory):
        print(f'目录不存在: {args.directory}')
        sys.exit(1)

    groups = scan(args.directory)
    if not groups:
        print('没有找到任何匹配的旧格式文件（{base}_camN.mp4 / {base}_imuN_raw.csv），什么都不用做。')
        return

    renames, deletes = build_plan(args.directory, groups)

    if not renames and not deletes:
        print('没有需要改动的文件。')
        return

    print(f'共 {len(groups)} 组录制，计划:')
    print(f'\n── 重命名 {len(renames)} 个文件 ──')
    for src, dst in renames:
        print(f'  {os.path.basename(src)}  ->  {os.path.basename(dst)}')
    print(f'\n── 删除 {len(deletes)} 个文件 ──')
    for p in deletes:
        print(f'  {os.path.basename(p)}')

    if args.dry_run:
        print('\n（--dry-run，未真正改动任何文件）')
        return

    print()
    confirm = input(f'确认执行以上 {len(renames)} 个重命名 + {len(deletes)} 个删除？(y/N) ').strip().lower()
    if confirm != 'y':
        print('已取消，未改动任何文件。')
        return

    ok_rename = ok_delete = 0
    for src, dst in renames:
        try:
            os.rename(src, dst)
            ok_rename += 1
        except OSError as e:
            print(f'重命名失败 {os.path.basename(src)}: {e}')
    for p in deletes:
        try:
            os.remove(p)
            ok_delete += 1
        except OSError as e:
            print(f'删除失败 {os.path.basename(p)}: {e}')

    print(f'完成: 重命名 {ok_rename}/{len(renames)}，删除 {ok_delete}/{len(deletes)}')


if __name__ == '__main__':
    main()
