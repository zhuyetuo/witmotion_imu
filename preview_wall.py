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


#: 猜不到屏幕分辨率时按这个算。现场两台都是 1080p。
DEFAULT_SCREEN = (1920, 1080)

#: 留给标题栏、任务栏和窗口边框的余量。铺满 100% 的话窗口会被挤得要拖动，
#: 而这个窗口是"扫一眼"用的，不该还要人去摆弄它。
SCREEN_MARGIN_W = 0.96
SCREEN_MARGIN_H = 0.88


def screen_size() -> tuple[int, int]:
    """屏幕多大。取不到就按 1080p 算——**宁可猜一个常见值，也不要退回一个
    小到没法看的固定值**（480 一格的 2x2 才 960x540，在 1080p 上只占四分之一，
    第一版就是这么小的）。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32          # 只有 Windows 有
        user32.SetProcessDPIAware()
        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        if w > 0 and h > 0:
            return (w, h)
    except Exception:  # noqa: BLE001 非 Windows、或者拿不到，都退回默认
        pass
    return DEFAULT_SCREEN


def fit_tile_width(frame_w: int, frame_h: int, n: int,
                   max_w: int, max_h: int) -> int:
    """n 路画面排成网格、整块塞进 max_w x max_h，每格该多宽。

    宽和高两个方向都要塞得下，取小的那个：只按宽度算的话，2x2 在
    1920x1080 上会算出每格 960 宽、整块 1920x1080，加上标题栏就超出屏幕，
    窗口被系统缩到一半，反而更小。
    """
    if n <= 0 or frame_w <= 0 or frame_h <= 0:
        return 16
    cols, rows = grid_shape(n)
    by_w = max_w / cols
    by_h = (max_h / rows) * (frame_w / frame_h)
    return max(16, int(min(by_w, by_h)))


def tile_size(frame_w: int, frame_h: int, tile_w: int) -> tuple[int, int]:
    """按原画面的宽高比缩到 tile_w 宽。**不拉伸**——拉伸过的画面判断狗的
    姿态会出错（一只趴着的狗拉扁了像躺着）。"""
    tile_w = max(int(tile_w), 16)
    if frame_w <= 0 or frame_h <= 0:
        return (tile_w, tile_w * 9 // 16)
    return (tile_w, max(1, round(tile_h_for(frame_w, frame_h, tile_w))))


def tile_h_for(frame_w: int, frame_h: int, tile_w: int) -> float:
    return frame_h * (tile_w / frame_w)


#: 标签的字号。**固定值，不跟着格子大小放大。**
#:
#: 第一版按 `bar_h = h // 9` 算字号，格子一大（自动铺满之后每格 844 宽）
#: 就变成 3 倍多的巨字，压掉画面上面九分之一，一行字还写不下被切断——
#: 现场截图里 cam1 那行就被 cam2 的格子切掉了半截。
#:
#: 标签是"扫一眼确认这路还活着"，不是主角，小小一块贴在角上就行。
LABEL_SCALE = 0.45
LABEL_PAD = 4


def label_tile(tile: np.ndarray, text: str, down: bool = False) -> np.ndarray:
    """在小图左上角贴一小块字。**就地画**，因为 tile 已经是 resize 出来的新
    数组，不是写进视频的那一帧。

    只盖住文字那么大一块，不横贯整行——整行的黑条会把画面顶上一截整个吃掉。

    **一律用 ASCII。** OpenCV 的 putText 只认 ASCII，中文画出来是一串问号；
    狗名本来就是拼音（xiaobai / keji），正好不用操心。
    """
    text = _ascii(text).strip()
    if not text:
        # 空标签 = 这一路的画面里本来就带着叠加信息了，别再画一遍
        return tile
    font, thick = cv2.FONT_HERSHEY_SIMPLEX, 1
    (tw, th), base = cv2.getTextSize(text, font, LABEL_SCALE, thick)
    x0, y0 = LABEL_PAD, LABEL_PAD
    x1, y1 = x0 + tw + LABEL_PAD * 2, y0 + th + base + LABEL_PAD
    x1, y1 = min(x1, tile.shape[1]), min(y1, tile.shape[0])
    # 半透明底：纯黑底把画面挡死，全透明又看不清字
    patch = tile[y0:y1, x0:x1]
    if patch.size:
        cv2.addWeighted(patch, 0.35, np.zeros_like(patch), 0.65, 0, patch)
    cv2.putText(tile, text, (x0 + LABEL_PAD, y1 - LABEL_PAD - base // 2),
                font, LABEL_SCALE,
                (80, 80, 255) if down else (180, 255, 180), thick, cv2.LINE_AA)
    return tile


def _ascii(text: str) -> str:
    """非 ASCII 一律换成 '?' 之前先尽量去掉——画出来一串问号还不如不画。

    OpenCV 的 Hershey 字体没有中文，putText 遇到中文就是一堆方框/问号。
    窗口标题同理（现场截图里标题栏就是乱码）。
    """
    return text.encode('ascii', 'replace').decode('ascii')


def compose(frames: list, labels: list[str], tile_w: int | None = None,
            down: list[bool] | None = None, zoom: int | None = None,
            screen: tuple[int, int] | None = None) -> np.ndarray:
    """几路画面 → 一张大图。

    tile_w 不给（默认）就按屏幕大小自动铺满；给了就按给的来（--wall-width）。

    zoom 给了就只画那一路，**铺满跟网格一样大的画布**——换成"网格的两倍"
    之类的话，点一下窗口变大、再点又变小，看着像在跳。zoom 越界当没给：
    鼠标点在最后一行的空格子上就是这种情况，不该崩掉整个录制。
    """
    down = down or [False] * len(frames)
    live = [(f, labels[i], down[i]) for i, f in enumerate(frames) if f is not None]
    if not live:
        return np.zeros((120, 320, 3), dtype=np.uint8)

    sw, sh = screen or screen_size()
    max_w, max_h = int(sw * SCREEN_MARGIN_W), int(sh * SCREEN_MARGIN_H)
    h0, w0 = live[0][0].shape[:2]
    cols, rows = grid_shape(len(live))
    tw = int(tile_w) if tile_w else fit_tile_width(w0, h0, len(live), max_w, max_h)
    grid_w, grid_h = tile_size(w0, h0, tw)

    if zoom is not None and 0 <= zoom < len(frames) and frames[zoom] is not None:
        f = frames[zoom]
        h, w = f.shape[:2]
        # 画布跟网格一模一样大，放大的那一路居中、四周留黑。
        #
        # 为什么要留黑而不是"把画面撑满画布"：网格的长宽比取决于有几路
        # （2 路是 2x1，很扁），单张 16:9 铺不满那种形状，硬撑就得拉伸。
        # 画布大小不变则窗口不会一点就变大、再点又缩回去。
        cw, ch = grid_w * cols, grid_h * rows
        big_w = fit_tile_width(w, h, 1, cw, ch)
        tw2, th2 = tile_size(w, h, big_w)
        tile = label_tile(cv2.resize(f, (tw2, th2), interpolation=cv2.INTER_AREA),
                          f'{labels[zoom]}  [click to zoom out]', down[zoom])
        canvas = np.zeros((ch, cw, 3), dtype=np.uint8)
        y0, x0 = (ch - th2) // 2, (cw - tw2) // 2
        canvas[y0:y0 + th2, x0:x0 + tw2] = tile
        return canvas

    tw, th = grid_w, grid_h
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
