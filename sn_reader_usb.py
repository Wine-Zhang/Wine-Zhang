#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fiona-SN Reader USB 事件监听层.

基于 pywin32 创建隐藏顶层窗口并运行独立消息循环, 监听 WM_DEVICECHANGE 广播,
在设备插入/拔出时回调通知上层.

不使用 ctypes WNDPROC, 规避 ucrtbase 0xc0000409 闪退风险.
"""

import threading

try:
    import win32gui
    import win32con
    _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False

WM_DEVICECHANGE = 0x0219
_CLASS_NAME = "FionaSnReaderUsbWatcher"


def is_available():
    """返回 pywin32 USB 监听能力的可用性."""
    return _HAS_WIN32


class UsbEventWatcher:
    """在后台线程中监听 USB 设备变更事件.

    on_change 在监听线程中被调用 (无参数), 仅应做轻量操作 (例如入队),
    不得直接操作 tkinter 控件.
    """

    def __init__(self, on_change):
        self._on_change = on_change
        self._hwnd = None
        self._thread = None
        self._running = False
        self._ready = threading.Event()

    def start(self):
        if not _HAS_WIN32:
            return False
        if self._running:
            return True
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="UsbEventWatcher")
        self._thread.start()
        self._ready.wait(timeout=3)
        return self._hwnd is not None

    def _run(self):
        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = win32gui.GetModuleHandle(None)
            wc.lpszClassName = _CLASS_NAME
            wc.lpfnWndProc = self._wndproc
            try:
                win32gui.RegisterClass(wc)
            except Exception:
                pass
            self._hwnd = win32gui.CreateWindow(
                wc.lpszClassName, _CLASS_NAME, 0,
                0, 0, 0, 0, 0, 0, wc.hInstance, None)
        except Exception:
            self._hwnd = None
            self._ready.set()
            return
        self._ready.set()
        try:
            win32gui.PumpMessages()
        except Exception:
            pass

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_DEVICECHANGE:
                try:
                    self._on_change()
                except Exception:
                    pass
                return True
            if msg == win32con.WM_CLOSE:
                win32gui.DestroyWindow(hwnd)
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def is_alive(self):
        """监听线程是否仍在运行 (供上层 watchdog 使用)."""
        return bool(self._thread and self._thread.is_alive())

    def stop(self):
        self._running = False
        try:
            if self._hwnd:
                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._hwnd = None
        self._thread = None