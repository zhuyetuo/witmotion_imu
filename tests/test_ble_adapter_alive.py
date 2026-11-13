"""有设备在收数据时，别把「收不到广播」当成适配器假死。

2026-11-12 狗场1/2：充电那组项圈拿去了别的屋子，整天收不到；戴着的那组连着（连着就不广播）。
「有设备掉线 + 90 秒没广播」一直成立 → 每 90 秒重建扫描器 → 每 3 次判一次「适配器假死」去禁用 /
启用蓝牙适配器（15 分钟一次）→ WiFi 蓝牙一体网卡被拔插得 WiFi 一起掉、整机卡死，每天要重启。
"""

from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fake_bleak = types.ModuleType("bleak")
_fake_bleak.BleakClient = object
_fake_bleak.BleakScanner = object
sys.modules.setdefault("bleak", _fake_bleak)

import imu_camera_sync_multi as m  # noqa: E402


class _FakeBleakScanner:
    def __init__(self, detection_callback=None):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass


def test_数据在进_看门狗越等越久_有上限():
    assert m.next_stall_limit(90, True, 90, 1800) == 180
    assert m.next_stall_limit(1200, True, 90, 1800) == 1800
    assert m.next_stall_limit(1800, True, 90, 1800) == 1800


def test_一个都没连着_保持短间隔():
    assert m.next_stall_limit(720, False, 90, 1800) == 90


def test_数据在进_重建多少次都不复位适配器(monkeypatch):
    monkeypatch.setattr(m, "BleakScanner", _FakeBleakScanner)
    resets = []
    monkeypatch.setattr(m.bt_adapter_reset, "available", lambda: (True, ""))
    monkeypatch.setattr(m.bt_adapter_reset, "reset_adapter", lambda why: resets.append(why) or (True, "ok"))
    monkeypatch.setattr(m.asyncio, "sleep", _no_sleep)
    sc = m.SharedScanner()
    sc._alive_fn = lambda: True          # 戴着的那组在收数据
    for _ in range(10):
        asyncio.run(sc._restart_locked("测试"))
    assert resets == [], "数据在进说明适配器是好的，不能去禁用 / 启用它"
    # 一个都没连着、一直零广播：照旧会复位（真假死的那种）
    sc._alive_fn = lambda: False
    for _ in range(3):
        asyncio.run(sc._restart_locked("测试"))
    assert len(resets) == 1


async def _no_sleep(_s):
    return None
