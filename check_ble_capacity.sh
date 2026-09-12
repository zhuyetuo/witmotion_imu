#!/bin/bash
# 按场地一次性测全部设备（当班 + 备用），看蓝牙适配器扛不扛得住
# ==============================================================
#
# 要回答的问题：「每只狗的两个 IMU 一起采」行不行。
#   狗场 6 只狗 × 2 = 12 个
#   影棚 4 只狗 × 2 = 8 个
#
# 用法:
#   SITE=狗场 ./check_ble_capacity.sh                  # 默认跑 10 分钟
#   SITE=影棚 ./check_ble_capacity.sh --duration 900   # 跑 15 分钟
#   SITE=狗场 ./check_ble_capacity.sh --stagger 0      # 全部同时发起连接
#
# 这个脚本不开摄像头、不写视频、不落 NAS 形状的文件名，测完就走。
# 想知道为什么不直接开一次录制来测，见 check_ble_capacity.py 的开头。
#
# 只连当班那 6/4 个（跟今天的录制一样）对照一下：
#   SITE=狗场 ONLY_ON_DUTY=1 ./check_ble_capacity.sh
# 先跑这个拿到基准，再跑全量，两边的 Hz 一对比就知道多出来的那几个代价多大。

set -euo pipefail
cd "$(dirname "$0")"

SITE="${SITE:-}"
ONLY_ON_DUTY="${ONLY_ON_DUTY:-0}"

if [ -z "$SITE" ] && [ -f "sites/.current" ]; then
    SITE="$(tr -d '\r\n ' < sites/.current)"
fi
if [ -z "$SITE" ]; then
    echo "要指定场地：SITE=狗场 ./check_ble_capacity.sh"
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

if [ "$ONLY_ON_DUTY" = "1" ]; then
    echo "只测当班的 $n 个（ONLY_ON_DUTY=1）——这是今天录制的实际负载，拿它当基准"
else
    echo "测全部 $n 个（当班 + 备用）"
    # 备用那组没配就只测到当班，容易让人以为「12 个没问题」，其实只测了 6 个
    if [ -n "${DEVICES:-}" ] && [ -z "${DEVICES_STANDBY:-}" ]; then
        echo "⚠ 这个场地没有配 DEVICES_STANDBY，所以只测到当班这几个。"
        echo "  要测「一只狗两个一起采」，先把备用那组填进 sites/${SITE}.env 的 DEVICES_STANDBY。"
    elif [ -z "${DEVICES:-}" ] && [ -z "${IMUS_STANDBY:-}" ]; then
        echo "⚠ 这个场地没有配 IMUS_STANDBY，所以只测到当班这几个。"
        echo "  要测「一只狗两个一起采」，先把备用那组填进 sites/${SITE}.env 的 IMUS_STANDBY。"
    fi
fi
echo

exec python check_ble_capacity.py "${imu_args[@]}" "${label_args[@]}" "$@"
