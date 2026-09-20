"""整点切段的收尾要在后台做，下一段立刻开录。

背景（真实数据，2026-09-15 狗场）：平台上每只狗"录了多久"一天最多 23 小时 46 分，
文件名也看得出来——每段都是 xx:00:12~16 才开始。差的那十几秒就是上一段的收尾：
等六七路 ffmpeg 刷完缓冲、逐路核对帧数、生成配对 CSV 和硬链接，做完才开下一段。
一天 24 段就丢十几分钟。
"""

from __future__ import annotations

import inspect
import os
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import imu_camera_sync_multicam as m  # noqa: E402


def test_loop_mode_finalizes_in_background_and_waits_on_exit():
    src = inspect.getsource(m._run_one_segment)
    fin = src[src.rindex("    finally:"):]
    # 句柄先摘下来（下一段要挂新的），收尾函数拿的是摘下来的副本
    assert "cam.video_writer = None" in fin and "d.set_raw_writer(None)" in fin
    # 循环录制时起线程；最后一段就地收尾
    assert "threading.Thread(target=_finalize_guarded" in fin
    assert "if record_mode and args.loop and not should_stop[0]:" in fin
    # 主循环退出前等后台收尾写完，不然进程退了配对文件还没生成
    run_src = inspect.getsource(m.run_cameras)
    assert "wait_finalizers()" in run_src


def test_finalize_error_does_not_propagate(capsys):
    def boom():
        raise RuntimeError("坏了")

    m._finalize_guarded(boom, "/x/multicam_1")
    assert "收尾 multicam_1 出错" in capsys.readouterr().out


def test_wait_finalizers_joins_all():
    done = []
    for i in range(3):
        t = threading.Thread(target=lambda i=i: done.append(i))
        t.start()
        m._finalize_threads.append(t)
    m.wait_finalizers()
    assert sorted(done) == [0, 1, 2] and m._finalize_threads == []
