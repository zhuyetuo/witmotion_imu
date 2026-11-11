#!/bin/bash
# 蓝牙信号强度调试：按场地把全部项圈列出来，实时看这台电脑收到的信号强度（RSSI）
# ==========================================================================
#
# 用法:
#   SITE=佛山博朗旺_狗场3 ./check_rssi.sh --note 户外        # 一直跑，Ctrl-C 停止并出汇总
#   SITE=佛山博朗旺_狗场1 ./check_rssi.sh --note 室内 --duration 300
#
# ⚠ 测之前三台电脑的录制都先停掉（按 q）：项圈被任何一台连着就不广播，谁都测不到。
# 每条读数记进 logs/rssi/rssi_<电脑名>_<日期>.csv。说明见 check_rssi.py 开头。

set -euo pipefail
cd "$(dirname "$0")"

# 仓库自带的 python 挂到 PATH 上：miniconda 是 /AddToPath=0 装的，新开的
# 终端里 python 不在 PATH 上，下面直接 exec python 会 command not found
# —— 看起来像环境没装好，其实装好了。见 activate_env.sh。
# shellcheck disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/activate_env.sh"

SITE="${SITE:-}"
ONLY_ON_DUTY="0"

if [ -z "$SITE" ] && [ -f "sites/.current" ]; then
    SITE="$(tr -d '\r\n ' < sites/.current)"
fi
if [ -z "$SITE" ]; then
    echo "要指定场地：SITE=狗场 ./check_rssi.sh"
    echo "现有的场地："
    ls sites/*.env 2>/dev/null | sed 's|^|  |' || echo "  （一个都没有）"
    exit 1
fi

# SITE 可以写 ASCII 别名（gouchang / yingpeng），跟 record_multicam.sh 一套规则：
# 别名登记在场地文件自己的 SITE_ALIAS= 里，现扫不写死对照表
if [ ! -f "sites/${SITE}.env" ]; then
    for _f in sites/*.env; do
        [ -f "$_f" ] || continue
        if grep -q "^[[:space:]]*SITE_ALIAS=[\"']\?${SITE}[\"']\?[[:space:]]*\$" "$_f"; then
            SITE="$(basename "$_f" .env)"
            break
        fi
    done
fi
site_file="sites/${SITE}.env"
if [ ! -f "$site_file" ]; then
    echo "找不到场地配置 $site_file"
    exit 1
fi
# shellcheck disable=SC1090
. "$site_file"
echo "场地：$SITE（$site_file）"

imu_args=()
label_args=()

# DEVICES 表（狗场用）：一行「编号 MAC 狗名」。标签用真实编号 imu9、imu10…，
# 报表里一眼能看出是哪只狗的哪一个。
_add_table() {
    local block="$1" tag="$2"
    [ -n "$block" ] || return 0
    while IFS= read -r _line; do
        _line="${_line%%#*}"
        read -ra _f <<< "$_line"
        [ ${#_f[@]} -eq 0 ] && continue
        if [ ${#_f[@]} -ne 3 ]; then
            echo "${tag} 这一行应该是「编号 MAC 狗名」三列，收到 ${#_f[@]} 列: $_line"
            exit 1
        fi
        imu_args+=(--imu "wit=${_f[1]}")
        label_args+=(--label "imu${_f[0]#imu}/${_f[2]}")
    done <<< "$block"
}

# IMUS 扁平串（影棚用）：wit=WT1 wit=WT5 …，配 DOG_NAMES。
# 影棚没有 IMU_IDS（真实编号还没确认，见 sites/影棚.env 里的说明），
# 所以标签只能用设备名本身——这对容量测试够了，它测的是蓝牙不是身份。
_add_flat() {
    local specs="$1" names="$2"
    [ -n "$specs" ] || return 0
    read -ra _names <<< "$names"
    local i=0
    for _spec in $specs; do
        imu_args+=(--imu "$_spec")
        local _nm="${_names[$i]:-}"
        label_args+=(--label "${_spec#wit=}${_nm:+/$_nm}")
        i=$((i + 1))
    done
}

if [ -n "${DEVICES:-}" ]; then
    _add_table "${DEVICES}" "DEVICES"
    if [ "$ONLY_ON_DUTY" != "1" ]; then
        _add_table "${DEVICES_STANDBY:-}" "DEVICES_STANDBY"
    fi
else
    _add_flat "${IMUS:-}" "${DOG_NAMES:-}"
    if [ "$ONLY_ON_DUTY" != "1" ]; then
        _add_flat "${IMUS_STANDBY:-}" "${DOG_NAMES_STANDBY:-}"
    fi
fi

n=$(( ${#imu_args[@]} / 2 ))
if [ "$n" -eq 0 ]; then
    echo "场地配置里一个设备都没读到（DEVICES 或 IMUS 都是空的）"
    exit 1
fi
echo "看这 $n 个项圈的信号强度"
echo

exec python check_rssi.py "${imu_args[@]}" "${label_args[@]}" "$@"
