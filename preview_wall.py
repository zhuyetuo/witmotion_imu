"""监控墙：几路画面拼成一张大图，一个窗口看齐，点一下放大、再点还原。

为什么要它：原来的预览是**一路一个窗口**。四七路开起来窗口互相叠在一起，
屏幕不够大就只能一个个点开看，而且每路一次 `cv2.imshow` 全尺寸 720p——
现场实测六路每 tick 吃掉 30 多毫秒，掉四成帧率。

这边三件事一起省：

  1. **一个窗口**。N 次 imshow 变一次。
  2. **先缩小再拼**。每格默认 480 宽，四格加起来约 52 万像素，
     而四路 720p 全尺寸是 368 万——像素搬运量差七倍。
  3. **按自己的节奏刷**。录制 25fps，墙默认 8fps，又省下三分之二。

叠加信息（时间、Hz、狗名）也画在**缩小之后的小图**上，不再为了预览去拷一张
整帧再画。原来 `--no-save-overlay` 的场地每路每 tick 要多拷 2.76MB，就是为了
留一张干净的原图写视频；墙这条路直接读那张原图，一次拷贝都不用。

**这个模块碰不到写进视频的那一帧。** 只读，`cv2.resize` 出来的是新数组。
录像的分辨率、帧率、内容跟墙开不开没有任何关系——墙只是另外看一眼。

下面这些函数不碰窗口、不碰摄像头，纯算数组和坐标，所以能直接单元测试
（现场那台机器上没法跑 pytest 去点鼠标）。
"""

from __future__ import annotations

import math

import cv2
import numpy as np


def grid_shape(n: int) -> tuple[int, int]:
    """n 路画面排几列几行 → (列, 行)。

    尽量接近正方形，列不少于行——屏幕是横的，宽度比高度富裕。
    3 路排 2x2 而不是 3x1：3x1 每格只能很扁，狗在画面里就剩一条。
    """
    if n <= 0:
        return (0, 0)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return (cols, rows)


def tile_size(frame_w: int, frame_h: int, tile_w: int) -> tuple[int, int]:
    """按原画面的宽高比缩到 tile_w 宽。**不拉伸**——拉伸过的画面判断狗的
    姿态会出错（一只趴着的狗拉扁了像躺着）。"""
    tile_w = max(int(tile_w), 16)
    if frame_w <= 0 or frame_h <= 0:
        return (tile_w, tile_w * 9 // 16)
    return (tile_w, max(1, round(tile_h_for(frame_w, frame_h, tile_w))))


def tile_h_for(frame_w: int, frame_h: int, tile_w: int) -> float:
    return frame_h * (tile_w / frame_w)


def label_tile(tile: np.ndarray, text: str, down: bool = False) -> np.ndarray:
    """在小图左上角写一行字。**就地画**，因为 tile 已经是 resize 出来的新数组，
    不是写进视频的那一帧。"""
    h, w = tile.shape[:2]
    bar_h = max(16, h // 9)
    cv2.rectangle(tile, (0, 0), (w, bar_h), (0, 0, 0), -1)
    scale = bar_h / 32.0
    cv2.putText(tile, text, (6, int(bar_h * 0.72)), cv2.FONT_HERSHEY_SIMPLEX,
                max(0.35, scale), (80, 80, 255) if down else (200, 255, 200),
                1, cv2.LINE_AA)
    return tile


def compose(frames: list, labels: list[str], tile_w: int = 480,
            down: list[bool] | None = None, zoom: int | None = None) -> np.ndarray:
    """几路画面 → 一张大图。

    zoom 给了就只画那一路（铺满整张），否则按网格拼。zoom 越界当没给——
    鼠标点在最后一行的空格子上就是这种情况，不该崩掉整个录制。
    """
    down = down or [False] * len(frames)
    live = [(f, labels[i], down[i]) for i, f in enumerate(frames) if f is not None]
    if not live:
        return np.zeros((120, 320, 3), dtype=np.uint8)

    if zoom is not None and 0 <= zoom < len(frames) and frames[zoom] is not None:
        f = frames[zoom]
        h, w = f.shape[:2]
        big_w = tile_w * 2
        tile = cv2.resize(f, tile_size(w, h, big_w), interpolation=cv2.INTER_AREA)
        return label_tile(tile, f'{labels[zoom]}  [点一下还原]', down[zoom])

    cols, rows = grid_shape(len(live))
    h0, w0 = live[0][0].shape[:2]
    tw, th = tile_size(w0, h0, tile_w)
    canvas = np.zeros((th * rows, tw * cols, 3), dtype=np.uint8)
    for i, (f, lab, is_down) in enumerate(live):
        tile = cv2.resize(f, (tw, th), interpolation=cv2.INTER_AREA)
        label_tile(tile, lab, is_down)
        r, c = divmod(i, cols)
        canvas[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = tile
    return canvas


def hit_test(x: int, y: int, n_live: int, tile_w: int, tile_h: int) -> int | None:
    """鼠标点在墙上的哪一格 → 第几路（从 0 数），点在空格子上返回 None。

    最后一行常常是空的（3 路排 2x2 就空一格），点空格子必须是"什么都不做"，
    不能算成最后一路——那样点空白处画面会莫名其妙放大。
    """
    if n_live <= 0 or tile_w <= 0 or tile_h <= 0:
        return None
    cols, rows = grid_shape(n_live)
    c, r = x // tile_w, y // tile_h
    if c < 0 or r < 0 or c >= cols or r >= rows:
        return None
    idx = r * cols + c
    return idx if idx < n_live else None
