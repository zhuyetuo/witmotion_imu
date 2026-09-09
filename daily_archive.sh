#!/bin/bash
# 每天收工后的固定动作：挑出要用的文件 → 传到 NAS → 删掉暂存。
#
# 之前是手工两步：
#   ./cleanup_resampled_pairs.sh data/multicam_multiimu/2026_9_7 cam1_imu1 cam2_imu2 cam3_imu3 cam1_imu4:csv
#   然后把目录拷到 \\192.168.2.249\ai_data\data_raw
# 这个脚本把它们串起来，挂到 Windows 任务计划里每天自动跑（见 daily_archive.bat）。
#
# 跟手工流程的区别：**原始数据一个都不删**。
#   手工那套是就地删掉没用到的配对，删错了不可逆。这里改成：先建一个暂存目录，
#   把要传的文件放进去（能用硬链接就用硬链接，不占额外空间也不用真拷几十 G），
#   传完 NAS 再把暂存删掉——删的是链接，原始文件原封不动。
#
# 三条硬性要求：
#   1) 绝不碰正在录的数据。默认只处理"昨天"，而且目录里最近 SETTLE_MIN 分钟内
#      有文件被写过就跳过。录制进程和这个脚本互相不知道对方存在，靠"这一天已经
#      不再写了"来保证安全。
#   2) NAS 连不上不算失败。公司 NAS 要走 VPN，断了是常态。连不上就记一笔跳过，
#      本地什么都不动，绝不影响正在跑的录制。
#   3) 失败的天自动补传。每次运行先扫一遍待传的日子挨个重试，VPN 断几天也不用
#      管，通了之后下一次自动全部补上。
#
# 用法（Git Bash，仓库根目录）：
#   ./daily_archive.sh              处理昨天 + 补传之前失败的
#   ./daily_archive.sh 2026_9_7     处理指定日期
#   ./daily_archive.sh --today      处理今天（确定已经收工了才用）
#   ./daily_archive.sh --retry-only 只补传，不处理新的一天
#   DRY_RUN=1 ./daily_archive.sh    只打印要做什么
#   ARCHIVE_ENABLED=0 ./daily_archive.sh   关掉，什么都不做（或建 .archive_disabled 文件）
#
# 可配置（环境变量，或直接改下面的默认值）：
#   KEEP_PAIRS   要传的配对，默认 "cam1_imu1 cam2_imu2 cam3_imu3 cam1_imu4:csv"
#                （:csv / :mp4 后缀表示只要其中一种，跟 cleanup 脚本一个写法）
#   NAS_DEST     NAS 目标目录，默认 //192.168.2.249/ai_data/data_raw
#   NAS_DAY_SUFFIX  NAS 上日期目录的后缀，默认 _<场地名>（2026_9_9_狗场）；
#                设成空串退回不加后缀的老行为
#   DATA_DIR     本地数据根目录，默认 data/multicam_multiimu
#   STAGE_ROOT   暂存目录根，默认 data/_upload
#   SETTLE_MIN   处理"今天"时，最近一次文件改动要超过这么多分钟，默认 20
#   PAST_SETTLE_MIN 处理过去的日期时同上，默认 3（录制早就写到今天的目录去了，
#                只需留几分钟等最后一个整点片段落盘）
set -uo pipefail        # 故意不开 -e：某一步失败要能继续走补传/收尾
cd "$(dirname "${BASH_SOURCE[0]}")"

# 场地配置：跟 record_multicam.sh 读同一份 sites/<名字>.env，KEEP_PAIRS 写在那里。
# 不共用一份的话，录制端改了配对、归档端还按老的挑文件，结果是默默漏传——
# 而且是几天后才从平台上样本数不对发现。
# 场地名：命令行没给就读 sites/.current。
#
# 为什么要这个文件：每日归档是计划任务跑的，没法每次手敲 SITE=狗场；而
# daily_archive.bat 是仓库里的文件，在它里面写死场地名的话，每台机器都要改一次，
# 而且 git pull 每次都冲突。.current 是每台机器自己的一行小文件、不进版本库，
# 配一次就完事，两个脚本都读它。
#
# 设置：  echo 狗场 > sites/.current
#
# tr 去掉 \r：这文件多半是在 Windows 上用记事本建的，带 CRLF，不去掉的话
# 场地名会变成 "狗场\r"，找不到 sites/狗场\r.env，报错还看不出哪儿不对。
SITE="${SITE:-}"
if [ -z "$SITE" ] && [ -f "sites/.current" ]; then
    SITE="$(tr -d '\r\n ' < sites/.current)"
fi
if [ -n "$SITE" ] && [ -f "sites/${SITE}.env" ]; then
    _saved_keep="${KEEP_PAIRS:-}"
    # shellcheck disable=SC1090
    . "sites/${SITE}.env"
    [ -n "$_saved_keep" ] && KEEP_PAIRS="$_saved_keep"
    say_site="（场地：$SITE）"
fi

# 默认值是影棚的形状。狗场是 6 路摄像头 6 只狗，必须靠 SITE= 或显式传 KEEP_PAIRS，
# 否则会漏传一多半。
KEEP_PAIRS="${KEEP_PAIRS:-cam1_imu1 cam2_imu2 cam3_imu3 cam1_imu4:csv}"
NAS_DEST="${NAS_DEST:-//192.168.2.249/ai_data/data_raw}"

# NAS 上的目录名 = 日期 + 这个后缀，比如 2026_9_9_狗场。
#
# 为什么要加：两个场地是两台机器，各自往 NAS 传，日期目录是同一个。文件本身
# 不会互相覆盖（文件名里的 imu 号是全局唯一的，时间戳还精确到毫秒），但一个
# 2026_9_9 里混着两个场地的东西，想确认"狗场今天传全了没有"只能自己按 imu 号
# 挑。加个后缀就一眼看得出来。
#
# 默认取场地名，不用在每个 sites/*.env 里各写一行。
# 万一 Windows 上中文目录名出问题（robocopy 走的是 cygpath 转出来的路径），
# 在 sites/<场地>.env 里写一行 NAS_DAY_SUFFIX="_gouchang" 换成 ASCII 就行。
# 设成空串就退回老行为（不加后缀）。
#
# 注意：这只影响以后传的。NAS 上已经有的那些不带后缀的日期目录原地不动，
# 平台扫描用的是 os.walk（递归），新旧两种目录名都能扫到，不用改后端。
NAS_DAY_SUFFIX="${NAS_DAY_SUFFIX-${SITE:+_$SITE}}"
DATA_DIR="${DATA_DIR:-data/multicam_multiimu}"
STAGE_ROOT="${STAGE_ROOT:-data/_upload}"
SETTLE_MIN="${SETTLE_MIN:-20}"        # 处理"今天"时要求的静默分钟数
PAST_SETTLE_MIN="${PAST_SETTLE_MIN:-3}"  # 处理过去的日期时（录制早就换目录了）
DRY_RUN="${DRY_RUN:-0}"
NAS_PROBE_TIMEOUT="${NAS_PROBE_TIMEOUT:-15}"

# 总开关：这整套自动归档是可选的。不注册任务计划就等于没有它——录制脚本
# 完全不知道这个文件存在，不 import、不调用、不共享任何状态。已经注册了想临时
# 关掉，两种办法：设 ARCHIVE_ENABLED=0，或者在仓库根目录建一个 .archive_disabled
# 文件（不用改任务计划，也不用改代码）。
if [ "${ARCHIVE_ENABLED:-1}" = "0" ] || [ -f .archive_disabled ]; then
    echo "每日归档已关闭（ARCHIVE_ENABLED=0 或存在 .archive_disabled），什么都不做"
    exit 0
fi

LOG_DIR="${IMU_LOG_DIR:-logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_archive_$(date +%Y-%m-%d).log"
say() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG_FILE"; }

ARCHIVED=".archived"        # 传完了
PENDING=".upload_pending"   # 试过但没传成功（NAS 不通/传一半），下次自动重试

# ── 这一天是不是已经录完了 ───────────────────────────────────────────
settled() {
    local dir="$1"
    local day="$2"
    local mins="$SETTLE_MIN"
    # 处理的是过去的日期时只要很短的静默期就够：录制进程按当天日期建目录，
    # 早就写到今天那个目录去了，昨天这个不可能再有人写。留几分钟是等最后一个
    # 整点片段落盘。这样任务计划挂在 00:05 这种刚过零点的时间也能正常跑
    # （用 20 分钟的话，昨天 23:59 写完的文件会把整个流程挡住）
    [ "$day" = "$(date +%Y_%-m_%-d)" ] || mins="$PAST_SETTLE_MIN"
    # 排除 . 开头的标记文件——.upload_pending / .archived 是这个脚本自己写的，
    # 算进去会把"最近有文件改动"判成真，补传永远被自己挡住
    [ -z "$(find "$dir" -type f ! -name '.*' -newermt "-${mins} minutes" -print -quit 2>/dev/null)" ]
}

# ── NAS 是否可达 ─────────────────────────────────────────────────────
# 直接 ls //server/share 在 VPN 断的时候会卡很久（SMB 自己的超时），套一层 timeout
nas_reachable() {
    timeout "$NAS_PROBE_TIMEOUT" ls "$NAS_DEST" >/dev/null 2>&1
}

# ── 某个文件要不要传 ─────────────────────────────────────────────────
# 规则跟 cleanup_resampled_pairs.sh 完全一致：mp4/csv 只留匹配关键字的
# （带 :mp4 / :csv 后缀时只留那一种），其它扩展名的文件一律保留
want_file() {
    local name="$1"
    local ext="${1##*.}"
    local spec key
    case "$ext" in
        mp4|csv) ;;
        *) return 0 ;;
    esac
    for spec in $KEEP_PAIRS; do
        key="${spec%%:*}"
        case "$name" in
            *"$key"*) ;;
            *) continue ;;
        esac
        case "$spec" in
            *:mp4) [ "$ext" = "mp4" ] && return 0 ;;
            *:csv) [ "$ext" = "csv" ] && return 0 ;;
            *)     return 0 ;;
        esac
    done
    return 1
}

# ── 建暂存目录：把要传的文件放进去（优先硬链接）─────────────────────
build_stage() {
    local day="$1"
    local src="$DATA_DIR/$day"
    local stage="$STAGE_ROOT/$day"
    local n=0 skipped=0 linked=0

    rm -rf "$stage" 2>/dev/null
    mkdir -p "$stage" || { say "  ✗ 建不了暂存目录 $stage"; return 1; }

    local f base
    for f in "$src"/*; do
        [ -f "$f" ] || continue
        base="$(basename "$f")"
        case "$base" in .*) continue ;; esac        # 跳过 .archived 这类标记
        if want_file "$base"; then
            # 硬链接：同一个卷上是瞬间完成、不占额外空间的；删暂存时删的是链接，
            # 原始文件不受影响。跨卷或文件系统不支持时退回真拷贝
            if ln "$f" "$stage/$base" 2>/dev/null; then
                linked=$((linked + 1))
            else
                cp "$f" "$stage/$base" || { say "  ✗ 拷贝失败: $base"; return 1; }
            fi
            n=$((n + 1))
        else
            skipped=$((skipped + 1))
        fi
    done

    if [ "$n" -eq 0 ]; then
        say "  ✗ 没有匹配 KEEP_PAIRS 的文件，检查配对关键字是否写对"
        rm -rf "$stage"
        return 1
    fi
    say "  暂存就绪: $n 个文件（其中 $linked 个硬链接，没占额外空间），跳过 $skipped 个不用传的"
    printf '%s' "$n"  > "$stage/.count"
    return 0
}

# ── 传之前先看 NAS 上有没有同名目录 ─────────────────────────────────
# 结果放全局变量 PRECHECK（不能用 echo 返回：函数里的 say 也写 stdout，
# 会跟返回值混在一起，$( ) 捕获到的就不是纯粹的 skip/go/conflict 了）：
#   skip     —— NAS 上已经有这一天，而且要传的文件一个不少，不用重传
#   go       —— 没这个目录，或者只传了一部分（上次 VPN 断在半路），继续传
#   conflict —— 同名文件存在但大小对不上，可能是另一次录制/被改过，
#               绝不覆盖，报出来让人确认（FORCE_OVERWRITE=1 才允许覆盖）
nas_precheck() {
    local day="$1"
    local stage="$STAGE_ROOT/$day"
    local dest="$NAS_DEST/${day}${NAS_DAY_SUFFIX}"

    PRECHECK=go
    if [ ! -d "$dest" ]; then
        return
    fi

    local missing=0 conflict=0 same=0
    local f base dsize lsize
    for f in "$stage"/*; do
        [ -f "$f" ] || continue
        base="$(basename "$f")"
        case "$base" in .*) continue ;; esac
        if [ ! -f "$dest/$base" ]; then
            missing=$((missing + 1))
            continue
        fi
        lsize=$(stat -c %s "$f" 2>/dev/null || echo -1)
        dsize=$(stat -c %s "$dest/$base" 2>/dev/null || echo -2)
        if [ "$lsize" = "$dsize" ]; then
            same=$((same + 1))
        else
            conflict=$((conflict + 1))
            say "  ⚠ 同名但大小不同: $base（本地 $lsize / NAS $dsize）"
        fi
    done

    say "  NAS 上已有这一天的目录：相同 $same 个，缺 $missing 个，冲突 $conflict 个"
    if [ "$conflict" -gt 0 ] && [ "${FORCE_OVERWRITE:-0}" != "1" ]; then
        PRECHECK=conflict
    elif [ "$missing" -eq 0 ] && [ "$conflict" -eq 0 ]; then
        PRECHECK=skip
    else
        PRECHECK=go
    fi
}

# ── 把暂存目录传到 NAS ───────────────────────────────────────────────
sync_stage() {
    local day="$1"
    local stage="$STAGE_ROOT/$day"
    local dest="$NAS_DEST/${day}${NAS_DAY_SUFFIX}"
    local n_local rc
    n_local=$(find "$stage" -maxdepth 1 -type f ! -name '.*' | wc -l)

    if command -v robocopy >/dev/null 2>&1 && command -v cygpath >/dev/null 2>&1; then
        # Windows 上用 robocopy：断点续传、自动重试、多线程，比 cp 靠谱得多。
        # 重试压到 2 次、间隔 5 秒——VPN 真断了就赶紧返回去下次补传，别在这耗着
        local src_win dst_win
        src_win="$(cygpath -w "$stage")"
        dst_win="$(cygpath -w "$dest")"
        robocopy "$src_win" "$dst_win" /E /Z /R:2 /W:5 /MT:8 /NP /NDL /XF ".*" >>"$LOG_FILE" 2>&1
        rc=$?
        # robocopy 退出码不按常规来：0-7 都算成功（1=有文件被复制），>=8 才是真出错
        if [ "$rc" -ge 8 ]; then
            say "  ✗ robocopy 失败，退出码 $rc（详见日志）"
            return 1
        fi
    else
        mkdir -p "$dest" 2>/dev/null || { say "  ✗ 建不了目标目录 $dest"; return 1; }
        find "$stage" -maxdepth 1 -type f ! -name '.*' -exec cp {} "$dest/" \; || return 1
    fi

    # 核对口径要跟暂存一致：暂存里除了 mp4/csv 还可能有别的文件（meta 之类），
    # 只数 mp4/csv 会把数量对不上误判成没传完
    local n_dest
    n_dest=$(find "$dest" -maxdepth 1 -type f ! -name '.*' 2>/dev/null | wc -l)
    if [ "$n_dest" -lt "$n_local" ]; then
        say "  ✗ 核对不过：暂存 $n_local 个，NAS 只有 $n_dest 个，下次继续补"
        return 1
    fi
    say "  ✓ 已传 $n_local 个文件到 $dest（NAS 现有 $n_dest 个）"
    return 0
}

# ── 处理一天：建暂存 → 传 → 删暂存 ──────────────────────────────────
process_day() {
    local day="$1"
    local src="$DATA_DIR/$day"
    local stage="$STAGE_ROOT/$day"

    if [ ! -d "$src" ]; then
        say "  目录不存在: $src（这一天没录到数据？）"
        return 0
    fi
    if [ -f "$src/$ARCHIVED" ]; then
        say "  ✓ 已归档过（$(cat "$src/$ARCHIVED")）"
        return 0
    fi
    if ! settled "$src" "$day"; then
        say "  ⚠ ${SETTLE_MIN} 分钟内还有文件在写，说明还在录，这次不动它"
        return 0
    fi
    if [ "$DRY_RUN" = "1" ]; then
        local n=0 f
        for f in "$src"/*; do
            [ -f "$f" ] && want_file "$(basename "$f")" && n=$((n + 1))
        done
        say "  (DRY_RUN) 会把 $n 个文件放进 $stage 再传到 $NAS_DEST/${day}${NAS_DAY_SUFFIX}，原始数据不动"
        return 0
    fi

    build_stage "$day" || { touch "$src/$PENDING"; return 1; }

    if [ "$NAS_OK" != "1" ]; then
        say "  暂不传（NAS 不可达），暂存留着，等 VPN 通了下次自动传"
        touch "$src/$PENDING"
        return 0
    fi

    nas_precheck "$day"
    case "$PRECHECK" in
        skip)
            say "  ✓ NAS 上已经是完整的一份，不重复传"
            rm -rf "$stage"
            rm -f "$src/$PENDING"
            echo "$(date '+%Y-%m-%d %H:%M:%S') NAS 上已存在完整数据，未重复上传" > "$src/$ARCHIVED"
            return 0
            ;;
        conflict)
            say "  ✗ NAS 上同名文件跟本地大小对不上，为免覆盖已停手"
            say "    要么去 NAS 上把 $day 那个目录处理掉，要么确认无误后 FORCE_OVERWRITE=1 重跑"
            touch "$src/$PENDING"
            return 1
            ;;
    esac

    if sync_stage "$day"; then
        # 传完了才删暂存——删的是硬链接，原始数据一个不少
        rm -rf "$stage"
        rm -f "$src/$PENDING"
        echo "$(date '+%Y-%m-%d %H:%M:%S') 已传到 $NAS_DEST/${day}${NAS_DAY_SUFFIX}" > "$src/$ARCHIVED"
        say "  ✓ 暂存已清理，原始数据原样保留在 $src"
        return 0
    fi
    touch "$src/$PENDING"
    say "  ⚠ 没传成功，暂存保留在 $stage，下次自动重试"
    return 1
}

# ── 主流程 ───────────────────────────────────────────────────────────
RETRY_ONLY=0
DAY=""
case "${1:-}" in
    --retry-only) RETRY_ONLY=1 ;;
    --today)      DAY="$(date +%Y_%-m_%-d)" ;;
    "")           DAY="$(date -d 'yesterday' +%Y_%-m_%-d)" ;;   # 今天还在录，不动
    *)            DAY="$1" ;;
esac

say "=========== 每日归档 ==========="
[ "$DRY_RUN" = "1" ] && say "(DRY_RUN=1，只看不做)"
say "保留配对: $KEEP_PAIRS"

NAS_OK=0
if nas_reachable; then
    NAS_OK=1
    say "NAS 可达: $NAS_DEST"
else
    # 这不算失败：VPN 断了很正常，记一笔，等通了自动补
    say "⚠ NAS 连不上（VPN 断了？）：$NAS_DEST —— 这次只做本地准备，之后自动补传"
fi

failed=0
if [ "$RETRY_ONLY" = "0" ]; then
    say ""
    say "── $DAY ──"
    process_day "$DAY" || failed=1
fi

# 补传：之前试过没成功的日子（有 .upload_pending 标记）
if [ "$NAS_OK" = "1" ]; then
    pending=()
    for d in "$DATA_DIR"/*/; do
        [ -d "$d" ] || continue
        name="$(basename "$d")"
        [ "$name" = "$DAY" ] && continue
        [ -f "$d/$PENDING" ] || continue
        [ -f "$d/$ARCHIVED" ] && continue
        pending+=("$name")
    done
    if [ "${#pending[@]}" -gt 0 ]; then
        say ""
        say "── 补传 ${#pending[@]} 天 ──"
        for name in "${pending[@]}"; do
            say "── $name（补传）──"
            process_day "$name" || failed=1
        done
    fi
fi

# 待传清单：VPN 断了可能过几天才发现，这里明确列出来，一眼看到欠了几天
still=()
for d in "$DATA_DIR"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    [ -f "$d/$PENDING" ] && [ ! -f "$d/$ARCHIVED" ] && still+=("$name")
done

say ""
if [ "${#still[@]}" -gt 0 ]; then
    say "⚠ 还有 ${#still[@]} 天没传到 NAS：${still[*]}"
    say "  处理好 VPN 之后跑一次 ./daily_archive.sh --retry-only 就会全部补上"
    # 写一份清单到数据根目录，不用翻日志也能看到
    printf '%s\n' "最后检查: $(date '+%Y-%m-%d %H:%M:%S')" "还没传到 NAS 的日期:" "${still[@]}" \
        > "$DATA_DIR/待传到NAS.txt"
elif [ "$failed" = "1" ]; then
    say "⚠ 有步骤没做完，原始数据原样保留，下次运行自动重试"
else
    rm -f "$DATA_DIR/待传到NAS.txt"
    say "✓ 全部完成，没有欠传的日期"
fi
# 永远以 0 退出：这个脚本失败不该让任务计划报警，更不该影响别的事情。
# 状态都在日志和 .archived / .upload_pending 标记里
exit 0
