"""自制项圈采样率评估：均匀 31.25 Hz 判正常；0/64 ms 交替、采样率不对、大缝都要报出来；帧解析对得上协议文档的例子。"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import petcollar_rate_monitor as M  # noqa: E402


def _ts(intervals, start=1_793_671_800_000):
    out = [start]
    for d in intervals:
        out.append(out[-1] + d)
    return out


def test_uniform_31_25hz_is_ok():
    # 31.25 Hz = 32 ms 一个，设备 ms 时间戳整数，均匀
    a = M.assess(_ts([32] * 300), [], 31.25)
    assert a["verdict"] == "正常" and abs(a["rate_hz"] - 31.25) < 0.01 and a["within10_pct"] == 100


def test_alternating_long_short_flagged_even_if_average_rate_right():
    # 0 / 64 ms 交替：平均下来还是 31.25 Hz，但一会长一会短
    a = M.assess(_ts([0, 64] * 150), [], 31.25)
    assert abs(a["rate_hz"] - 31.25) < 0.2
    assert a["verdict"] == "有问题" and a["dup"] == 150 and a["alternating"] > 100
    assert any("长短交替" in p for p in a["problems"])


def test_wrong_rate_and_gaps_flagged():
    a = M.assess(_ts([40] * 200), [], 31.25)              # 25 Hz
    assert any("采样率" in p for p in a["problems"])
    b = M.assess(_ts([32] * 100 + [500] + [32] * 100), [], 31.25)
    assert b["gaps"] == 1 and b["gap_ms_max"] == 500


def test_frames_match_protocol_doc_examples():
    # 协议 v2.0 §6.2：2026-11-03 02:15:30 UTC 的校时帧
    f = M.build_time_frame(datetime(2026, 11, 3, 2, 15, 30, tzinfo=timezone.utc))
    assert f.hex(" ") == "55 aa 00 06 00 07 01 1a 0b 03 02 0f 1e 64"
    # §9：确认至 seq 122
    assert M.build_ack_frame(122).hex(" ") == "55 aa 00 0a 00 04 00 00 00 7a 87"
    # §7.2：当前行为帧
    b = bytes.fromhex("55AA0007001 70A02000800000000 6AE943C230040001023102 00 02005B50".replace(" ", ""))
    cmd, payload = M.parse_frame(b)
    d = M.parse_dps(payload)
    assert cmd == 0x07 and d[0x30] == 2 and d[0x31] == 91
    assert M.parse_frame(b[:-1] + bytes([b[-1] ^ 1])) is None      # 校验和不对丢弃


def test_raw_frame_roundtrip():
    def dp(i, v, n):
        return bytes([i, 0x02, 0, n]) + v.to_bytes(n, "big", signed=True)
    body = dp(0x0A, 1_793_671_800_000, 8) + dp(0x17, -123456, 4) + dp(0x18, 2000, 4) + dp(0x19, 1_000_000, 4)
    cmd, payload = M.parse_frame(M.build_frame(0x08, body))
    d = M.parse_dps(payload)
    assert cmd == 0x08 and d[0x0A] == 1_793_671_800_000 and d[0x17] / 1e6 == -0.123456 and d[0x19] / 1e6 == 1.0
