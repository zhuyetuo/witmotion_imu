"""监控墙：拼图、点击放大、以及**绝不碰录像那一帧**。

需求是「一个屏幕看齐所有画面，点一格放大、再点还原」，前提是不影响录制的
帧率和分辨率。所以这些用例分两类：

  1. 拼出来的图对不对（尺寸、不拉伸、点击命中哪一格）；
  2. **传进去的帧一个字节都没被改** —— 写进视频的就是这些帧，墙要是就地
     画了标签，录下来的视频上就会糊着一层预览用的字，而且没人会想到是它。

不需要显示器，也不需要摄像头。
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import preview_wall as w  # noqa: E402


def frame(val: int, h: int = 720, wd: int = 1280):
    return np.full((h, wd, 3), val, dtype=np.uint8)


# ── 排几行几列 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n,expect", [
    (1, (1, 1)), (2, (2, 1)), (3, (2, 2)), (4, (2, 2)),
    (5, (3, 2)), (6, (3, 2)), (7, (3, 3)),
])
def test_grid_shape(n, expect):
    assert w.grid_shape(n) == expect


def test_three_cameras_go_two_by_two_not_a_single_row():
    """3 路排成一行的话每格又扁又长，狗在画面里只剩一条。"""
    assert w.grid_shape(3) == (2, 2)


def test_columns_are_never_fewer_than_rows():
    """屏幕是横的，宽度比高度富裕。"""
    for n in range(1, 17):
        cols, rows = w.grid_shape(n)
        assert cols >= rows, n


# ── 拼图 ──────────────────────────────────────────────────────────────────


def test_four_720p_tiles_make_one_canvas():
    canvas = w.compose([frame(i) for i in (10, 20, 30, 40)],
                       ["cam4", "cam5", "cam6", "cam7"], tile_w=480)
    assert canvas.shape == (270 * 2, 480 * 2, 3)


def test_aspect_ratio_is_kept():
    """拉伸过的画面判断姿态会出错——一只趴着的狗拉扁了像躺着。"""
    canvas = w.compose([frame(10, h=1080, wd=1920)], ["cam1"], tile_w=480)
    h, wd = canvas.shape[:2]
    assert abs((wd / h) - (1920 / 1080)) < 0.02


def test_the_wall_never_touches_the_frames_that_get_recorded():
    """**这条是整件事的前提。**

    写进视频的就是传进来的这些帧。墙要是就地画了标签，录下来的视频上就糊着
    一层预览用的字——而且没人会往「预览」上想。
    """
    frames = [frame(i) for i in (10, 20, 30, 40)]
    before = [f.copy() for f in frames]
    w.compose(frames, ["a", "b", "c", "d"])
    w.compose(frames, ["a", "b", "c", "d"], zoom=2)
    for f, b in zip(frames, before):
        assert np.array_equal(f, b), "墙改了录像用的那一帧"


def test_a_dead_camera_does_not_take_the_whole_wall_down():
    """某一路读失败时传进来的可能是 None，剩下几路照样要看得见。"""
    canvas = w.compose([frame(10), None, frame(30)], ["cam1", "cam2", "cam3"])
    assert canvas.size > 0


def test_all_cameras_dead_returns_something_showable():
    """全黑也得是一张能 imshow 的图，不能返回 None 让上面炸掉。"""
    canvas = w.compose([None, None], ["cam1", "cam2"])
    assert canvas.ndim == 3 and canvas.shape[2] == 3


# ── 点一下放大 ────────────────────────────────────────────────────────────


def test_zoom_shows_only_that_one():
    frames = [frame(10), frame(20), frame(30), frame(40)]
    z = w.compose(frames, ["a", "b", "c", "d"], tile_w=480, zoom=1)
    # 放大那路是单张，宽度是格子的两倍，不是 2x2 网格
    assert z.shape[1] == 960 and z.shape[0] == 540


def test_zoom_out_of_range_falls_back_to_the_grid():
    """鼠标点在最后一行的空格子上就会传进来一个越界的号。

    崩掉的话整个录制跟着停——预览出错绝不该连累录制。
    """
    frames = [frame(10), frame(20), frame(30)]
    g = w.compose(frames, ["a", "b", "c"], tile_w=480)
    for bad in (-1, 3, 99):
        assert w.compose(frames, ["a", "b", "c"], tile_w=480, zoom=bad).shape == g.shape


def test_hit_test_maps_clicks_to_tiles():
    # 2x2，每格 480x270
    assert w.hit_test(10, 10, 4, 480, 270) == 0
    assert w.hit_test(500, 10, 4, 480, 270) == 1
    assert w.hit_test(10, 300, 4, 480, 270) == 2
    assert w.hit_test(500, 300, 4, 480, 270) == 3


def test_clicking_an_empty_cell_does_nothing():
    """3 路排 2x2 会空一格。点空格子算成"最后一路"的话，点空白处画面会
    莫名其妙放大。"""
    assert w.hit_test(500, 300, 3, 480, 270) is None


def test_hit_test_outside_the_canvas_is_none():
    assert w.hit_test(9999, 10, 4, 480, 270) is None
    assert w.hit_test(-5, 10, 4, 480, 270) is None


def test_hit_test_survives_a_zero_sized_tile():
    """窗口还没建起来时拿到的尺寸可能是 0，别在这儿除零。"""
    assert w.hit_test(10, 10, 4, 0, 0) is None


# ── 接进录制脚本的那部分 ──────────────────────────────────────────────────


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _recorder_src() -> str:
    """去掉注释的源码。**不去注释的话，这些检查会被我自己写的说明骗过去** ——
    注释里提到 `cv2.imshow` 是常事。"""
    src = open(os.path.join(REPO, "imu_camera_sync_multicam.py"), encoding="utf-8").read()
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


def test_wall_mode_replaces_the_per_camera_windows():
    """墙要是"额外多开一个窗口"，那就是更慢而不是更快 —— 一路一个 imshow
    照旧，再加一次整墙。必须是替换。"""
    code = _recorder_src()
    assert "if preview.on and not wall_on:" in code, \
        "墙开着的时候还在一路一个 imshow"


def test_wall_mode_skips_the_full_frame_copy_and_overlay():
    """`--no-save-overlay` 的场地（狗场就是）为了预览要 `frame.copy()` 再画
    整帧叠加，每路每 tick 2.76MB。墙自己在缩小后的小图上写标签，这份拷贝
    应该省掉。"""
    code = _recorder_src()
    assert "if not save_overlay and (not preview.on or wall_on):" in code, \
        "墙模式还在为了预览拷整帧"


def test_the_wall_is_throttled():
    """录制 25fps，墙没必要也跟着 25fps —— 省下来的都是录制的余量。"""
    code = _recorder_src()
    assert "wall_period" in code and ">= wall_period" in code


def test_the_mouse_callback_stays_trivial():
    """回调跑在 cv2 的事件循环里。在里面做重活或者抛异常都会连累录制那一 tick。"""
    src = open(os.path.join(REPO, "imu_camera_sync_multicam.py"), encoding="utf-8").read()
    body = src.split("def _on_wall_click(", 1)[1].split("\n    frame_interval", 1)[0]
    for banned in ("cv2.imshow", "cv2.resize", "compose(", "time.sleep", "write"):
        assert banned not in body, f"鼠标回调里不该出现 {banned}"


def test_the_recorder_accepts_the_wall_flags():
    """参数真的挂上去了，而且默认是关的 —— 无人值守跑一整晚不该开着窗口。"""
    import subprocess

    # bleak 在没有蓝牙的机器上装不了，而 --help 根本用不着它：塞个假模块进去，
    # 好让这条用例在 CI / 开发机上也能跑
    boot = (
        "import sys, types, runpy\n"
        "fake = types.ModuleType('bleak')\n"
        "fake.BleakClient = fake.BleakScanner = object\n"
        "sys.modules['bleak'] = fake\n"
        "sys.argv = ['x', '--help']\n"
        "runpy.run_path('imu_camera_sync_multicam.py', run_name='__main__')\n"
    )
    out = subprocess.run([sys.executable, "-c", boot], cwd=REPO,
                         capture_output=True, text=True, timeout=120)
    assert "--wall" in out.stdout, out.stdout[-500:] + out.stderr[-2000:]
    for flag in ("--wall", "--wall-width", "--wall-fps"):
        assert flag in out.stdout, f"{flag} 没挂上"
    assert "默认 8" in out.stdout, "--wall-fps 的默认值没写进帮助"


def test_wall_env_turns_preview_on_by_itself():
    """`WALL=1` 就是"我要看画面"。还要人额外写 PREVIEW=1 才显示的话，
    第一次用的人只会看到什么都没有。"""
    sh = open(os.path.join(REPO, "record_multicam.sh"), encoding="utf-8").read()
    code = "\n".join(l for l in sh.splitlines() if not l.lstrip().startswith("#"))
    assert '_wall_on' in code
    assert 'if [ "$_wall_on" = "1" ]; then' in code, "WALL=1 没有顺带打开预览"


# ── 默认要铺满屏幕 ────────────────────────────────────────────────────────


def test_the_wall_fills_the_screen_by_default():
    """**第一版默认每格 480，2x2 才 960x540 —— 在 1080p 上只占四分之一，
    小到看不清狗在干嘛。** 默认应该按屏幕算。"""
    frames = [frame(i) for i in (10, 20, 30, 40)]
    canvas = w.compose(frames, ["a", "b", "c", "d"], screen=(1920, 1080))
    h, wd = canvas.shape[:2]
    assert wd > 1920 * 0.8, f"只用了屏幕宽度的 {wd / 1920:.0%}"
    assert h > 1080 * 0.8, f"只用了屏幕高度的 {h / 1080:.0%}"


def test_it_never_overflows_the_screen():
    """超出屏幕的话窗口会被系统缩到一半，反而更小。留了标题栏/任务栏的余量。"""
    for n in range(1, 8):
        canvas = w.compose([frame(10)] * n, [f"c{i}" for i in range(n)],
                           screen=(1920, 1080))
        h, wd = canvas.shape[:2]
        assert wd <= 1920 and h <= 1080, (n, wd, h)


def test_a_bigger_screen_gets_bigger_tiles():
    small = w.compose([frame(10)] * 4, list("abcd"), screen=(1280, 720))
    big = w.compose([frame(10)] * 4, list("abcd"), screen=(2560, 1440))
    assert big.shape[1] > small.shape[1] * 1.5


def test_fit_uses_whichever_side_runs_out_first():
    """只按宽度算的话，2x2 在 1920x1080 会算出整块 1920x1080，加上标题栏
    就超出屏幕了。高度这一侧才是先到头的那个。"""
    by_both = w.fit_tile_width(1280, 720, 4, 1920, 950)
    assert by_both < 1920 / 2, "只按宽度算了"


def test_explicit_width_still_wins():
    """`--wall-width 480` 是帧率紧张时的手动挡，不能被自动铺满顶掉。"""
    canvas = w.compose([frame(10)] * 4, list("abcd"), tile_w=480, screen=(2560, 1440))
    assert canvas.shape[1] == 960


@pytest.mark.parametrize("n", [1, 2, 3, 4, 6, 7])
def test_zoom_keeps_the_window_the_same_size(n):
    """点一下窗口就变大、再点又变小的话，看着像在跳。

    **每种路数都要试。** 2x2 的时候「格子宽的两倍」正好等于整块宽度，
    只测 4 路的话这个巧合会让错的实现也通过；2 路（2x1）和 7 路（3x3）
    才露馅。
    """
    frames = [frame(10 + i * 10) for i in range(n)]
    labels = [f"c{i}" for i in range(n)]
    grid = w.compose(frames, labels, screen=(1920, 1080))
    for i in range(n):
        z = w.compose(frames, labels, zoom=i, screen=(1920, 1080))
        assert abs(z.shape[0] - grid.shape[0]) <= 2, (n, i, z.shape, grid.shape)
        assert abs(z.shape[1] - grid.shape[1]) <= 2, (n, i, z.shape, grid.shape)


def test_screen_size_falls_back_to_1080p():
    """拿不到屏幕尺寸（非 Windows、或者调用失败）时不能退回一个小值。"""
    assert w.screen_size()[0] >= 1280
    assert w.DEFAULT_SCREEN == (1920, 1080)


def test_wall_width_zero_means_auto():
    """录制脚本用 0 表示"自动"，传给 compose 前要变成 None。"""
    code = _recorder_src()
    assert "tile_w=args.wall_width or None" in code


# ── 标签要小、要是 ASCII ──────────────────────────────────────────────────


def test_the_label_is_a_small_corner_chip_not_a_full_width_band():
    """**第一版的标签条按格子高度的九分之一算，自动铺满之后就是一条巨大的
    黑带，把画面上面一截整个吃掉，字还写不下被切断。**

    标签是"扫一眼确认这路还活着"，不是主角。
    """
    tile = np.full((475, 844, 3), 120, dtype=np.uint8)
    w.label_tile(tile, "cam1  26fps  xiaobai 46Hz")
    ys, xs = np.where((tile != 120).any(axis=2))
    assert ys.max() < 475 * 0.10, f"标签占了画面高度的 {ys.max() / 475:.0%}"
    assert xs.max() < 844 * 0.5, f"标签横贯了画面宽度的 {xs.max() / 844:.0%}"


def test_the_label_does_not_grow_with_the_tile():
    """字号跟着格子放大的话，1080p 铺满之后就是三倍大的巨字。"""
    small = np.full((270, 480, 3), 120, dtype=np.uint8)
    big = np.full((950, 1688, 3), 120, dtype=np.uint8)
    w.label_tile(small, "cam1 26fps")
    w.label_tile(big, "cam1 26fps")
    h_small = np.where((small != 120).any(axis=2))[0].max()
    h_big = np.where((big != 120).any(axis=2))[0].max()
    assert abs(h_small - h_big) <= 2, f"小格 {h_small}px、大格 {h_big}px，字号跟着涨了"


def test_the_label_stays_inside_a_narrow_tile():
    """字超出格子会被切断——现场截图里 cam1 那行就被 cam2 的格子切掉半截。"""
    tile = np.full((90, 160, 3), 120, dtype=np.uint8)
    w.label_tile(tile, "cam1  26fps  xiaobai 46Hz  xiaobai2 50Hz")
    assert tile.shape == (90, 160, 3)


def test_non_ascii_never_reaches_putText():
    """OpenCV 的 Hershey 字体没有中文，画出来是一串方框/问号。"""
    assert w._ascii("监控墙") == "???"
    assert w._ascii("cam1 26fps") == "cam1 26fps"
    # 画中文 和 画同样长度的 '?' 必须一模一样——不一样就说明中文真的送进
    # putText 了（它不会报错，只是画出一串看不懂的东西）
    a = np.zeros((200, 400, 3), dtype=np.uint8)
    b = np.zeros((200, 400, 3), dtype=np.uint8)
    w.label_tile(a, "cam1 小白")
    w.label_tile(b, "cam1 ??")
    assert np.array_equal(a, b), "中文没有先转成 ASCII 就画上去了"


def test_the_window_title_is_ascii():
    """Windows 上 OpenCV 按本地编码建窗口，中文标题出来是乱码
    （现场截图：'鍵聂帘澶?IMU(multicam)'）。"""
    code = _recorder_src()
    line = next(l for l in code.splitlines() if "WALL_WIN = " in l)
    line.encode("ascii")            # 有中文就在这儿抛


def test_the_zoom_hint_is_ascii():
    src = open(os.path.join(REPO, "preview_wall.py"), encoding="utf-8").read()
    hints = [l for l in src.splitlines() if "click to zoom out" in l]
    assert hints, "放大提示不见了"
    for l in hints:
        l.encode("ascii")           # 有中文就在这儿抛


def test_an_empty_label_draws_nothing():
    """画面里本来就带叠加信息的场地（save_overlay 开着的），墙再写一遍
    就是同样的数字并排出现两次。"""
    tile = np.full((200, 400, 3), 77, dtype=np.uint8)
    w.label_tile(tile, "")
    w.label_tile(tile, "   ")
    assert (tile == 77).all(), "空标签还是画了东西"


def test_the_wall_skips_labels_when_the_frame_already_has_the_overlay():
    code = _recorder_src()
    assert "if save_overlay:" in code.split("labels = []", 1)[1][:400], \
        "画面里已经有叠加信息时，墙还是又写了一遍"


# ── 标签分段上色：掉了的项圈红字 MISSING ──────────────────────────────────


def _red_pixels(tile):
    """红字像素：R 高、G/B 低（BGR 排列）。"""
    b, g, r = tile[..., 0], tile[..., 1], tile[..., 2]
    return int(((r > 180) & (g < 120) & (b < 120)).sum())


def test_a_missing_imu_is_drawn_in_red():
    """现场盯着墙就是为了看哪个项圈掉了：掉了的那一段必须是红的、写着 MISSING，
    不是一个不起眼的 `--`。"""
    tile = np.full((200, 600, 3), 90, dtype=np.uint8)
    w.label_tile(tile, [("cam1", "ok"), ("25fps", "ok"), ("xiaobai MISSING 12%", "bad")])
    assert _red_pixels(tile) > 30, "MISSING 没画成红色"


def test_only_the_missing_segment_is_red_not_the_whole_line():
    """两个项圈一个掉了一个没掉：整行一起变红就分不出是哪个。"""
    tile_mixed = np.full((200, 600, 3), 90, dtype=np.uint8)
    w.label_tile(tile_mixed, [("cam1 25fps", "ok"), ("keji 50Hz", "ok"), ("keji MISSING 3%", "bad")])
    tile_all_bad = np.full((200, 600, 3), 90, dtype=np.uint8)
    w.label_tile(tile_all_bad, [("cam1 25fps", "bad"), ("keji 50Hz", "bad"), ("keji MISSING 3%", "bad")])
    assert 0 < _red_pixels(tile_mixed) < _red_pixels(tile_all_bad) * 0.7


def test_plain_string_labels_still_work():
    """老调用（一整串文字）不受影响。"""
    tile = np.full((200, 400, 3), 90, dtype=np.uint8)
    w.label_tile(tile, "cam1 25fps xiaobai 50Hz")
    assert (tile != 90).any() and _red_pixels(tile) == 0


def test_segments_are_ascii_too():
    a = np.zeros((200, 600, 3), dtype=np.uint8)
    b = np.zeros((200, 600, 3), dtype=np.uint8)
    w.label_tile(a, [("cam1", "ok"), ("小白 MISSING", "bad")])
    w.label_tile(b, [("cam1", "ok"), ("?? MISSING", "bad")])
    assert np.array_equal(a, b)


def test_zoom_keeps_segment_colors():
    frames = [frame(10), frame(20)]
    z = w.compose(frames, [[("cam1", "ok"), ("xiaobai MISSING 5%", "bad")], "cam2 25fps"],
                  zoom=0, screen=(1920, 1080))
    assert _red_pixels(z) > 30


def test_recorder_writes_missing_not_dashes():
    """需求原话：missing 的时候要显示红色、显示 MISSING，不是 `--`。"""
    code = _recorder_src()
    block = code.split("labels = []", 1)[1].split("labels.append(segs)", 1)[0]
    assert "MISSING" in block and "'bad'" in block
    assert '"--"' not in block and "'--'" not in block


def test_recorder_shows_a_rolling_miss_rate_not_just_the_instant():
    """瞬时状态会闪（蓝牙一批一批送），只看它分不出"稳不稳"；要有最近几秒的比例。"""
    code = _recorder_src()
    assert "wall_miss = {d.label: _deque(maxlen=int(target_fps * 10))" in code
    assert "wall_miss[d.label].append(1 if (missing or imu_row is None) else 0)" in code
