"""场地配置拼出来的命令行对不对——尤其是机位号。

狗场从一台电脑（7 路摄像头 + 6 路蓝牙）拆成两台：

    电脑1  1/2/3 号单间          3 路摄像头 + 6 路蓝牙（9..14）
    电脑2  4/5/6 号单间 + 7 号公共区  4 路摄像头 + 6 路蓝牙（15..20）

**按房间分，不按单双号分**：每只狗两个项圈轮换充电（9/10 都是小白的），
两个都归管着那间房的那台机器。按单双号分的话，一只狗今天有没有视频要看
它今天戴的是单号还是双号项圈——两种分法的蓝牙/摄像头负载完全一样。

要命的是**机位号**：电脑2 只开 4 路，但那 4 路是 cam4..cam7，不是 cam1..cam4。
按位置排的话两台的 cam1 指不同房间，文件传到同一个 NAS 目录就再也分不出来——
跟设备编号按位置排那次一模一样（imu2 里装着 5 号设备的数据，平台按号查狗查错）。

这些用例不连摄像头、不连蓝牙：用一个假 python 把最终命令行打出来，直接比。

    pytest tests/ -q
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


@pytest.fixture(scope="module")
def fake_python(tmp_path_factory):
    """一个只把参数原样打印出来的 python，用来看脚本最终拼了什么。"""
    d = tmp_path_factory.mktemp("fakebin")
    p = d / "python"
    p.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    p.chmod(0o755)
    return str(d)


def cmdline(site: str, fake_python: str, **env) -> list[str]:
    e = {**os.environ, "PATH": f"{fake_python}:{os.environ['PATH']}", "SITE": site, **env}
    out = subprocess.run(["bash", "record_multicam.sh"], cwd=REPO, env=e,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout.splitlines()


def opt(args: list[str], name: str) -> list[str]:
    """--pair 这种可重复参数的全部取值。"""
    return [args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)]


# ── 老配置一个字都不能变 ──────────────────────────────────────────────────


def test_the_old_single_pc_config_is_untouched(fake_python):
    """**老的一台机器录七路那套必须原封不动。**

    加 --camera-id 是为了拆机器，不该让老跑法多出任何一个参数——多一个
    没人看得懂的参数，下次出问题时第一个怀疑的就是它。
    """
    args = cmdline("狗场", fake_python)
    assert opt(args, "--camera") == ["4", "0", "3", "5", "2", "1", "6"]
    assert opt(args, "--camera-id") == [], "老配置不该出现 --camera-id"
    assert opt(args, "--pair") == [
        "cam1:imu9", "cam2:imu11", "cam3:imu13",
        "cam4:imu15", "cam5:imu17", "cam6:imu19",
        "cam7:imu9", "cam7:imu11", "cam7:imu13",
        "cam7:imu15", "cam7:imu17", "cam7:imu19",
    ]
    assert opt(args, "--rotate") == ["cam7:180"]


# ── 拆开之后：机位号要是真号 ──────────────────────────────────────────────


def test_pc1_cameras_are_1_2_3(fake_python):
    args = cmdline("狗场1", fake_python)
    assert opt(args, "--camera-id") == ["1", "2", "3"]
    assert len(opt(args, "--camera")) == 3


def test_pc2_cameras_are_4_5_6_7_not_1_2_3_4(fake_python):
    """**这条是整件事的重点。**

    电脑2 只开 4 路，机位号却必须是 4/5/6/7。按位置排的话它们会叫 cam1..cam4，
    跟电脑1 的 cam1 撞车——两台传到同一个 NAS 日期目录，4 号房间的画面和
    1 号房间的画面文件名一模一样。
    """
    args = cmdline("狗场2", fake_python)
    assert opt(args, "--camera-id") == ["4", "5", "6", "7"]
    assert len(opt(args, "--camera")) == 4


def test_the_two_pcs_do_not_share_any_camera_number(fake_python):
    a = set(opt(cmdline("狗场1", fake_python), "--camera-id"))
    b = set(opt(cmdline("狗场2", fake_python), "--camera-id"))
    assert a & b == set(), f"两台机器的机位号撞了: {a & b}"
    assert a | b == {"1", "2", "3", "4", "5", "6", "7"}, "七个机位号要正好分完"


def test_the_two_pcs_do_not_share_any_device(fake_python):
    """同一个设备被两台机器同时连会互相抢——BLE 一个从机同时只能连一个主机。"""
    a = set(opt(cmdline("狗场1", fake_python), "--imu-label"))
    b = set(opt(cmdline("狗场2", fake_python), "--imu-label"))
    assert a & b == set(), f"两台机器抢同一个设备: {a & b}"
    assert len(a) == len(b) == 6, "每台六个，正好是一个蓝牙适配器扛得住的数"


# ── 配对只指向自己有的机位 ────────────────────────────────────────────────


def test_pairs_only_reference_cameras_this_pc_has(fake_python):
    """配对指向一个这台机器没有的机位，脚本会打警告并丢掉——能跑，但每次
    刷一屏警告，而且说明配置写错了。"""
    for site in ("狗场1", "狗场2"):
        args = cmdline(site, fake_python)
        have = set(opt(args, "--camera-id"))
        for pr in opt(args, "--pair"):
            cam = pr.split(":")[0].removeprefix("cam")
            assert cam in have, f"{site} 的配对 {pr} 指向本机没有的机位"


def test_pairs_only_reference_devices_this_pc_records(fake_python):
    for site in ("狗场1", "狗场2"):
        args = cmdline(site, fake_python)
        have = set(opt(args, "--imu-label"))
        for pr in opt(args, "--pair"):
            assert pr.split(":")[1] in have, f"{site} 的配对 {pr} 指向本机没在录的设备"


def test_only_pc2_has_the_ceiling_camera_and_rotates_it(fake_python):
    """天花板那路倒装，必须转 180；它只在电脑2 上。"""
    assert opt(cmdline("狗场2", fake_python), "--rotate") == ["cam7:180"]
    assert opt(cmdline("狗场1", fake_python), "--rotate") == []


def test_both_pcs_write_the_same_day_directory(fake_python):
    """机位号已经全局唯一了，两台写同一个日期目录，平台看到的还是一个狗场。"""
    a = opt(cmdline("狗场1", fake_python), "--day-suffix")
    b = opt(cmdline("狗场2", fake_python), "--day-suffix")
    assert a == b == ["_gouchang"]


def test_every_device_has_its_own_room_camera_on_the_same_pc(fake_python):
    """**按房间分的全部意义就在这一条。**

    每个设备都要在本机配到一路**房间**摄像头（不算公共区那路）。按单双号分的话
    这条不成立：电脑1 会录着 15/17/19，而那三只的房间摄像头在电脑2 上——
    于是一只狗今天有没有视频，取决于它今天戴的是单号还是双号项圈。

    公共区（cam7）不算数：它看得到所有狗，但一路大场景画面代替不了单间的近景。
    """
    shared = {"cam7"}
    for site in ("狗场1", "狗场2"):
        args = cmdline(site, fake_python)
        paired = {pr.split(":")[1] for pr in opt(args, "--pair")
                  if pr.split(":")[0] not in shared}
        assert paired == set(opt(args, "--imu-label")), \
            f"{site} 这几个设备在本机没有房间摄像头: {set(opt(args, '--imu-label')) - paired}"


def test_both_collars_of_a_dog_live_on_the_same_pc(fake_python):
    """一只狗的两个轮换项圈必须在同一台机器上。

    分到两台的话，换班那天这只狗的数据就跑到另一台去了，而那台没有它的
    房间摄像头——正是按单双号分的老毛病。
    """
    for site in ("狗场1", "狗场2"):
        args = cmdline(site, fake_python)
        dogs = opt(args, "--dog-name")
        assert len(dogs) == 6 and len(set(dogs)) == 3, \
            f"{site} 应该是 3 只狗各 2 个项圈，实际: {dogs}"
        for d in set(dogs):
            assert dogs.count(d) == 2, f"{site} 的 {d} 只有 {dogs.count(d)} 个项圈在本机"


# ── 每个设备都得有人归档 ──────────────────────────────────────────────────


def test_every_device_is_named_in_keep_pairs(fake_python):
    """KEEP_PAIRS 没点到的设备，一份文件都不会传上 NAS——录了一整夜，
    平台上什么都看不到，而且不报错。

    没有对应机位的那三只狗要靠 `:csv` 那几条（只要 CSV、不要视频）。
    """
    import re

    for site, ids in (("狗场1", {"9", "10", "11", "12", "13", "14"}),
                      ("狗场2", {"15", "16", "17", "18", "19", "20"})):
        text = open(os.path.join(REPO, "sites", f"{site}.env"), encoding="utf-8").read()
        keep = re.search(r'KEEP_PAIRS="([^"]*)"', text, re.S).group(1)
        named = {m.group(1) for m in re.finditer(r"imu(\d+)", keep)}
        assert named == ids, f"{site} 的 KEEP_PAIRS 漏了设备: {ids - named}"


def test_every_camera_is_named_in_keep_pairs(fake_python):
    """某路摄像头一份视频都没点到的话，那一路整天的画面都不会上 NAS。"""
    import re

    for site, cams in (("狗场1", {"1", "2", "3"}), ("狗场2", {"4", "5", "6", "7"})):
        text = open(os.path.join(REPO, "sites", f"{site}.env"), encoding="utf-8").read()
        keep = re.search(r'KEEP_PAIRS="([^"]*)"', text, re.S).group(1)
        # `:csv` 结尾的那几条只传 CSV，不算"这路摄像头有视频上去"
        named = {m.group(1) for m in re.finditer(r"cam(\d+)_imu\d+(?!:csv)(?=\s|$)", keep)}
        assert named == cams, f"{site} 的 KEEP_PAIRS 漏了机位: {cams - named}"


# ── 清理脚本 ──────────────────────────────────────────────────────────────


def _cleanup(site: str | None, tmpdir: str) -> str:
    e = dict(os.environ)
    e.pop("SITE", None)
    if site:
        e["SITE"] = site
    out = subprocess.run(["bash", "cleanup_gouchang.sh", "-n", tmpdir],
                         cwd=REPO, env=e, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


def test_cleanup_reads_the_shared_camera_from_the_site(tmp_path):
    """电脑1 没有天花板那路，清理时不该去找 cam7。

    忘了传 `--shared-cam none` 不会删错东西（找不到就跳过），但"清理跑过了"
    这个印象是错的，人不会再回去看。让场地文件自己说。
    """
    assert "公用机位 = （没有）" in _cleanup("狗场1", str(tmp_path))
    assert "公用机位 = 7" in _cleanup("狗场2", str(tmp_path))


def test_cleanup_without_a_site_is_unchanged(tmp_path):
    """老用法（不带 SITE，直接给目录）行为一个字不变，默认还是 cam7。"""
    out = _cleanup(None, str(tmp_path))
    assert "公用机位" not in out


def test_cleanup_accepts_the_ascii_alias(tmp_path):
    """.bat 里只能写 ASCII，所以别名这条路也得通。"""
    assert "公用机位 = 7" in _cleanup("gouchang2", str(tmp_path))


def test_the_old_site_still_defaults_to_cam7(tmp_path):
    """老的 sites/狗场.env 里没有 SHARED_CAM，读不到就保持默认 7。"""
    out = _cleanup("狗场", str(tmp_path))
    assert "公用机位" not in out, "老场地文件没写 SHARED_CAM，不该打印这行"


def test_a_room_camera_is_uploaded_only_once():
    """同一路房间画面不能在 KEEP_PAIRS 里点两次。

    轮换的第二个项圈配的是**同一路摄像头的同一段画面**。点成完整配对的话，
    本地是硬链接不占地方，但 robocopy 走 SMB 每份都是实打实的拷贝——
    一小时 720p 约 500MB，六路就是每小时白传 1.5GB。所以第二个只点 `:csv`。

    公共区那路（cam7）例外：老 sites/狗场.env 就是六个配对都点，
    这里沿用，cleanup 会把它们收成不带 imu 号的那一份。
    """
    import collections
    import re

    for site in ("狗场1", "狗场2"):
        text = open(os.path.join(REPO, "sites", f"{site}.env"), encoding="utf-8").read()
        keep = re.search(r'KEEP_PAIRS="([^"]*)"', text, re.S).group(1)
        vids = collections.Counter(
            m.group(1) for m in re.finditer(r"cam(\d+)_imu\d+(?!:)(?=\s|$)", keep))
        dup = {c: n for c, n in vids.items() if n > 1 and c != "7"}
        assert not dup, f"{site} 这几路房间画面会重复传 NAS: {dup}"


# ── 备忘文档别跟配置走岔 ──────────────────────────────────────────────────


def test_the_per_pc_memo_matches_the_site_config():
    """docs/狗场电脑N.md 是现场照着敲命令的那份，跟场地文件对不上就是在误导人。

    对照表里的机位号、项圈号、MAC 都要在场地文件里真的存在。
    """
    import re

    for site, doc in (("狗场1", "狗场电脑1"), ("狗场2", "狗场电脑2")):
        env = open(os.path.join(REPO, "sites", f"{site}.env"), encoding="utf-8").read()
        md = open(os.path.join(REPO, "docs", f"{doc}.md"), encoding="utf-8").read()

        # 文档里提到的 imuN，场地文件的 DEVICES 里都得有
        dev_block = re.search(r'DEVICES="\n(.*?)"', env, re.S).group(1)
        real_ids = {m.group(1) for m in re.finditer(r"^(\d+)\s", dev_block, re.M)}
        doc_ids = {m.group(1) for m in re.finditer(r"imu(\d+)", md)}
        assert real_ids <= doc_ids, f"{doc}.md 漏了这几个项圈: {real_ids - doc_ids}"
        # 多出来的只能是另一台的（文档里会提一句"那几只在电脑N 上"），不能是
        # 整个狗场都没有的号
        assert doc_ids <= {str(n) for n in range(9, 21)}, \
            f"{doc}.md 提到了狗场没有的项圈: {doc_ids - {str(n) for n in range(9, 21)}}"

        # 文档里贴的 MAC 也得是真的（check_device_worn 那条命令要能直接复制运行）
        for mac in re.findall(r"wit=([0-9A-F]{2}(?::[0-9A-F]{2}){5})", md):
            assert mac in env, f"{doc}.md 里的 {mac} 在 {site}.env 里不存在"

        # 文档里的机位号跟 CAMERAS 表一致
        cam_block = re.search(r'CAMERAS="\n(.*?)"', env, re.S).group(1)
        real_cams = {m.group(1) for m in re.finditer(r"^(\d+)\s", cam_block, re.M)}
        doc_cams = {m.group(1) for m in re.finditer(r"\bcam(\d+)\b", md)}
        assert real_cams <= doc_cams, f"{doc}.md 漏了这几路机位: {real_cams - doc_cams}"
        assert doc_cams <= {"1", "2", "3", "4", "5", "6", "7"}, \
            f"{doc}.md 提到了狗场没有的机位: {doc_cams - {'1','2','3','4','5','6','7'}}"

        # 命令里的 SITE= 不能写成另一台的（复制粘贴最容易错的地方）
        other = "狗场2" if site == "狗场1" else "狗场1"
        for line in md.splitlines():
            if line.strip().startswith(("SITE=", "PREVIEW=", "DRY_RUN=", "CAMS=")):
                assert f"SITE={other}" not in line, f"{doc}.md 里有一条命令写着 {other}: {line}"
