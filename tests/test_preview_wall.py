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
