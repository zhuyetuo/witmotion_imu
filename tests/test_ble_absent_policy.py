"""设备长时间收不到广播之后，还能不能自己回来。

现场故障（影棚，ALL_DEVICES=1 通宵录）：某个设备信号断了之后**一整天再也连
不上**，只有杀掉命令重新跑才能连上。

成因是「重建扫描器满 3 次就永久不再为它重建」那条：广播收不到的原因之一
恰恰是本机蓝牙栈留着陈旧的连接/配对记录，而能解开它的动作就是重建。于是

    收不到广播 → 放弃重建 → 更收不到广播

而"放弃"标志只在收到广播时才撤销，广播永远不会来。这个循环没有出口，
除非重启进程。

    pytest tests/ -q        （在仓库根目录跑；不需要蓝牙硬件）
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# bleak 在没有蓝牙的机器上装不了，而这些测试一行真实蓝牙代码都不碰。
# 用假模块顶掉，好让 CI / 开发机上跑得起来。
class _FakeClient:
    """默认连不上——大部分用例关心的是"连不上之后还试不试"。"""

    instances: list = []

    def __init__(self, target, disconnected_callback=None):
        self.target = target
        _FakeClient.instances.append(self)

    async def connect(self):
        raise RuntimeError("le-connection-abort-by-local")

    async def disconnect(self):
        pass


_fake_bleak = types.ModuleType("bleak")
_fake_bleak.BleakClient = _FakeClient
_fake_bleak.BleakScanner = object
sys.modules.setdefault("bleak", _fake_bleak)

import imu_camera_sync_multi as m  # noqa: E402


# ── 纯函数：间隔怎么走 ────────────────────────────────────────────────────


def test_rescan_interval_doubles_before_the_cap():
    assert m.next_rescan_after(120.0, rescans_done=1, scanner_alive=True,
                               max_rescans=3, cap_s=600.0) == 240.0


def test_rescan_interval_stops_at_the_cap():
    assert m.next_rescan_after(600.0, rescans_done=1, scanner_alive=True,
                               max_rescans=3, cap_s=600.0) == 600.0


def test_absent_device_backs_off_to_the_long_interval():
    """认定"多半真不在"之后拉长到半小时——充电座上那几个不该整夜折腾蓝牙栈。"""
    got = m.next_rescan_after(600.0, rescans_done=3, scanner_alive=True,
                              max_rescans=3, cap_s=600.0)
    assert got == m.ABSENT_RESCAN_MIN_INTERVAL_S == 1800.0


def test_absent_device_never_stops_rescanning():
    """**这条就是那个一天连不上的 bug。**

    永久放弃的话，"收不到广播"和"不再重建"互为因果，循环没有出口。
    间隔可以很长，但必须是个有限的数。
    """
    interval = 120.0
    for n in range(1, 50):
        interval = m.next_rescan_after(interval, rescans_done=n, scanner_alive=True,
                                       max_rescans=3, cap_s=600.0)
        assert interval < float("inf")
    assert interval <= m.ABSENT_RESCAN_MIN_INTERVAL_S, "拉长也要有上限，不能越滚越大"


def test_scanner_looks_dead_keeps_the_short_interval():
    """扫描器自己可能死了（WinRT 上 watcher 被系统 abort）。这种时候重建是
    有用的，别因为"重建过三次"就把它降级成半小时一次。"""
    got = m.next_rescan_after(120.0, rescans_done=9, scanner_alive=False,
                              max_rescans=3, cap_s=600.0)
    assert got == 240.0


# ── 纯函数：盲连 ──────────────────────────────────────────────────────────


def test_blind_connect_waits_for_its_interval():
    assert m.should_blind_connect(60.0, have_mac=True) is False
    assert m.should_blind_connect(m.BLIND_CONNECT_INTERVAL_S, have_mac=True) is True


def test_no_mac_no_blind_connect():
    """只配了名字的设备没法盲连——名字是从广播里读的，而广播正好收不到。"""
    assert m.should_blind_connect(99999.0, have_mac=False) is False


# ── 整个重连循环 ──────────────────────────────────────────────────────────


class _FakeScanner:
    """只实现 run_wit_device 用到的那几个口子。"""

    STALL_RESTART_S = 90.0

    def __init__(self, found=None, alive=True, clock=None):
        self.found = found          # find() 返回什么
        self.restarts = []
        # alive=True 表示扫描器在正常收**别的**设备的广播（影棚里手机、别的
        # 项圈一直在广播，这是常态）。这一点很要紧：真正的现场就是"扫描器活着、
        # 唯独这一个设备的广播上不来"，那正是原来那条永久放弃的触发条件。
        self.alive = alive
        self._clock = clock if clock is not None else [0.0]

    @property
    def last_detect(self):
        return self._clock[0] if self.alive else 0.0

    def find(self, name_filter=None, address=None):
        return self.found

    async def restart(self, reason):
        self.restarts.append(reason)

    def evict(self, address):
        pass


def _run_device_for(device, scanner, *, fake_clock, stop_after_s):
    """把 run_wit_device 跑到假时钟走完 stop_after_s 为止。

    时间是假的（time.monotonic 被换掉），asyncio.sleep 也被换成"推进假时钟、
    立刻返回"，所以一天份的重连循环几毫秒就跑完了——真等一天没法测。
    """
    m.stop_event.clear()

    real_sleep, real_mono = asyncio.sleep, m.time.monotonic

    async def fake_sleep(sec):
        fake_clock[0] += sec
        if fake_clock[0] >= stop_after_s:
            m.stop_event.set()
        # 用抓下来的**真** sleep 让出一次；直接调 asyncio.sleep 是在调自己
        await real_sleep(0)

    m.time.monotonic = lambda: fake_clock[0]
    asyncio.sleep = fake_sleep
    try:
        asyncio.run(m.run_wit_device(device, scanner))
    finally:
        asyncio.sleep, m.time.monotonic = real_sleep, real_mono
        m.stop_event.set()


def _device(mac="D1:FD:A8:C7:1A:EF"):
    d = m.ImuDevice(label="imu1", dev_type="wit", ident="WT1")
    d.mac = mac
    return d


@pytest.fixture(autouse=True)
def _clean_clients():
    _FakeClient.instances.clear()
    yield
    m.stop_event.set()


def test_a_device_absent_all_day_is_still_being_retried(monkeypatch):
    """**缺席一整天之后还在试**——这是这个修复的全部意义。

    原来的代码在第 3 次重建之后就彻底不动了，后面 23 小时一次蓝牙操作都没有，
    所以设备信号恢复了也回不来（陈旧状态没人去清），只能重启命令。
    """
    monkeypatch.setattr(m, "BleakClient", _FakeClient)
    clock = [0.0]
    scanner = _FakeScanner(found=None, clock=clock)   # 一整天都收不到这个设备的广播
    _run_device_for(_device(), scanner, fake_clock=clock, stop_after_s=86400.0)

    assert scanner.restarts, "一天下来一次扫描器重建都没有 = 彻底放弃了"
    assert len(scanner.restarts) >= 20, f"半小时一次的话一天该有 40 次左右，实际 {len(scanner.restarts)}"
    assert _FakeClient.instances, "一天下来一次盲连都没试过"


def test_absent_day_does_not_thrash_the_bluetooth_stack(monkeypatch):
    """另一半要求：别退回到"每两分钟动一次蓝牙栈"那种折腾。

    一天 40 次左右的重建（半小时一次）是可以接受的；400 次就不行了——
    正是那种折腾把 WinRT 的蓝牙服务拖到假死、只能重启电脑。
    """
    monkeypatch.setattr(m, "BleakClient", _FakeClient)
    clock = [0.0]
    scanner = _FakeScanner(found=None, clock=clock)
    _run_device_for(_device(), scanner, fake_clock=clock, stop_after_s=86400.0)
    assert len(scanner.restarts) <= 60, f"重建太频繁：一天 {len(scanner.restarts)} 次"
    assert len(_FakeClient.instances) <= 60, f"盲连太频繁：一天 {len(_FakeClient.instances)} 次"


def test_blind_connect_uses_the_mac(monkeypatch):
    monkeypatch.setattr(m, "BleakClient", _FakeClient)
    clock = [0.0]
    scanner = _FakeScanner(found=None, clock=clock)
    _run_device_for(_device("AA:BB:CC:DD:EE:FF"), scanner,
                    fake_clock=clock, stop_after_s=7200.0)
    assert _FakeClient.instances
    assert all(c.target == "AA:BB:CC:DD:EE:FF" for c in _FakeClient.instances)


def test_no_mac_means_no_blind_connect_but_rescans_continue(monkeypatch):
    """没 MAC 的设备盲连不了，但重建扫描器这条路不能跟着一起停。"""
    monkeypatch.setattr(m, "BleakClient", _FakeClient)
    d = m.ImuDevice(label="imu1", dev_type="wit", ident="WT1")
    d.mac = None
    clock = [0.0]
    scanner = _FakeScanner(found=None, clock=clock)
    _run_device_for(d, scanner, fake_clock=clock, stop_after_s=86400.0)
    assert not _FakeClient.instances
    assert len(scanner.restarts) >= 20


class _GoodClient(_FakeClient):
    """连得上、订阅得上的客户端。"""

    async def connect(self):
        return True

    async def start_notify(self, uuid, cb):
        return None


class _OnlyWithAdvertClient(_GoodClient):
    """只有拿到广播对象才连得上；盲连（传的是 MAC 字符串）一律失败。

    用来演真正的"设备不在场"：盲连怎么试都不会成功，只有它重新广播了才行。
    """

    async def connect(self):
        if isinstance(self.target, str):
            raise RuntimeError("le-connection-abort-by-local")
        return True


class _Advert:
    def __init__(self, name="WT1", address="D1:FD:A8:C7:1A:EF"):
        self.name, self.address = name, address


def test_it_comes_back_by_itself_after_a_day_missing(monkeypatch):
    """**这就是现场要的结果**：设备缺席一整天，广播一回来就自己连上，
    不用人去杀掉命令重跑。

    修之前走不到这一步——第 3 次重建之后循环里再没有任何蓝牙动作，
    陈旧的蓝牙栈状态没人清，广播也就一直上不来。
    """
    monkeypatch.setattr(m, "BleakClient", _OnlyWithAdvertClient)
    clock = [0.0]
    scanner = _FakeScanner(found=None, clock=clock)

    real_find = scanner.find

    def find_after_a_day(**kw):
        return _Advert() if clock[0] >= 86400.0 else real_find(**kw)

    scanner.find = find_after_a_day
    d = _device()
    _run_device_for(d, scanner, fake_clock=clock, stop_after_s=86500.0)

    assert d.mac == "D1:FD:A8:C7:1A:EF"
    # 连上的那次用的是广播给的 BLEDevice 对象，不是盲连
    assert any(getattr(c.target, "address", None) == "D1:FD:A8:C7:1A:EF"
               for c in _FakeClient.instances), "广播回来之后应该走正常连接这条路"


def test_a_blind_connect_that_works_resets_the_long_interval(monkeypatch):
    """盲连成功 = 设备其实一直在，只是广播上不来。这时候要把"缺席很久"的
    计时清掉，否则下次一断开又要按半小时的长间隔白等。"""
    monkeypatch.setattr(m, "BleakClient", _GoodClient)
    clock = [0.0]
    scanner = _FakeScanner(found=None, clock=clock)
    d = _device()
    _run_device_for(d, scanner, fake_clock=clock, stop_after_s=7200.0)
    # 盲连成功之后进入"已连接"的等待循环，不会再反复重建扫描器
    assert len(scanner.restarts) <= 6, f"盲连成功之后还在猛重建：{len(scanner.restarts)} 次"


def test_fails_keep_counting_when_the_advert_is_there_all_along(monkeypatch):
    """广播一直收得到、只是连不上：失败计数**不能**每轮清零。

    清了的话永远到不了"连着失败 3 次就重建扫描器""失败 6 次改按地址连"
    这两级补救——而那两级正是治"广播在、握手失败"的。
    """
    monkeypatch.setattr(m, "BleakClient", _FakeClient)   # 永远连不上
    clock = [0.0]
    scanner = _FakeScanner(found=_Advert(), clock=clock)
    _run_device_for(_device(), scanner, fake_clock=clock, stop_after_s=600.0)

    assert scanner.restarts, "连着失败这么多次，一次扫描器重建都没触发"
    assert any(isinstance(c.target, str) for c in _FakeClient.instances), \
        "失败够多次之后应该改成按地址连"
