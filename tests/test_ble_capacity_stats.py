"""check_ble_capacity 的到包节奏统计：能不能把「真丢包」和「攒批送包」分开。

现场困惑：狗场1 监控墙上项圈来回闪 MISSING，但每分钟一行的「缺x%」不高，
狗场2 一切平稳。两种情况到包节奏长得完全不同：

  逐包送：50Hz，每 20ms 一条，每次 notify 1~2 个样本
  攒批送：每 400ms 一次，一次 20 个样本——Hz 一样是 50，可夹在两批之间的
          帧就会在墙上闪 MISSING（录制的容忍窗口是 120ms）
  真丢包：Hz 明显低于 50

不连蓝牙：bleak 用假模块顶掉，直接喂时间戳。
"""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fake = types.ModuleType("bleak")
_fake.BleakClient = _fake.BleakScanner = object
sys.modules.setdefault("bleak", _fake)

import check_ble_capacity as c  # noqa: E402


def _dev(stamps):
    d = c.DevStat("x", "imu9/xiaobai")
    d.stamps = list(stamps)
    return d


def test_per_packet_delivery_looks_normal():
    """50Hz 逐包送：间隔 20ms，没有超过 120ms 的，每包 1 个。"""
    d = _dev([i * 0.02 for i in range(500)])
    gs = d.gap_stats()
    assert abs(gs["median_gap"] - 0.02) < 1e-6
    assert gs["frac_over"] == 0.0
    assert abs(gs["batch"] - 1.0) < 1e-6
    assert abs(d.hz(10.0) - 50.0) < 1.0


def test_batched_delivery_keeps_hz_but_gaps_exceed_the_lag_limit():
    """**这就是狗场1 的模式。** 每 400ms 一批、一批 20 个：Hz 还是 50，
    但每个间隔都超过 120ms——墙上必然闪，数据却一条不少。"""
    stamps = []
    for k in range(25):                 # 10 秒
        stamps += [k * 0.4] * 20
    d = _dev(stamps)
    gs = d.gap_stats()
    assert abs(gs["median_gap"] - 0.4) < 1e-6
    assert gs["frac_over"] == 1.0
    assert abs(gs["batch"] - 20.0) < 1e-6
    assert d.hz(10.0) > 45.0, "攒批送包不该被判成 Hz 低"


def test_real_packet_loss_shows_as_low_hz_not_as_batching():
    """真丢包：逐包送但只剩 30Hz。间隔中位数还是正常的，Hz 才是证据。"""
    d = _dev([i * (1 / 30) for i in range(300)])
    gs = d.gap_stats()
    assert gs["frac_over"] == 0.0
    assert d.hz(10.0) < 45.0


def test_gap_stats_survive_a_single_packet():
    assert _dev([0.0]).gap_stats()["frac_over"] == 0.0
    assert _dev([]).gap_stats()["batch"] == 0.0


def test_lag_limit_matches_the_recorder():
    """录制里 max_lag_ms = 3 / fps，25fps 就是 120ms。两边对不上，这里算的
    「>120ms 比例」就跟墙上闪的比例对不上。"""
    assert abs(c.LAG_LIMIT_S - 3 / 25) < 1e-9


def test_report_flags_batching_without_calling_it_a_failure(capsys):
    """攒批要在表里点出来（人得知道闪的原因），但它不是"顶不住"。"""
    stamps = []
    for k in range(25):
        stamps += [k * 0.4] * 20
    d = _dev(stamps)
    d.connected_at = 0.5
    rc = c.report([d], 10.0, 50.0)
    out = capsys.readouterr().out
    # 说明文字里本来就有"攒批送包"四个字，要认的是**这台设备被点名了**
    assert "imu9/xiaobai 是攒批送包" in out
    assert "~ 攒批送包（100%" in out
    assert rc == 0, "攒批送包数据没丢，不该判成「顶不住」"
    assert "顶得住" in out


def test_real_loss_still_fails_the_verdict(capsys):
    d = _dev([i * (1 / 30) for i in range(300)])
    d.connected_at = 0.5
    rc = c.report([d], 10.0, 50.0)
    out = capsys.readouterr().out
    assert rc != 0 and "Hz 偏低" in out
