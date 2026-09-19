"""蓝牙适配器自动复位：什么时候复位（连着几次重建扫描器一条广播都没有）、冷却、
做不了时只提示一次、复位在线程里跑不卡事件循环。不碰真蓝牙。"""

from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeBleakScanner:
    started = 0

    def __init__(self, detection_callback=None):
        self.cb = detection_callback

    async def start(self):
        _FakeBleakScanner.started += 1

    async def stop(self):
        pass


_fake_bleak = types.ModuleType("bleak")
_fake_bleak.BleakClient = object
_fake_bleak.BleakScanner = _FakeBleakScanner
sys.modules.setdefault("bleak", _fake_bleak)

import bt_adapter_reset as r  # noqa: E402
import imu_camera_sync_multi as m  # noqa: E402


def test_policy_counts_restarts_without_adverts_and_cools_down():
    p = r.ResetPolicy(restarts_before=3, cooldown_s=900)
    for _ in range(2):
        p.on_restart()
    assert not p.should_reset(100.0)
    p.on_advert()                      # 中间来了一条广播：计数清零
    p.on_restart()
    assert not p.should_reset(100.0)
    p.on_restart(); p.on_restart()
    assert p.should_reset(100.0)
    p.did_reset(100.0)
    p.on_restart(); p.on_restart(); p.on_restart()
    assert not p.should_reset(500.0)   # 冷却期内
    assert p.should_reset(1001.0)


def test_env_switch_and_platform_gate(monkeypatch):
    monkeypatch.setenv("BT_RESET", "0")
    assert not r.enabled() and r.available()[0] is False and "BT_RESET" in r.available()[1]
    monkeypatch.delenv("BT_RESET")
    monkeypatch.setattr(r.sys, "platform", "linux")
    ok, why = r.available()
    assert not ok and "Windows" in why
    ok, why = r.reset_adapter("x")
    assert not ok and "Windows" in why


def _scanner_with_fake_reset(monkeypatch, *, can: bool):
    calls = []
    monkeypatch.setattr(r, "available", lambda: (can, "" if can else "不是管理员"))
    monkeypatch.setattr(r, "reset_adapter", lambda reason="": (calls.append(reason) or (True, "已复位 1 个适配器")))
    # 别的测试文件可能先装了 BleakScanner=object 的假模块，按测试顺序不同这里拿到的不一样
    monkeypatch.setattr(m, "BleakScanner", _FakeBleakScanner)
    s = m.SharedScanner()
    s.RESTART_COOLDOWN_S = 0.0
    return s, calls


def test_three_silent_restarts_reset_the_adapter_once(monkeypatch, capsys):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _s: real_sleep(0))
    s, calls = _scanner_with_fake_reset(monkeypatch, can=True)

    async def go():
        await s.start()
        await s.restart("a")
        await s.restart("b")
        assert not calls
        await s.restart("c")               # 第三次、期间零广播 → 复位
        assert len(calls) == 1
        await s.restart("d")               # 冷却期内不再复位
        assert len(calls) == 1
        # 来了广播 → 计数清零，之后再连着三次才会考虑（还在冷却期，也不复位）
        s._on_detect(types.SimpleNamespace(address="aa:bb"), None)
        assert s.reset_policy.restarts_without_advert == 0
        await s.stop()

    asyncio.run(go())
    out = capsys.readouterr().out
    assert "复位完成" in out and "已复位" in out
    assert _FakeBleakScanner.started >= 5     # 复位完扫描器照常重建


def test_cannot_reset_says_so_once(monkeypatch, capsys):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _s: real_sleep(0))
    s, calls = _scanner_with_fake_reset(monkeypatch, can=False)

    async def go():
        await s.start()
        for x in "abcdef":
            await s.restart(x)
        await s.stop()

    asyncio.run(go())
    assert not calls
    out = capsys.readouterr().out
    assert out.count("不是管理员") == 1 and "设备管理器" in out
