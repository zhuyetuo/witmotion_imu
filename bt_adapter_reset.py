# -*- coding: utf-8 -*-
"""蓝牙栈假死时把适配器禁用再启用（等于拔插一次），不用重启电脑。

现场的病：狗场 2 那台录着录着**整个适配器扫不到任何设备**，重连再怎么写都救不回来，
只有重启电脑才好。这是 Windows 蓝牙栈（WinRT）被反复的扫描开关 / 连接握手折腾到假死，
哪颗适配器先死取决于驱动，跟代码没关系——但代码能做的是：判定它死了之后，自动做人
到现场做的那件事：设备管理器里禁用再启用蓝牙适配器。

怎么判定"死了"（SharedScanner 里）：扫描器重建了 RESTARTS_BEFORE_RESET 次、期间**一条
广播都没收到**（不只是某个设备的，是所有设备、包括环境里别人的手机），而且有设备掉线在
等重连。正常情况下几只狗的项圈就在附近，每秒都有广播；重建三次（约 5 分钟）一条都没有，
不是适配器死了就是所有设备都不在——后一种情况复位一次也没什么代价（几秒钟）。

条件：
  - 只在 Windows 上做（Linux 的 BlueZ 走别的路，这里不管）
  - 要管理员权限（Disable-PnpDevice 需要）。计划任务是 /RL HIGHEST 起的，满足；手动开的
    终端不是管理员的话只打一次提示，告诉人怎么办
  - 环境变量 BT_RESET=0 关掉这个功能（怀疑它帮倒忙时用）
  - 两次复位之间至少隔 RESET_COOLDOWN_S，复位不是免费的：那几秒钟所有设备都断
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

RESTARTS_BEFORE_RESET = 3       # 扫描器连着重建这么多次、期间零广播 → 复位适配器
RESET_COOLDOWN_S = 900.0        # 两次复位最少隔 15 分钟
_PS_TIMEOUT_S = 90.0

# 物理适配器的 InstanceId 以 USB\ 或 PCI\ 开头；蓝牙类下面别的都是它的子设备
# （BTHENUM\、BTH\ ……），禁用那些没用
_PS_FIND = (
    "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
    "Where-Object { $_.InstanceId -like 'USB\\*' -or $_.InstanceId -like 'PCI\\*' } | "
    "Select-Object -ExpandProperty InstanceId"
)


def enabled() -> bool:
    return os.environ.get('BT_RESET', '1').strip().lower() not in ('0', 'no', 'off', 'false', 'n')


def is_windows() -> bool:
    return sys.platform.startswith('win')


def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def available() -> tuple[bool, str]:
    """能不能复位：(能, 不能的原因)。"""
    if not enabled():
        return False, 'BT_RESET=0 关掉了'
    if not is_windows():
        return False, '只在 Windows 上做'
    if not is_admin():
        return False, ('不是管理员，Disable-PnpDevice 做不了。计划任务（install_autostart.bat）起的录制'
                       '是管理员；手动跑的话以管理员身份开 Git Bash 再跑 record_multicam.sh')
    return True, ''


def _ps(cmd: str, timeout: float = _PS_TIMEOUT_S) -> tuple[int, str]:
    p = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', cmd],
                       capture_output=True, text=True, timeout=timeout, encoding='utf-8', errors='replace')
    return p.returncode, (p.stdout or '') + (p.stderr or '')


def find_adapters() -> list[str]:
    code, out = _ps(_PS_FIND, timeout=30.0)
    if code != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def reset_adapter(reason: str = '') -> tuple[bool, str]:
    """禁用 → 等 3 秒 → 启用。同步、阻塞（最多一分半），调用方放到线程里跑。"""
    ok, why = available()
    if not ok:
        return False, why
    ids = find_adapters()
    if not ids:
        return False, '设备管理器里找不到 USB/PCI 的蓝牙适配器（Get-PnpDevice -Class Bluetooth 没结果）'
    print(f'[蓝牙] {reason}：复位适配器（禁用再启用）{ids}')
    lines = []
    for iid in ids:
        q = iid.replace("'", "''")
        lines.append(f"Disable-PnpDevice -InstanceId '{q}' -Confirm:$false -ErrorAction Stop")
    lines.append('Start-Sleep -Seconds 3')
    for iid in ids:
        q = iid.replace("'", "''")
        lines.append(f"Enable-PnpDevice -InstanceId '{q}' -Confirm:$false -ErrorAction Stop")
    code, out = _ps('; '.join(lines))
    if code != 0:
        return False, f'PowerShell 退出码 {code}：{out.strip()[:300]}'
    return True, f'已复位 {len(ids)} 个适配器'


class ResetPolicy:
    """什么时候该复位：纯计数，不碰蓝牙，好测。"""

    def __init__(self, restarts_before: int = RESTARTS_BEFORE_RESET, cooldown_s: float = RESET_COOLDOWN_S):
        self.restarts_before = restarts_before
        self.cooldown_s = cooldown_s
        self.restarts_without_advert = 0
        self.last_reset_at = -1e9
        self.resets = 0

    def on_advert(self) -> None:
        self.restarts_without_advert = 0

    def on_restart(self) -> None:
        self.restarts_without_advert += 1

    def should_reset(self, now: float) -> bool:
        return (self.restarts_without_advert >= self.restarts_before
                and now - self.last_reset_at >= self.cooldown_s)

    def did_reset(self, now: float) -> None:
        self.last_reset_at = now
        self.resets += 1
        self.restarts_without_advert = 0


if __name__ == '__main__':
    ok, msg = reset_adapter('手动')
    print('OK' if ok else '失败', msg)
    print('适配器：', find_adapters())
