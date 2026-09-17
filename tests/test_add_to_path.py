"""把仓库自带的 python 常驻到 PATH 上：装 / 幂等 / 撤销 / 不碰别人的东西。

背景：miniconda 是 /AddToPath=0 装的（跟着仓库走，不接管整台机器的 python），
所以裸敲 `python` 是 command not found。录制脚本自己会找，不受影响，但天天
要手敲 `source ./activate_env.sh` 确实烦。这个脚本把那一行常驻进 ~/.bashrc。

改的是用户主目录里的文件，所以每条用例都用一个假 HOME，绝不碰真的 .bashrc。
"""

from __future__ import annotations

import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "add_to_path.sh")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """假 HOME + 假的 .tools/miniconda3/python.exe（这台机器上没有真的）。"""
    py = os.path.join(REPO, ".tools", "miniconda3", "python.exe")
    made = not os.path.exists(py)
    if made:
        os.makedirs(os.path.dirname(py), exist_ok=True)
        open(py, "w").close()
        os.chmod(py, 0o755)
    yield tmp_path
    if made:
        os.remove(py)


def run(home, *args):
    out = subprocess.run(["bash", SCRIPT, *args], cwd=REPO, text=True,
                         capture_output=True, env={**os.environ, "HOME": str(home)})
    return out


def rc_text(home):
    p = home / ".bashrc"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_installs_a_line_that_sources_activate_env(fake_home):
    assert run(fake_home).returncode == 0
    body = rc_text(fake_home)
    assert "activate_env.sh" in body
    assert REPO in body, "要写绝对路径——.bashrc 跑的时候 cwd 不在仓库里"


def test_the_line_is_guarded_so_a_moved_repo_does_not_break_every_shell(fake_home):
    """仓库哪天挪走/删掉，这一行要安静地什么都不做。

    不加判断的话，每开一个终端都报一次 `No such file or directory`——
    而那时候人多半已经忘了这行是谁加的。
    """
    run(fake_home)
    line = [l for l in rc_text(fake_home).splitlines() if "activate_env.sh" in l][0]
    assert line.startswith("[ -f "), f"没加存在性判断: {line}"


def test_running_twice_does_not_duplicate(fake_home):
    """手滑跑两遍不该在 .bashrc 里留两行。"""
    run(fake_home)
    run(fake_home)
    hits = [l for l in rc_text(fake_home).splitlines() if "activate_env.sh" in l]
    assert len(hits) == 1, hits


def test_undo_removes_it(fake_home):
    run(fake_home)
    assert run(fake_home, "--undo").returncode == 0
    assert "activate_env.sh" not in rc_text(fake_home)


def test_undo_keeps_everything_else_in_bashrc(fake_home):
    """**别人的 .bashrc 内容一个字都不能动。**

    撤销是按标记删那两行，不是把文件清空——这个脚本动的是用户主目录里
    可能攒了很多年的文件。
    """
    (fake_home / ".bashrc").write_text('export FOO=1\nalias ll="ls -l"\n', encoding="utf-8")
    run(fake_home)
    run(fake_home, "--undo")
    body = rc_text(fake_home)
    assert "export FOO=1" in body and 'alias ll="ls -l"' in body


def test_undo_on_a_clean_home_is_not_an_error(fake_home):
    assert run(fake_home, "--undo").returncode == 0


def test_status_says_which_state_it_is_in(fake_home):
    assert "没常驻" in run(fake_home, "--status").stdout
    run(fake_home)
    assert "已常驻" in run(fake_home, "--status").stdout


def test_refuses_when_the_repo_python_is_not_installed(tmp_path):
    """还没跑 setup_windows.sh 就先加 PATH，会加出一条指向空目录的路径——
    之后敲 python 报的错比"找不到"更难懂。
    """
    py = os.path.join(REPO, ".tools", "miniconda3", "python.exe")
    assert not os.path.exists(py), "这条用例要在没有 python.exe 的情况下跑"
    out = run(tmp_path)
    assert out.returncode != 0
    assert "setup_windows.sh" in out.stdout


def test_unknown_flag_is_rejected(fake_home):
    out = run(fake_home, "--oops")
    assert out.returncode != 0
    assert "activate_env.sh" not in rc_text(fake_home), "参数没看懂就不该动文件"
