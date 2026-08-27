#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查询Windows当前"默认"蓝牙适配器的信息（名字/地址/是否支持BLE），帮你确认
bleak（这个仓库所有BLE脚本用的库）实际在用哪一个物理蓝牙适配器——比如你
装了台式机/笔记本自带的蓝牙 + 新买的USB蓝牙适配器，想知道Windows到底默认
用的是哪一个。

只能在Windows上跑（用到WinRT的蓝牙API，其它系统没有这个接口）。

用法:
    python check_bt_adapter.py

如果这个脚本因为找不到 winrt/winsdk 相关模块报错，说明bleak这个版本底层用的
包名不一样，装不上就直接用更简单的办法：去"设备管理器"里把其中一个蓝牙
适配器临时禁用，再跑一遍 wit_ble_live.py --scan 之类的命令，如果还能正常
扫到设备，说明用的就是没被禁用的那个。
"""

import sys


def main():
    if not sys.platform.startswith('win'):
        print('这个脚本只能在Windows上运行（用到WinRT接口）。')
        sys.exit(1)

    try:
        # bleak 在 Windows 上用 winrt（老版本）或 winsdk（新版本）作为底层绑定，
        # 具体是哪个取决于安装的 bleak 版本，这里两个都试一下。
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter
        except ImportError:
            from winsdk.windows.devices.bluetooth import BluetoothAdapter
    except ImportError as e:
        print(f'找不到 WinRT 蓝牙绑定（{e}），这个脚本用不了。')
        print('改用这个办法：去"设备管理器"里把其中一个蓝牙适配器临时禁用，')
        print('再跑一遍 wit_ble_live.py --scan，还能扫到设备就说明用的是没禁用的那个。')
        sys.exit(1)

    import asyncio

    async def query():
        adapter = await BluetoothAdapter.get_default_async()
        if adapter is None:
            print('没有找到默认蓝牙适配器（可能所有蓝牙都被禁用了）。')
            return
        # BluetoothAddress 是一个 uint64，需要转成常见的 XX:XX:XX:XX:XX:XX 格式
        addr = adapter.bluetooth_address
        addr_str = ':'.join(f'{(addr >> (8 * i)) & 0xFF:02X}' for i in range(5, -1, -1))
        print(f'当前Windows默认蓝牙适配器地址: {addr_str}')
        print(f'  是否支持经典蓝牙(Classic):     {adapter.is_classic_supported}')
        print(f'  是否支持低功耗蓝牙(BLE):       {adapter.is_low_energy_supported}')
        print(f'  是否支持中心角色(Central):     {adapter.is_central_role_supported}')
        print(f'  是否支持外围角色(Peripheral):  {adapter.is_peripheral_role_supported}')
        print()
        print('拿上面这个地址去"设备管理器"里对应适配器的属性/详细信息里核对一下')
        print('（一般在"详细信息"选项卡，属性选"设备实例路径"或类似字段里能看到MAC），')
        print('如果跟你的Realtek USB蓝牙适配器的地址一致，说明现在用的就是它。')

    asyncio.run(query())


if __name__ == '__main__':
    main()
