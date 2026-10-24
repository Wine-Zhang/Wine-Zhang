#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fiona-SN Reader-v1.9: 045E:0B50 USB 事件驱动自动读取.

插入设备 (WM_DEVICECHANGE) -> 自动读取 SN -> 严格校验 -> 二次确认 -> 写 17B 文件;
拔出设备 -> 即时重置状态.

可靠性设计:
  - 常驻单工作线程 + 任务队列 (不反复建线程, 规避线程/COM 句柄累积);
  - 结果经队列由主线程统一更新 UI (无跨线程 tkinter 调用);
  - USB 事件为主触发, 低频兜底轮询 (1.5s) 弥补偶发丢事件;
  - 写入前二次确认 SN 稳定, 且严格校验格式, 绝不写入错误 SN;
  - USB 监听线程与工作线程均有 watchdog, 异常退出自动重启;
  - 重试采用时间窗口 (2s) 而非固定次数, 在不阻塞后续产品检测的前提下
    等待设备就绪;
  - 确认瞬间设备已拔走不再丢弃: 短暂复核后仍为空则照写 (快速插拔不丢产品).

异常记录: 只把 WARN/ERROR 写入 events_YYYYMMDD.log (正常读取不产生记录).

分层: sn_reader_logic (业务) / sn_reader_usb (USB 事件) / sn_event (异常记录) / 本文件 (UI 与控制).
"""

import os
import sys
import time
import queue
import threading
import traceback
import tkinter as tk
from tkinter import scrolledtext, messagebox
from datetime import datetime

import sn_reader_logic as L
import sn_reader_usb
import sn_event as _ev

try:
    import win32api
    import win32event
    import winerror        # 标准库; 注意不是 pywin32 的 win32error (不存在)
    _HAS_WIN32_MUTEX = True
except Exception:
    _HAS_WIN32_MUTEX = False

# --- 运行参数 ---
SAFETY_POLL = 1.5          # 兜底轮询间隔 (秒)
DEBOUNCE_MS = 150          # USB 事件去抖 (毫秒)
POLL_MS = 100              # 主线程轮询间隔 (毫秒)
STABLE_CHECK_GAP = 0.05    # 写入前二次确认的初始间隔 (秒)
RETRY_WINDOW_MAX = 2.0     # 事件触发的重试总时间窗口 (秒). 2.0s 足以覆盖大部分
                           # 设备就绪时间, 同时把 worker 阻塞时间控制在 v1.8
                           # 同一数量级, 避免对后续产品检测造成连锁延迟.
EVENT_RETRY_GAP = 0.1      # 重试间隔 (秒); 每轮约 0.1+0.07(WMI)=0.17s
NOSN_PERSIST_CHECKS = 6    # 同一个非法序列号连续出现多少次才判定"真的读不出"
CONFIRM_GONE_RETRY = 2     # 确认时设备"已消失"的复核次数
CONFIRM_GONE_GAP = 0.02    # 复核间隔 (秒)
# 产线实测: 设备两阶段枚举本身约需 2.8s (Windows/固件固有, 代码无法消除),
# 端到端 2.6~3.2s 属正常范围. 故"偏慢"阈值放宽到 5s, 真正异常才提示.
SLOW_READ_MS = 5000        # 端到端超过该值在界面提示偏慢
NO_SN_ALERT_GAP = 30       # "设备无有效序列号"提示的限频 (秒)
VERSION = "1.9"


class TrayApp:
    def __init__(self, root):
        _ev.init()

        self.root = root
        root.title("Fiona-SN Reader-v%s (045E:0B50)" % VERSION)
        root.geometry("470x400")
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        # 线程与队列
        self.task_queue = queue.Queue()
        self.event_queue = queue.Queue()
        self.result_queue = queue.Queue()
        self._stop = threading.Event()
        self._debounce_job = None
        self.watcher = None
        self.worker = None

        # 状态 (仅工作线程访问: 当前记录的在位 SN)
        self._present_sn = None
        self._display_sn = None        # 主线程维护的界面展示 SN
        self._dual_sent = False         # 是否已就"进入冲突"状态通知过界面
        self._usb_n = 0
        self._last_no_sn_alert = 0.0

        # 计数器
        self.read_cnt = 0
        self.ok_cnt = 0
        self.fail_cnt = 0
        self.nosn_cnt = 0
        self._last_read_ms = 0.0

        self._build_ui()

        stale_exist, stale_n = L.count_pending_files()
        if stale_n > 0:
            _ev.warn("stale_files", n=stale_n, note="last_run_plc_may_not_have_cleared")
        cleaned = L.cleanup_old_files()
        if cleaned:
            self.add_log("启动清理: 删除 %d 个残留产物文件" % cleaned)

        self._start_usb_watcher()
        self._start_worker()

        self.root.after(POLL_MS, self._poll)
        self.root.after(200, lambda: self.task_queue.put(("reconcile", "first")))
        self.root.after(500, self._refresh_status_line)

    # --- UI 构建 ---

    def _build_ui(self):
        top = tk.Frame(self.root)
        top.pack(fill=tk.X, padx=12, pady=8)
        self.dot = tk.Label(top, text="●", font=("Arial", 18), fg="gray")
        self.dot.pack(side=tk.LEFT)
        self.lbl_state = tk.Label(top, text="等待插入 045E:0B50 ...",
                                  font=("Microsoft YaHei", 11))
        self.lbl_state.pack(side=tk.LEFT, padx=6)

        self.sn_var = tk.StringVar(value="—")
        tk.Label(self.root, textvariable=self.sn_var, font=("Consolas", 20, "bold"),
                 fg="#0a7d2c").pack(pady=6)

        self.stats_var = tk.StringVar(value="读取: 0  写成功: 0  写失败: 0  无序列号: 0")
        self.stats_label = tk.Label(self.root, textvariable=self.stats_var,
                                    font=("Consolas", 10), fg="#333")
        self.stats_label.pack(fill=tk.X, padx=12, pady=(0, 4))

        bar = tk.Frame(self.root)
        bar.pack(fill=tk.X, padx=12)
        tk.Button(bar, text="复制SN", command=self.copy_sn).pack(side=tk.LEFT, padx=6)
        tk.Button(bar, text="最小化", command=self.hide_to_tray).pack(side=tk.LEFT)
        tk.Button(bar, text="退出", command=self.quit_all).pack(side=tk.RIGHT)

        self.usb_var = tk.StringVar(value="USB 监听: 启动中 ...")
        tk.Label(self.root, textvariable=self.usb_var, font=("Consolas", 9),
                 fg="#555", anchor="w").pack(fill=tk.X, padx=12, pady=(4, 0))

        self.lastread_var = tk.StringVar(value="上次读取耗时: —")
        tk.Label(self.root, textvariable=self.lastread_var, font=("Consolas", 9),
                 fg="#555", anchor="w").pack(fill=tk.X, padx=12)

        self.log = scrolledtext.ScrolledText(self.root, height=7, state="disabled",
                                             font=("Consolas", 10))
        self.log.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

    def _refresh_status_line(self):
        try:
            self.lastread_var.set("上次读取耗时: %s" % (
                ("%.0f ms" % self._last_read_ms) if self._last_read_ms else "—"))
            self.root.after(500, self._refresh_status_line)
        except Exception:
            pass

    # --- USB 监听 ---

    def _start_usb_watcher(self):
        if not sn_reader_usb.is_available():
            self.usb_var.set("USB 监听: 不可用 (缺少 pywin32)")
            _ev.error("watcher_unavailable", reason="pywin32_missing")
            return
        try:
            self.watcher = sn_reader_usb.UsbEventWatcher(self._on_usb_event)
            if self.watcher.start():
                self.usb_var.set("USB 监听: 已启动")
            else:
                self.usb_var.set("USB 监听: 启动失败")
                _ev.error("watcher_start_failed")
        except Exception as e:
            self.usb_var.set("USB 监听: 异常")
            _ev.error("watcher_exception", err="%s: %s" % (type(e).__name__, e))

    def _on_usb_event(self):
        """在 USB 监听线程中被调用, 仅入队."""
        self.event_queue.put(time.time())

    def _check_watcher(self):
        """USB 监听线程异常退出则自动重启."""
        if self.watcher is not None and not self.watcher.is_alive():
            _ev.warn("watcher_restart")
            self.add_log("WARN: USB 监听已停止, 正在重启 ...", "warn")
            try:
                self.watcher = sn_reader_usb.UsbEventWatcher(self._on_usb_event)
                self.usb_var.set("USB 监听: 已启动" if self.watcher.start()
                                 else "USB 监听: 重启失败")
            except Exception as e:
                self.usb_var.set("USB 监听: 异常")
                _ev.error("watcher_restart_failed",
                          err="%s: %s" % (type(e).__name__, e))

    # --- 工作线程 ---

    def _start_worker(self):
        self.worker = threading.Thread(target=self._worker_loop, daemon=True,
                                       name="SnWorker")
        self.worker.start()

    def _check_worker(self):
        """工作线程异常退出则自动重启 (补齐与 USB 监听的对称保护)."""
        if self.worker is not None and not self.worker.is_alive():
            _ev.warn("worker_restart")
            self.add_log("WARN: 读取线程已停止, 正在重启 ...", "warn")
            try:
                self._start_worker()
            except Exception as e:
                _ev.error("worker_restart_failed",
                          err="%s: %s" % (type(e).__name__, e))

    def _worker_loop(self):
        while not self._stop.is_set():
            # 先清掉积压的重复触发, 只保留最新的一次.
            # 否则重试窗口阻塞期间排队的每个事件都会再触发一轮重试, 形成连锁延迟.
            item = None
            try:
                while True:
                    item = self.task_queue.get_nowait()
            except queue.Empty:
                pass
            if item is None:
                try:
                    item = self.task_queue.get(timeout=SAFETY_POLL)
                except queue.Empty:
                    pass
            if self._stop.is_set():
                break
            trigger = "poll"
            if isinstance(item, (tuple, list)) and len(item) > 1:
                trigger = item[1]
            try:
                self._reconcile_once(trigger)
            except Exception as e:
                _ev.error("reconcile_exception", trigger=trigger,
                          err="%s: %s" % (type(e).__name__, e))

    def _reconcile_once(self, trigger):
        t_start = time.time()
        # 事件驱动绕开 WMI 节流; 兜底轮询保持节流以节省 CPU
        wmi_force = (trigger != "poll")

        sns, info = L.enum_sns(wmi_force=wmi_force)

        # 事件触发但没读到 -> 在时间窗口内重试.
        # 设备插入后分两阶段枚举: 先出现位置 ID (5&181816B0&0&1), 约 3 秒后
        # 才换成真 SN. 窗口取 2.0s: 在不阻塞后续产品检测的前提下, 能覆盖
        # 大部分设备就绪时间; 剩余情况由 1.5s 兜底轮拾取. 操作员插入
        # 节奏最短 7.8s, 2.0s 窗口占比约 26%, 与 v1.8 体感一致.
        if trigger == "event" and not sns and self._present_sn is None:
            bad_value = None
            bad_streak = 0
            locid_value = None
            while not self._stop.is_set() and \
                    (time.time() - t_start) < RETRY_WINDOW_MAX:
                time.sleep(EVENT_RETRY_GAP)
                sns, info = L.enum_sns(wmi_force=True)
                if sns:
                    break
                if L.is_location_id(info.get("bad_value")):
                    # 位置 ID: 枚举过渡态, 序列号马上就会就绪, 继续等
                    locid_value = info["bad_value"]
                    bad_streak = 0
                    bad_value = None
                    continue
                # 设备已不在位 (wmi_rows=0 且无非法值): 操作员在窗口内拔走,
                # 无需继续等, 立即放弃本次事件, 避免白耗窗口剩余时间.
                if not info.get("sns") and not info.get("bad_value") and \
                        info.get("wmi_rows", 0) == 0:
                    break
                # 仅当"同一个非空非法序列号"持续出现才判定真的读不出;
                # 序列号暂未就绪 (empty) 属枚举过渡态, 继续等.
                if L.has_invalid_serial(info):
                    if info.get("bad_value") == bad_value:
                        bad_streak += 1
                    else:
                        bad_value = info.get("bad_value")
                        bad_streak = 1
                    if bad_streak >= NOSN_PERSIST_CHECKS:
                        self._report_no_valid_sn(info)
                        bad_value = None
                        break
                else:
                    bad_streak = 0
                    bad_value = None
            # 整个窗口走完仍未读到 -> 放弃本次事件.
            # 若只见位置 ID (过渡态), 不得报"无序列号", 交给 1.5s 兜底轮拾取;
            # 若是真非法值, has_invalid_serial 已在循环内提前报过.

        # 多设备冲突: 只在"首次进入冲突"时弹窗 (worker 侧去重, 避免 1.5s 一次刷屏)
        if len(sns) > 1:
            if not self._dual_sent:
                self._dual_sent = True
                self.result_queue.put(("dual", sns[0], sns[1], t_start))
            return

        # 冲突已解除 -> 主动通知界面清除冲突态.
        # (v1.8 的缺陷: _dual_active 只在 arrival/removal 时复位, 冲突解除走
        #  "状态未变化"分支不发消息, 导致界面永久停在红色冲突态且后续冲突不再弹窗)
        if self._dual_sent:
            self._dual_sent = False
            self.result_queue.put(("dual_clear", time.time()))

        sn = sns[0] if sns else None

        if sn and sn != self._present_sn:
            # 写入前二次确认: SN 必须稳定.
            #   - 复核同一 SN           -> 写入
            #   - 复核为空 (设备已拔走) -> 快速复核几次, 仍为空则已读到就写入
            #   - 复核是其它 SN         -> 归属不明, 丢弃
            time.sleep(STABLE_CHECK_GAP)
            confirmed = False
            for attempt in range(CONFIRM_GONE_RETRY + 1):
                if attempt > 0:
                    time.sleep(CONFIRM_GONE_GAP)
                again, _i = L.enum_sns(wmi_force=True)
                if set(again) == set(sns):
                    confirmed = True
                    break
                if again:
                    _ev.warn("discard_sn_changed", first=sn,
                             second=";".join(sorted(again)))
                    return
                # again 为空: 设备被拔走, 继续短复核
            if not L.is_valid_sn(sn):
                return
            self._present_sn = sn
            path = L.write_sn_file(sn)
            self.result_queue.put(("arrival", sn, path, t_start))
        elif sn is None and self._present_sn is not None:
            gone = self._present_sn
            self._present_sn = None
            self.result_queue.put(("removal", gone, t_start))

    def _report_no_valid_sn(self, info):
        """设备在位但序列号持续无效 (未烧录/损坏), 提示且限频."""
        now = time.time()
        if now - self._last_no_sn_alert < NO_SN_ALERT_GAP:
            return
        self._last_no_sn_alert = now
        sample = info.get("bad_value") or "-"
        locid = L.is_location_id(sample)
        _ev.warn("device_no_valid_sn", sample=sample, kind="locid" if locid else "invalid",
                 hid_count=info.get("hid_count", 0), wmi_rows=info.get("wmi_rows", 0),
                 expect="%d_chars" % L.SN_LEN)
        self.nosn_cnt += 1
        self.result_queue.put(("nosn", sample, time.time(), locid))

    # --- 主线程轮询 ---

    def _poll(self):
        try:
            # 1. USB 事件 -> 去抖触发核对
            had_event = False
            try:
                while True:
                    self.event_queue.get_nowait()
                    had_event = True
                    self._usb_n += 1
            except queue.Empty:
                pass
            if had_event:
                self._schedule_reconcile()

            # 2. 工作线程结果 -> 主线程更新 UI
            try:
                while True:
                    self._apply_result(self.result_queue.get_nowait())
            except queue.Empty:
                pass

            # 3. watchdog
            self._check_watcher()
            self._check_worker()
        except Exception as e:
            _ev.error("poll_exception", err="%s: %s" % (type(e).__name__, e))
        finally:
            if not self._stop.is_set():
                self.root.after(POLL_MS, self._poll)

    def _schedule_reconcile(self, delay=DEBOUNCE_MS):
        try:
            if self._debounce_job is not None:
                self.root.after_cancel(self._debounce_job)
        except Exception:
            pass
        self._debounce_job = self.root.after(delay, self._enqueue_reconcile)

    def _enqueue_reconcile(self):
        self._debounce_job = None
        self.task_queue.put(("reconcile", "event"))

    # --- 结果处理 (主线程) ---

    def _apply_result(self, msg):
        kind = msg[0]
        now = time.time()
        if kind == "arrival":
            _, sn, path, t0 = msg
            self._display_sn = sn
            self.read_cnt += 1
            if path:
                self.ok_cnt += 1
            else:
                self.fail_cnt += 1
            total_ms = (now - t0) * 1000.0
            self._last_read_ms = total_ms
            self.set_state(True, sn)
            if path:
                msg_txt = "SN=%s  已写入 (%.0f ms)" % (sn, total_ms)
                if total_ms > SLOW_READ_MS:
                    msg_txt += "  [偏慢]"
                    # 超过 6s 属异常 (正常两阶段枚举约 2.6~3.2s), 记一条异常便于事后追查
                    if total_ms > 6000:
                        _ev.warn("slow_read", sn=sn, ms=round(total_ms),
                                 note="much_slower_than_3s_baseline")
                self.add_log(msg_txt)
            else:
                self.add_log("SN=%s  已读到但写文件失败!" % sn, "error")
        elif kind == "removal":
            self._display_sn = None
            self.set_state(False)
        elif kind == "dual":
            _, sn1, sn2, _t = msg
            self._alert_dual_device(sn1, sn2)
        elif kind == "dual_clear":
            # 冲突解除 -> 恢复常态显示 (修复冲突态卡死)
            if self._display_sn:
                self.set_state(True, self._display_sn)
            else:
                self.set_state(False)
        elif kind == "nosn":
            _, sample, _t, locid = msg
            if locid:
                # 整段窗口只见位置 ID => 该设备确实没烧序列号 (多为治具/坏固件)
                self.add_log(
                    "设备无序列号 (Windows 只给出位置 ID, 非过渡态): %s\n"
                    "请确认是否治具或固件未烧录" % sample, "error")
            else:
                self.add_log("设备在位但序列号格式非法: %s" % sample, "error")
            self._show_no_sn_state()

    def _show_no_sn_state(self):
        """提示设备在位但无有效序列号 (区别于'没插设备')."""
        self.dot.config(fg="orange")
        self.lbl_state.config(text="⚠ 设备无序列号")
        self.sn_var.set("—")
        self._update_counters()

    def _alert_dual_device(self, sn1, sn2):
        self.set_state(True, "冲突: %s | %s" % (sn1, sn2))
        self.stats_label.config(fg="red")
        msg = "⚠ 检测到 2 台设备同时插入:\n%s\n%s\n请拔出一台后重试" % (sn1, sn2)
        self.add_log("ALERT: " + msg, "error")
        _ev.warn("dual_device", sn1=sn1, sn2=sn2)
        try:
            messagebox.showwarning("双设备冲突", msg)
        except Exception:
            pass

    # --- 计数器 ---

    def _update_counters(self):
        self.stats_var.set("读取: %d  写成功: %d  写失败: %d  无序列号: %d" % (
            self.read_cnt, self.ok_cnt, self.fail_cnt, self.nosn_cnt))

    # --- 界面日志 (只显示 warning/error) ---

    def add_log(self, msg, level="info"):
        self.log.config(state="normal")
        ts = time.strftime("[%H:%M:%S] ")
        self.log.insert(tk.END, ts + msg + "\n")
        if level == "error":
            self.log.tag_add("err", "end-2l", "end-1l")
            self.log.tag_config("err", foreground="red")
        lines = int(self.log.index('end-1c').split('.')[0])
        if lines > 30:
            self.log.delete('1.0', '2.0')
        self.log.see(tk.END)
        self.log.config(state="disabled")

    # --- 状态灯 ---

    def set_state(self, present, sn=""):
        if present:
            self.dot.config(fg="green")
            self.lbl_state.config(text="已接入")
            self.sn_var.set(sn)
            self.stats_label.config(fg="#333")
        else:
            self.dot.config(fg="gray")
            self.lbl_state.config(text="等待插入 045E:0B50 ...")
            self.sn_var.set("—")
            self.stats_label.config(fg="#333")
        self._update_counters()

    # --- 交互 ---

    def copy_sn(self):
        sn = self.sn_var.get()
        if sn not in ("—", ""):
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(sn)
                self.add_log("已复制: %s" % sn)
            except Exception:
                pass

    def hide_to_tray(self):
        self.root.iconify()

    def quit_all(self):
        _ev.note("exit", ver=VERSION, read_ok=self.ok_cnt, write_fail=self.fail_cnt,
                 nosn=self.nosn_cnt, usb_events=self._usb_n)
        self._stop.set()
        self.task_queue.put(("reconcile", "stop"))
        try:
            if self.watcher:
                self.watcher.stop()
        except Exception:
            pass
        self.root.destroy()


def _install_crash_hook():
    def _dump(exc_type, exc_value, exc_tb):
        try:
            _ev.error("crash", err="%s: %s" % (exc_type.__name__, exc_value),
                      tb=traceback.format_exc()[-2000:])
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _dump


if __name__ == "__main__":
    # 单实例互斥量 (全局命名空间, 防多开)
    if _HAS_WIN32_MUTEX:
        MUTEX_NAME = "Global\\FionaSNReader_v19_SingleInstance"
        try:
            mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(
                        0, "程序已在运行中", "Fiona-SN Reader v%s" % VERSION, 0x40)
                except Exception:
                    pass
                sys.exit(0)
        except Exception:
            pass

    _ev.init()
    _install_crash_hook()
    r = tk.Tk()
    app = TrayApp(r)
    r.mainloop()