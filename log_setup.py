"""
录制脚本的日志留存。

之前所有输出都是 print 到终端：关掉窗口、或者 SSH 断了，几天的运行痕迹就没了。
BLE"再也连不上"这类问题恰恰要看很久之前发生过什么（什么时候断的、等了多久、
扫描器重建过几次），没有日志就只能靠猜。

做法是把 stdout/stderr 接一个"三通"：照样打到终端，同时逐行加上时间戳写进
logs/<前缀>_<日期>.log。跨天自动换新文件（录制是 24 小时连着跑的），旧文件
按天数清理。代码里几百个 print 一个都不用改。
"""

import atexit
import os
import re
import sys
import threading
from datetime import datetime, timedelta

DEFAULT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
_FNAME_RE = re.compile(r'^(?P<prefix>.+)_(?P<date>\d{4}-\d{2}-\d{2})\.log$')


class _Tee:
    """写终端 + 写当天的日志文件，每行前面加时间戳。跨天自动换文件。"""

    def __init__(self, stream, log_dir: str, prefix: str, lock: threading.Lock):
        self._stream = stream
        self._log_dir = log_dir
        self._prefix = prefix
        self._lock = lock
        self._fh = None
        self._day = None
        self._at_line_start = True

    def _file(self):
        today = datetime.now().strftime('%Y-%m-%d')
        if self._fh is None or self._day != today:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
            path = os.path.join(self._log_dir, f'{self._prefix}_{today}.log')
            # buffering=1 = 行缓冲：进程被 kill 掉也不会丢最后几行
            self._fh = open(path, 'a', buffering=1, encoding='utf-8', errors='replace')
            self._day = today
        return self._fh

    def write(self, text):
        self._stream.write(text)
        if not text:
            return len(text)
        with self._lock:
            try:
                fh = self._file()
                stamp = datetime.now().strftime('%H:%M:%S')
                # 逐行加时间戳；一次 write 里可能有多行，也可能只是半行（print 的
                # 结尾换行是单独一次 write），所以要记住"是不是正好在行首"
                for part in text.splitlines(keepends=True):
                    if self._at_line_start and part.strip():
                        fh.write(f'{stamp} {part}')
                    else:
                        fh.write(part)
                    self._at_line_start = part.endswith('\n')
            except Exception:
                # 日志写不进去也不能影响录制本身
                pass
        return len(text)

    def flush(self):
        self._stream.flush()
        if self._fh is not None:
            try:
                self._fh.flush()
            except Exception:
                pass

    def isatty(self):
        return getattr(self._stream, 'isatty', lambda: False)()

    @property
    def encoding(self):
        return getattr(self._stream, 'encoding', 'utf-8')

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def cleanup_old(log_dir: str, keep_days: int) -> None:
    """删掉超过 keep_days 天的日志，别让它无限长下去。"""
    if keep_days <= 0 or not os.path.isdir(log_dir):
        return
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime('%Y-%m-%d')
    for fn in os.listdir(log_dir):
        m = _FNAME_RE.match(fn)
        if m and m.group('date') < cutoff:
            try:
                os.remove(os.path.join(log_dir, fn))
            except OSError:
                pass


def setup(prefix: str = 'record', log_dir: str = None, keep_days: int = 30) -> str:
    """
    在 main() 一开始调一次。返回当天日志文件的路径。
    环境变量 IMU_LOG_DIR / IMU_LOG_KEEP_DAYS 可以覆盖。
    """
    log_dir = log_dir or os.environ.get('IMU_LOG_DIR') or DEFAULT_LOG_DIR
    keep_days = int(os.environ.get('IMU_LOG_KEEP_DAYS', keep_days))
    os.makedirs(log_dir, exist_ok=True)
    cleanup_old(log_dir, keep_days)

    lock = threading.Lock()          # BLE 在独立线程里跑，两个流共用一把锁
    out = _Tee(sys.stdout, log_dir, prefix, lock)
    err = _Tee(sys.stderr, log_dir, prefix, lock)
    sys.stdout, sys.stderr = out, err

    @atexit.register
    def _close():
        out.flush()
        err.flush()
        out.close()
        err.close()

    return os.path.join(log_dir, f"{prefix}_{datetime.now():%Y-%m-%d}.log")
