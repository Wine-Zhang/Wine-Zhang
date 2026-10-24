#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fiona-SN Reader 核心逻辑层 (v1.9).

包含设备状态检测、HID/WMI 读取、SN 严格校验与产物文件写入.

安全原则: 宁可读不到 (下次重读), 也绝不写入校验不通过的 SN.
"""

import os
import re
import glob
import time
import threading
from datetime import datetime

try:
    import win32com.client
    _HAS_WMI = True
except Exception:
    _HAS_WMI = False

try:
    import pythoncom
    _HAS_PYTHONCOM = True
except Exception:
    _HAS_PYTHONCOM = False

try:
    import hid
    _HAS_HID = True
except Exception:
    _HAS_HID = False

import sn_event as _ev

# --- 常量定义 ---
VID, PID = 0x045E, 0x0B50
OUT_DIR = r"C:\BarcodeSNLog"
# WQL 中反斜杠需转义为 \\ (源码中写四个)
WMI_QUERY = f"SELECT PNPDeviceID FROM Win32_PnPEntity WHERE PNPDeviceID LIKE 'USB\\\\VID_{VID:04X}&PID_{PID:04X}%%'"

# --- SN 严格校验 ---
# 本产品 SN 为 14 位字母数字 (如 0D333DY26133YP). 规格变化只需改这两项.
SN_LEN = 14
SN_CHARS = "0-9A-Za-z"
SN_PATTERN = re.compile(f"^[{SN_CHARS}]{{{SN_LEN}}}$")

# --- 写盘重试 (应对 PLC 并发清空目录) ---
WRITE_RETRY = 5
WRITE_RETRY_GAP = 0.1

# --- WMI 兜底节流 (空闲轮询时避免每 1.5s 都跑 ~100ms 的慢查询) ---
WMI_FALLBACK_INTERVAL = 1.0


def is_valid_sn(sn):
    """严格校验 SN: 非空 / 非 'USB' / 不含 '&' 与 '\\' / 严格匹配 SN_PATTERN."""
    if not sn:
        return False
    sn = sn.strip()
    if sn == "USB":
        return False
    if "&" in sn or "\\" in sn:
        return False
    return bool(SN_PATTERN.match(sn))


def get_dependencies_status():
    return {"WMI": _HAS_WMI, "HID": _HAS_HID}


# --- WMI 连接 (每线程一份, 失败自动重连) ---
_tls = threading.local()
_wmi_lock = threading.Lock()
_last_wmi_check = 0.0


def _ensure_com_apartment():
    """确保当前线程已初始化 COM 套件 (STA).

    WMI 的 'winmgmts:' 是 COM moniker, 必须在 STA 中调用, 否则 GetObject 直接抛
    com_error 'invalid syntax'. 工作线程默认未初始化 COM —— 这是 WMI 兜底路径
    历史上一直静默失效的原因. 幂等: 每线程只初始化一次.
    """
    if not _HAS_PYTHONCOM:
        return False
    if getattr(_tls, "com_inited", False):
        return True
    try:
        pythoncom.CoInitialize()
        _tls.com_inited = True
        return True
    except Exception:
        return False


def _get_wmi():
    _ensure_com_apartment()
    obj = getattr(_tls, "wmi", None)
    if obj is None:
        obj = win32com.client.GetObject("winmgmts:")
        _tls.wmi = obj
    return obj


def _wmi_query_once(info):
    """执行一次 WMI 查询, 返回合法 SN 列表."""
    sns = []
    wmi = _get_wmi()
    for dev in wmi.ExecQuery(WMI_QUERY):
        pid = dev.PNPDeviceID or ""
        info["wmi_rows"] += 1
        parts = pid.split("\\")
        if len(parts) < 2:
            info["bad_count"] += 1
            continue
        sn = parts[-1].strip()
        if not sn:
            info["empty_count"] += 1
            continue
        if not is_valid_sn(sn):
            info["bad_count"] += 1
            if info["bad_value"] is None:
                info["bad_value"] = sn
            continue
        if sn not in sns:
            sns.append(sn)
    return sns


def _query_wmi(force=False):
    """WMI 慢路径. 返回 (sns, info).

    force=True 绕过节流 —— USB 事件驱动的重试/确认场景必须每次真查,
    否则只能等节流自然到期, 命中纯靠运气.
    """
    global _last_wmi_check
    info = {"wmi_rows": 0, "empty_count": 0, "bad_count": 0, "bad_value": None,
            "throttled": False, "error": "", "dur_ms": 0.0}
    if not _HAS_WMI:
        info["error"] = "WMI_unavailable"
        return [], info

    now = time.time()
    with _wmi_lock:
        if not force and (now - _last_wmi_check) < WMI_FALLBACK_INTERVAL:
            info["throttled"] = True
            return [], info
        _last_wmi_check = now

    t0 = time.perf_counter()
    try:
        sns = _wmi_query_once(info)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
        try:
            _tls.wmi = None           # 连接可能失效, 清缓存重试一次
            info["wmi_rows"] = 0
            info["bad_count"] = 0
            sns = _wmi_query_once(info)
        except Exception as e2:
            info["error"] = "%s | retry: %s: %s" % (err, type(e2).__name__, e2)
            sns = []
        _ev.error("wmi_error", err=info["error"])
    info["dur_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    return sns, info


def _query_hid():
    """HID 快路径. 返回 (sns, info)."""
    info = {"hid_count": 0, "empty_count": 0, "bad_count": 0, "bad_value": None,
            "error": "", "dur_ms": 0.0}
    sns = []
    if not _HAS_HID:
        info["error"] = "hidapi_unavailable"
        return sns, info
    t0 = time.perf_counter()
    try:
        devs = hid.enumerate(VID, PID)
    except Exception as e:
        info["error"] = "%s: %s" % (type(e).__name__, e)
        info["dur_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        _ev.error("hid_error", err=info["error"])
        return sns, info
    info["hid_count"] = len(devs)
    for d in devs:
        # 只认厂商自定义集合 0xFF00 的接口
        if d.get("usage_page") != 0xFF00:
            continue
        serial = (d.get("serial_number") or "").strip()
        if not serial:
            # 枚举过程中序列号可能尚未就绪, 属正常过渡态, 不能据此判定"读不出"
            info["empty_count"] += 1
            continue
        if not is_valid_sn(serial):
            info["bad_count"] += 1
            if info["bad_value"] is None:
                info["bad_value"] = serial
            continue
        if serial not in sns:
            sns.append(serial)
    info["dur_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    return sns, info


def enum_sns(wmi_force=False):
    """枚举当前在位的全部合法 SN. 返回 (sns, info).

    优先 HID (毫秒级); HID 无结果时回退 WMI.

    info 关键字段:
      sns        合法 SN 列表
      bad_count  设备在位但序列号"非空且格式非法"的次数 (>0 才算真正读不出)
      empty_count 序列号暂未就绪的次数 (枚举过渡态, 属正常, 不应据此放弃)
      bad_value  首个非法序列号样例值 (便于定位是未烧录还是格式异常)
    """
    info = {"src": "none", "hid_count": 0, "wmi_rows": 0, "empty_count": 0,
            "bad_count": 0, "bad_value": None, "throttled": False, "dur_ms": 0.0}
    t0 = time.perf_counter()
    sns, hinfo = _query_hid()
    info["hid_count"] = hinfo["hid_count"]
    info["empty_count"] += hinfo["empty_count"]
    info["bad_count"] += hinfo["bad_count"]
    if info["bad_value"] is None:
        info["bad_value"] = hinfo["bad_value"]
    src = "HID"
    if not sns:
        wmi_sns, winfo = _query_wmi(force=wmi_force)
        info["wmi_rows"] = winfo["wmi_rows"]
        info["empty_count"] += winfo["empty_count"]
        info["bad_count"] += winfo["bad_count"]
        info["throttled"] = winfo["throttled"]
        if info["bad_value"] is None:
            info["bad_value"] = winfo["bad_value"]
        if wmi_sns:
            sns = wmi_sns
            src = "WMI"
    info["sns"] = sns
    info["src"] = src if sns else "none"
    info["dur_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    return sns, info


def is_location_id(value):
    """判断该值是否为 Windows 生成的位置 ID (而非产品序列号).

    USB 设备插入后分两阶段枚举:
      阶段一  Windows 先用位置 ID 建节点, 例如 5&181816B0&0&1
              此时驱动尚未读到 USB 序列号描述符, 读不到 SN —— 属正常过渡态
      阶段二  约 3 秒后驱动读到描述符, 节点换成真 SN (如 0D3333F26133YP)

    本产品 SN 绝不含 '&' (is_valid_sn 已拒绝), 因此"含 &"即可安全判定为
    位置 ID. 关键: 位置 ID 必须继续等待, 绝不能据此判定"设备读不出".
    """
    return bool(value) and "&" in value


def has_invalid_serial(info):
    """设备在位且序列号"非空、非位置ID、但格式非法" -> 重试不会变好, 可提前放弃.

    排除两类假阳性 (v1.9 产线实测教训):
      1. 序列号暂未就绪 (empty_count) —— 枚举过渡态, 继续等;
      2. Windows 位置 ID (5&181816B0&0&1) —— 约 3 秒后会变成真 SN, 继续等.
    这两类若参与判定, 会在设备刚插上时误报"序列号无效"并放弃, 直接丢产品.
    """
    if info.get("sns"):
        return False
    if info.get("bad_count", 0) <= 0:
        return False
    bad = info.get("bad_value")
    if not bad or is_location_id(bad):
        return False
    return True


def write_sn_file(sn):
    """写入 17 字节规范文件 (SN={sn}), 带有限次重试.

    仅在校验通过时写入; 返回实际路径, 失败返回空串.
    重试原因: PLC 会周期性清空目录, 可能与写盘瞬间冲突.
    """
    if not is_valid_sn(sn):
        _ev.error("write_reject", value=sn)
        return ""
    last_err = ""
    for attempt in range(1, WRITE_RETRY + 1):
        try:
            os.makedirs(OUT_DIR, exist_ok=True)
            stamp = datetime.now().strftime("%Y.%m.%d_%H.%M.%S.%f")[:-3]
            path = os.path.join(OUT_DIR, "%s_%s.txt" % (sn, stamp))
            with open(path, "wb") as f:
                f.write(("SN=" + sn).encode("ascii"))
            if attempt > 1:
                _ev.warn("write_retry_recovered", sn=sn, attempt=attempt)
            return path
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            if attempt < WRITE_RETRY:
                time.sleep(WRITE_RETRY_GAP)
    _ev.error("write_failed", sn=sn, err=last_err, attempts=WRITE_RETRY)
    return ""


def count_pending_files():
    """返回 (目录是否存在, 当前 .txt 数量)."""
    try:
        if not os.path.isdir(OUT_DIR):
            return False, 0
        return True, len(glob.glob(os.path.join(OUT_DIR, "*.txt")))
    except Exception:
        return False, -1


def cleanup_old_files():
    """启动时清空上一次残留的产物文件 (与 PLC 清目录意图一致), 返回删除数量."""
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        files = glob.glob(os.path.join(OUT_DIR, "*.txt"))
        for f in files:
            os.remove(f)
        if files:
            _ev.warn("startup_cleanup", removed=len(files),
                     note="leftover_from_last_run")
        return len(files)
    except Exception as e:
        _ev.error("startup_cleanup_failed", err="%s: %s" % (type(e).__name__, e))
        return 0