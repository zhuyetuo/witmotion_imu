#!/bin/bash
# 把仓库自带的 python / ffmpeg 常驻到 PATH 上，省得每次手敲 source。
#
#   ./add_to_path.sh              以后每个新开的 Git Bash 都自动带上（推荐）
#   ./add_to_path.sh --windows    连 cmd / PowerShell 也带上（影响整台机器）
#   ./add_to_path.sh --undo       撤销
#   ./add_to_path.sh --status     看现在是什么状态
#
# ── 默认行为为什么只改 Git Bash ───────────────────────────────────────────
#
# 往 ~/.bashrc 里写一行「source 仓库的 activate_env.sh」，只影响 Git Bash，
# 一行文本，随时删得掉，而且**跟着仓库走**：仓库里的 activate_env.sh 改了
# 逻辑，这边自动跟上。
#
# 而且那一行带了存在性判断——仓库哪天挪走或删掉，它安安静静什么都不做，
# 不会让每个新开的终端都报一次错。
#
# ── --windows 那条要知道代价 ──────────────────────────────────────────────
#
# 它把 .tools/miniconda3 写进 Windows 的用户 PATH，于是 cmd、PowerShell、
# VS Code 的终端、双击跑的 .py 全都用这一份 python。代价是：
#
#   1. **这份 python 从此是这台机器的默认 python**。以后装别的东西（比如
#      标注工具、别的项目）时，pip install 会装进采集环境里，互相污染。
#   2. 仓库一挪一删，PATH 就指向空目录——敲 python 报的不再是"找不到"，
#      而是更难看懂的错。
#
# 这台机器只拿来采集、仓库放在 D: 不会动的话，这两条都不要紧，随便开。
# 机器上还跑别的 python 项目，就别开，用默认那条。

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
REPO="$(pwd)"

MARK="# witmotion 采集环境（add_to_path.sh 写的，删掉这两行即可撤销）"
LINE="[ -f \"$REPO/activate_env.sh\" ] && . \"$REPO/activate_env.sh\""
RC="$HOME/.bashrc"

# Windows 用户 PATH 里要加的两段
WIN_DIRS=".tools/miniconda3;.tools/miniconda3/Scripts"

_in_bashrc() { [ -f "$RC" ] && grep -Fq "$MARK" "$RC"; }

_status() {
    echo "仓库: $REPO"
    if _in_bashrc; then
        echo "  Git Bash : 已常驻（$RC 里有那一行）"
    else
        echo "  Git Bash : 没常驻（每次要手敲 source ./activate_env.sh）"
    fi
    if [ -x "$REPO/.tools/miniconda3/python.exe" ]; then
        echo "  python   : $REPO/.tools/miniconda3/python.exe"
    else
        echo "  python   : ✗ 仓库里还没装，先跑 ./setup_windows.sh"
    fi
}

case "${1:-}" in
    --status)
        _status
        exit 0 ;;
    --undo)
        if _in_bashrc; then
            # 删掉标记那一行和它下面那一行
            tmp="$(mktemp)"
            grep -Fv -e "$MARK" -e "$LINE" "$RC" > "$tmp"
            mv "$tmp" "$RC"
            echo "已从 $RC 里删掉。新开的终端生效。"
        else
            echo "$RC 里本来就没有，不用撤销。"
        fi
        echo
        echo "如果之前跑过 --windows，Windows 用户 PATH 里那两条要手动删："
        echo "  设置 → 系统 → 系统信息 → 高级系统设置 → 环境变量 → 用户变量 Path"
        echo "  删掉含 witmotion_imu\\.tools\\miniconda3 的两条"
        exit 0 ;;
    ""|--windows) ;;
    *)
        echo "不认识的参数: $1"
        sed -n '2,9p' "$0"
        exit 1 ;;
esac

if [ ! -x "$REPO/.tools/miniconda3/python.exe" ]; then
    echo "✗ $REPO/.tools/miniconda3 里没有 python，先跑 ./setup_windows.sh"
    exit 1
fi

if _in_bashrc; then
    echo "Git Bash 已经常驻过了，跳过（$RC）"
else
    printf '\n%s\n%s\n' "$MARK" "$LINE" >> "$RC"
    echo "✓ 写进 $RC"
fi

if [ "${1:-}" = "--windows" ]; then
    # setx 有 1024 字符上限，会把超长的 PATH 截断——**截断的是用户 PATH，
    # 截掉的那部分再也回不来**。所以用 PowerShell 读改写注册表，那条路没有
    # 长度限制，也不会碰系统 PATH。
    echo "写 Windows 用户 PATH（cmd / PowerShell 也会生效）..."
    powershell -NoProfile -Command "
        \$repo = '$(cygpath -w "$REPO" 2>/dev/null || echo "$REPO")'
        \$add  = @(\"\$repo\\.tools\\miniconda3\", \"\$repo\\.tools\\miniconda3\\Scripts\")
        \$cur  = [Environment]::GetEnvironmentVariable('Path', 'User')
        \$have = \$cur -split ';' | Where-Object { \$_ -ne '' }
        \$new  = \$add | Where-Object { \$have -notcontains \$_ }
        if (\$new) {
            [Environment]::SetEnvironmentVariable('Path', ((\$have + \$new) -join ';'), 'User')
            Write-Output ('  + ' + (\$new -join [Environment]::NewLine + '  + '))
        } else { Write-Output '  已经在里面了，没改' }
    "
    echo
    echo "⚠ 从现在起这份 python 是这台机器的默认 python。以后在别的项目里"
    echo "  pip install，装的也是这一份——机器上还跑别的 python 项目的话，"
    echo "  跑 ./add_to_path.sh --undo 撤销，改用默认那条（只影响 Git Bash）。"
fi

echo
echo "新开一个终端就生效了。验证："
echo "  python -V        # 应该是 Python 3.13.x"
echo "  python -c \"import bleak, cv2; print('ok')\""
