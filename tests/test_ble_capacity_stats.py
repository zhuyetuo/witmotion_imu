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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

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


# ── 第一版跑出来的三个假象 ────────────────────────────────────────────────


def test_hz_uses_the_data_span_not_the_wall_clock_end():
    """狗场两台跑出来 Hz 按连接顺序一路递减（33→21、40→28）——是分母的事：
    结束时刻是收尾之后才记的，数据早停了。晚连的分母里收尾占比更大。"""
    d = _dev([1.0 + i * 0.02 for i in range(500)])      # 1.0s 起、10 秒、50Hz
    assert abs(d.hz(17.0) - 50.0) < 0.5, "把收尾时间算进分母了"
    assert abs(d.hz() - 50.0) < 0.5


def test_link_rate_reads_the_connection_interval():
    """狗场1：每 59ms 推 2 个 → 上限 34Hz；狗场2：每 30ms 推 2 个 → 66Hz。
    这个数 10 秒就准，而且直接指向适配器。"""
    slow = _dev([t for k in range(170) for t in (k * 0.059, k * 0.059)])
    fast = _dev([t for k in range(330) for t in (k * 0.030, k * 0.030)])
    assert 32 < slow.link_rate_hz() < 36
    assert 62 < fast.link_rate_hz() < 70


def test_closing_disconnect_is_not_counted():
    """测完自己断开也会触发 disconnected_callback。第一版把它算成「掉线 1 次」，
    两台机器每个设备都"掉线 1 次"，全是自己断的。"""
    src = open(os.path.join(REPO_ROOT, "check_ble_capacity.py"), encoding="utf-8").read()
    body = src.split("def _on_disconnect(_client):", 1)[1].split("\n\n", 1)[0]
    assert "st.closing" in body
    assert "st.closing = True" in src.split("await asyncio.sleep(duration)", 1)[1][:200]


def test_each_device_gets_the_full_duration_after_it_connects():
    """六个错开连完要十几秒，跑 10 秒的话最后一台只剩两三秒数据。
    每台连上之后各跑满 duration。"""
    src = open(os.path.join(REPO_ROOT, "check_ble_capacity.py"), encoding="utf-8").read()
    assert "t_end_wall" not in src, "还在用全局收工时刻"
    assert "await asyncio.sleep(duration)" in src


def test_monitor_divides_by_the_actual_interval():
    """最后一段只有十几秒也除以 60，打出来「本分钟最低 4.3Hz」，纯属吓人。"""
    src = open(os.path.join(REPO_ROOT, "check_ble_capacity.py"), encoding="utf-8").read()
    body = src.split("async def monitor(", 1)[1].split("\ndef ", 1)[0]
    assert "got / 60.0" not in body
    assert "got / span" in body
