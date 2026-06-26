# -*- coding: utf-8 -*-
import asyncio
import time
from bleak import BleakClient, BleakScanner


class HzCounter:
    """滑动1秒窗口实时采样率计算器。"""
    def __init__(self):
        self._window: list[float] = []

    def tick(self) -> float:
        now = time.time()
        cutoff = now - 1.0
        while self._window and self._window[0] < cutoff:
            self._window.pop(0)
        self._window.append(now)
        return float(len(self._window))


async def scan_devices(timeout: float = 6.0):
    print(f'扫描 BLE 设备中（{timeout:.0f} 秒）...')
    devices = await BleakScanner.discover(timeout=timeout)
    if not devices:
        print('未发现任何 BLE 设备。请确认设备已开机、蓝牙已打开。')
        return
    print(f'发现 {len(devices)} 个设备:')
    for d in sorted(devices, key=lambda x: x.name or ''):
        name = d.name or '(无名称)'
        print(f'  {name:<30s}  {d.address}')


async def find_device(name_filter: str | None, address: str | None, timeout: float = 8.0):
    """按名称关键字或 MAC 地址查找 BLE 设备。address 优先于 name_filter。"""
    if address:
        print(f'按地址查找设备: {address}')
        dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if dev is None:
            print(f'未找到地址为 {address} 的设备，请确认设备已开机、在范围内。')
        return dev

    print(f'扫描中，查找名称包含 "{name_filter}" 的设备（最多等待 {timeout:.0f} 秒）...')
    found: dict = {}

    def _cb(device, adv_data):
        found[device.address] = device

    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    deadline = time.time() + timeout
    target = None
    while time.time() < deadline:
        await asyncio.sleep(0.3)
        for dev in found.values():
            if dev.name and name_filter.lower() in dev.name.lower():
                target = dev
                break
        if target:
            break
    await scanner.stop()

    if target is None:
        print(f'未找到名称包含 "{name_filter}" 的设备。已发现的设备:')
        for dev in found.values():
            print(f'  - {dev.name or "(无名称)"}  地址: {dev.address}')
    return target


async def list_services(client: BleakClient):
    """列出已连接设备的所有 GATT 服务和特征值。"""
    print('GATT 服务/特征值:')
    for svc in client.services:
        print(f'  服务  {svc.uuid}')
        for ch in svc.characteristics:
            props = ','.join(ch.properties)
            print(f'    特征 {ch.uuid}  [{props}]  handle={ch.handle}')
