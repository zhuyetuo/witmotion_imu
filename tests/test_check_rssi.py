"""信号强度调试：统计对不对、按场地能不能把全部项圈带上。"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import check_rssi as r  # noqa: E402


def test_summary_percentiles():
    s = r.summarize([-80, -70, -60, -75, -65])
    assert s["n"] == 5 and s["median"] == -70 and s["min"] == -80 and s["max"] == -60
    assert r.summarize([]) is None


def test_recent_window_drops_old_readings():
    st = r.RssiStat("aa:bb:cc:dd:ee:ff", "imu9")
    st.add(0.0, -90)
    st.add(20.0, -60)
    assert st.recent_median() == -60, "10 秒前的读数不该算进最近中位数"
    assert st.all == [-90, -60]


def test_render_marks_unheard_collars():
    a, b = r.RssiStat("AA:BB:CC:DD:EE:01", "imu9/dongde"), r.RssiStat("AA:BB:CC:DD:EE:02", "imu10/dongde")
    a.add(100.0, -66)
    out = r.render([a, b], 101.0, 90.0, "户外")
    assert "-66" in out.replace(" ", "") or "66" in out
    assert "没听到" in out.splitlines()[-1]


@pytest.fixture(scope="module")
def fake_python(tmp_path_factory):
    d = tmp_path_factory.mktemp("fakebin")
    p = d / "python"
    p.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    p.chmod(0o755)
    return str(d)


@pytest.mark.parametrize("site,n", [("佛山博朗旺_狗场3", 12), ("bolangwang1", 6), ("佛山博朗旺_狗场2", 6)])
def test_site_brings_all_collars(fake_python, site, n):
    e = {**os.environ, "PATH": f"{fake_python}:{os.environ['PATH']}", "SITE": site}
    out = subprocess.run(["bash", "check_rssi.sh", "--note", "户外"], cwd=REPO, env=e,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    args = out.stdout.splitlines()
    assert args.count("--imu") == n and args.count("--label") == n
    assert "check_rssi.py" in args and args[-2:] == ["--note", "户外"]
