#!/bin/bash
# 狗场专用清理：一次录制完，目录里除了配对好的 {base}_camX_imuY_raw.mp4/.csv
# （样本平台就认这批），还会剩下一堆中间产物。这个脚本把那批中间产物删掉。
#
# 默认删这三类：
#   {base}_camN_raw.mp4     每路摄像头未配对的原始视频
#   {base}.csv              合并后的总 IMU 流水
#   {base}_meta.csv         录制元信息
#
# 不删 {base}_imuM_raw.csv（每个设备的未裁剪原始流水）。配对出来的
# {base}_camN_imuM_raw.csv 是按视频起止裁过的，删了这份就再也拿不回整段数据，
# 所以要删得显式加 --with-imu-raw。
#
# 用法:
#   ./cleanup_gouchang.sh [-n] [-y] [--with-imu-raw] <目录> [<目录> ...]
#
#   -n / --dry-run    只列清单，不删
#   -y / --yes        跳过确认直接删（自动化用；手动跑别加）
#   --with-imu-raw    连 {base}_imuM_raw.csv 一起删
#
# 安全检查：{base}_camN_raw.mp4 之所以能删，是因为 {base}_camN_imuM_raw.mp4
# 是它的硬链接（同一份数据，删一个文件名不掉数据）。所以每删一路之前都会先确认
# 这一路确实有配对文件、而且指向同一个 inode（硬链接失败退回 copy 时按大小比）。
# 没通过就跳过那一路并提示，绝不盲删。
#
# 例子:
#   ./cleanup_gouchang.sh -n ~/witmotion_imu/data/2026_9_11_gouchang
#   ./cleanup_gouchang.sh ~/witmotion_imu/data/2026_9_11_gouchang

set -euo pipefail

ASSUME_YES=0
DRY_RUN=0
WITH_IMU_RAW=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1; shift ;;
        -n|--dry-run) DRY_RUN=1; shift ;;
        --with-imu-raw) WITH_IMU_RAW=1; shift ;;
        -h|--help)
            sed -n '2,28p' "$0"
            exit 0 ;;
        --) shift; break ;;
        -*) echo "未知选项: $1"; exit 1 ;;
        *) break ;;
    esac
done

if [ "$#" -lt 1 ]; then
    echo "用法: $0 [-n] [-y] [--with-imu-raw] <目录> [<目录> ...]"
    exit 1
fi

# 同一份数据？优先比 inode（硬链接），退回比字节数（当时 os.link 失败走了 copy）
same_data() {
    local a="$1" b="$2"
    [ -f "$a" ] && [ -f "$b" ] || return 1
    local ia ib
    ia=$(stat -c '%i' "$a" 2>/dev/null || stat -f '%i' "$a")
    ib=$(stat -c '%i' "$b" 2>/dev/null || stat -f '%i' "$b")
    if [ "$ia" = "$ib" ]; then
        return 0
    fi
    local sa sb
    sa=$(stat -c '%s' "$a" 2>/dev/null || stat -f '%z' "$a")
    sb=$(stat -c '%s' "$b" 2>/dev/null || stat -f '%z' "$b")
    [ "$sa" = "$sb" ] && [ "$sa" != "0" ]
}

to_delete=()
warnings=()

for DIR in "$@"; do
    if [ ! -d "$DIR" ]; then
        echo "目录不存在，跳过: $DIR"
        continue
    fi

    # 1) 未配对的 {base}_camN_raw.mp4：确认这一路有硬链接过去的配对文件才删
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        stem="$(basename "${f%.mp4}")"          # {base}_camN_raw
        prefix="${stem%_raw}"                   # {base}_camN
        paired_ok=0
        while IFS= read -r p; do
            [ -n "$p" ] || continue
            if same_data "$f" "$p"; then paired_ok=1; break; fi
        done < <(find "$DIR" -maxdepth 1 -type f -name "${prefix}_imu*_raw.mp4" 2>/dev/null)
        if [ "$paired_ok" = "1" ]; then
            to_delete+=("$f")
        else
            warnings+=("保留 $f —— 没找到与它同一份数据的 ${prefix}_imu*_raw.mp4，删了就真没了")
        fi
    done < <(find "$DIR" -maxdepth 1 -type f -regex '.*_cam[0-9]+_raw\.mp4$' 2>/dev/null)

    # 2) 合并流水 {base}.csv 和元信息 {base}_meta.csv
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        to_delete+=("$f")
    done < <(find "$DIR" -maxdepth 1 -type f -name '*_meta.csv' 2>/dev/null)

    while IFS= read -r f; do
        [ -n "$f" ] || continue
        b="$(basename "${f%.csv}")"
        # 只要 {base}.csv 本身：带 _cam/_imu/_meta/_resampled 段的都不是它
        case "$b" in
            *_cam[0-9]*|*_imu[0-9]*|*_meta|*_resampled*) continue ;;
        esac
        to_delete+=("$f")
    done < <(find "$DIR" -maxdepth 1 -type f -name '*.csv' 2>/dev/null)

    # 3) 可选：每个设备未裁剪的原始流水
    if [ "$WITH_IMU_RAW" = "1" ]; then
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            to_delete+=("$f")
        done < <(find "$DIR" -maxdepth 1 -type f -regex '.*_imu[0-9]+_raw\.csv$' \
                     ! -regex '.*_cam[0-9]+_imu[0-9]+_raw\.csv$' 2>/dev/null)
    fi
done

if [ "${#warnings[@]}" -gt 0 ]; then
    echo "⚠ 以下文件没通过安全检查，不会删："
    printf '  %s\n' "${warnings[@]}"
    echo
fi

if [ "${#to_delete[@]}" -eq 0 ]; then
    echo "没有需要删除的文件。"
    exit 0
fi

echo "以下 ${#to_delete[@]} 个文件将被删除:"
printf '  %s\n' "${to_delete[@]}"
echo
if [ "$WITH_IMU_RAW" != "1" ]; then
    echo "(未裁剪的 {base}_imuM_raw.csv 已保留，要一起删加 --with-imu-raw)"
    echo
fi

if [ "$DRY_RUN" = "1" ]; then
    echo "(-n 试运行，没有删除任何文件)"
    exit 0
fi

if [ "$ASSUME_YES" = "1" ]; then
    echo "(-y 已指定，直接删除)"
else
    read -r -p "确认删除以上文件？(y/N) " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "已取消，未删除任何文件。"
        exit 0
    fi
fi

rm -v -- "${to_delete[@]}"
echo
echo "删除完成，共删除 ${#to_delete[@]} 个文件。"
