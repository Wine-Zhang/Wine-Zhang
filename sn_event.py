#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fiona-SN Reader 极简异常记录 (v1.9).

只记录 WARN / ERROR 两级事件, 供产线在出问题时把单个文件发回分析.
正常读取路径不产生任何记录, 因此同步写盘不会影响读码速度.

日志目录按序探测第一个可写位置 (PLC 会清空 C:\\BarcodeSNLog, 绝不可写在那里):
  C:\\ProgramData\\FionaSNReader\\  ->  <exe目录>\\  ->  %LOCALAPPDATA%\\FionaSNReader\\

单文件上限 2MB, 最多保留 3 个.
"""

import os
import sys
import time
from datetime import datetime

MAX_BYTES = 2 * 1024 * 1024
KEEP_ROTATED = 3

_lock = None
_dir = None
_path = None
_size = 0
_ready = False
_start_ts = time.time()
_counts = {"warn": 0, "error": 0}


def _candidate_dirs():
    dirs = []
    try:
        pf = os.environ.get("PROGRAMDATA")
        if pf:
            dirs.append(os.path.join(pf, "FionaSNReader"))
    except Exception:
        pass
    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        dirs.append(base)
    except Exception:
        pass
    try:
        la = os.environ.get("LOCALAPPDATA")
        if la:
            dirs.append(os.path.join(la, "FionaSNReader"))
    except Exception:
        pass
    seen, out = set(), []
    for d in dirs:
        n = os.path.normcase(os.path.abspath(d))
        if n not in seen:
            seen.add(n)
            out.append(d)
    return out


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".ev_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def init():
    """选择可写的异常记录目录 (幂等). 返回目录, 失败返回 None."""
    global _lock, _dir, _ready
    if _ready:
        return _dir
    import threading
    _lock = threading.Lock()
    with _lock:
        if _ready:
            return _dir
        for d in _candidate_dirs():
            if _writable(d):
                _dir = d
                break
        _ready = True
    return _dir


def dir():
    """当前异常记录目录."""
    return _dir


def file_path():
    """当前异常记录文件完整路径."""
    return _path


def counts():
    return dict(_counts)


def _fmt(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if v is None:
        return "-"
    if isinstance(v, float):
        return "%.2f" % v
    s = str(v)
    s = s.replace("\n", " ").replace("\r", " ")
    return s.replace(" ", "_")


def _write(level, evt, kw):
    """同步追加一行. 任何异常都不得向上抛出."""
    global _path, _size
    try:
        if not _ready:
            init()
        if not _dir:
            return
        with _lock:
            now = datetime.now()
            ts = now.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (now.microsecond // 1000)
            parts = ["%s [%s] %s" % (ts, level.upper(), evt)]
            for k, v in kw.items():
                parts.append("%s=%s" % (k, _fmt(v)))
            line = " ".join(parts) + "\n"
            base = os.path.join(_dir, "events_%s.log" % now.strftime("%Y%m%d"))
            if _path is None or _path != base:
                _path = base
                try:
                    _size = os.path.getsize(_path) if os.path.exists(_path) else 0
                except Exception:
                    _size = 0
            if _size >= MAX_BYTES:
                _rotate(base)
            with open(_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
            _size += len(line.encode("utf-8"))
    except Exception:
        pass


def _rotate(base):
    global _path, _size
    try:
        oldest = "%s.%d" % (base, KEEP_ROTATED)
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(KEEP_ROTATED - 1, 0, -1):
            src = "%s.%d" % (base, i)
            dst = "%s.%d" % (base, i + 1)
            if os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                os.replace(src, dst)
        if os.path.exists(base):
            os.replace(base, base + ".1")
        _size = 0
    except Exception:
        pass


def warn(evt, **kw):
    """记录一条警告 (设备无序列号 / 写盘失败 / 双设备 / 线程重启 ...)."""
    try:
        _counts["warn"] += 1
        _write("WARN", evt, kw)
    except Exception:
        pass


def error(evt, **kw):
    """记录一条错误 (底层异常 / 崩溃)."""
    try:
        _counts["error"] += 1
        _write("ERROR", evt, kw)
    except Exception:
        pass


def note(evt, **kw):
    """记录一条关键节点 (启动 / 退出 / 启动清理)."""
    try:
        _write("INFO", evt, kw)
    except Exception:
        pass