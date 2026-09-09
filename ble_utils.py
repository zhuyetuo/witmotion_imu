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



def match_by_name(ident: str, candidates):
    """
    按名字从一堆 BLE 设备里挑出目标。返回 (设备, 错误说明)，两者只会有一个非空。

    candidates 是 (BLEDevice, 最后见到的时间) 的可迭代对象。

    规则是"精确优先，含糊就报错"，而不是原来的"名字包含就算、取最近见到的那个"：

    设备名从出厂的 WT901BLE68 改成 WT1…WT8 之后，子串匹配开始变得危险——一旦
    有了 WT10，`wit=WT1` 会同时命中 WT1 和 WT10，取哪个全看这一瞬间谁的广播更新，
    每次重连都可能换一个，而且一声不吭。表现是文件名里的 imu1 今天是这个设备、
    明天是另一个，数据串到别的狗身上，事后从文件里完全看不出来。设备要扩到 20 个，
    WT1/WT10、WT2/WT20 都会撞。

    所以：名字完全相同（忽略大小写）就直接用它，不管还有几个是子串命中的；
    没有精确命中而子串命中不止一个，就返回错误让人去写全名或 MAC，绝不猜。

    这个规则录制（SharedScanner.find）和各种小工具（find_device）共用一份，
    两条路必须一致，否则同一个 --imu 参数在不同脚本里会连到不同的设备。
    """
    ident_l = ident.lower()
    exact = [d for d, _ in candidates if d.name and d.name.lower() == ident_l]
    if exact:
        return exact[0], None
    subs = {}
    for d, ts in candidates:
        if d.name and ident_l in d.name.lower():
            prev = subs.get(d.address.upper())
            if prev is None or ts > prev[1]:
                subs[d.address.upper()] = (d, ts)
    if not subs:
        return None, None
    if len(subs) > 1:
        names = "、".join(sorted(f'{d.name}({d.address})' for d, _ in subs.values()))
        return None, f'"{ident}" 同时匹配到多个设备：{names}。请改用完整名字或 MAC 地址指定，不然每次连的可能不是同一个'
    return next(iter(subs.values()))[0], None


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

    print(f'扫描中，查找名称 "{name_filter}" 的设备（最多等待 {timeout:.0f} 秒）...')
    found: dict = {}

    def _cb(device, adv_data):
        found[device.address] = device

    scanner = BleakScanner(detection_callback=_cb)
    await scanner.start()
    deadline = time.time() + timeout
    target = None
    err = None
    while time.time() < deadline:
        await asyncio.sleep(0.3)
        # 名字完全对上就可以停了，不会再有更好的匹配；只是子串命中的话继续扫完，
        # 万一同名的那个还没广播，提前收工就选错了（见 match_by_name 的说明）
        target, err = match_by_name(name_filter, [(d, 0.0) for d in found.values()])
        if target and target.name and target.name.lower() == name_filter.lower():
            break
    await scanner.stop()

    if err:
        print(f'{err}')
        return None
    if target is not None and target.name and target.name.lower() != name_filter.lower():
        # 见 SharedScanner.find 里同样的提醒：只有一个候选也可能不是你要的那个
        print(f'注意："{name_filter}" 没有同名设备，按包含匹配连的是 {target.name}({target.address})')
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
