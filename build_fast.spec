# -*- mode: python ; coding: utf-8 -*-
# Fiona-SN Reader v1.9 - 单文件打包 (onefile 模式)
# 关键点:
# 1. onefile 模式 -> 单 exe 便于分发给操作员
# 2. 排除未用的大型模块, 缩小体积
# 3. 仅收集实际使用的 pywin32 子模块
# 4. 启用 strip, 禁用 UPX (避免杀软误报与解压延迟)
# 修复记录: win32error 并非 pywin32 模块(不存在), 单实例需用标准库 winerror.

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    "sn_reader_logic",
    "sn_reader_usb",
    "sn_event",
    "queue",
    "threading",
    "traceback",
    "datetime",
    "tkinter",
    "tkinter.scrolledtext",
    "tkinter.messagebox",
    "win32gui",
    "win32con",
    "win32api",
    "win32event",
    "winerror",
    "hid",
    "win32com.client",
    "pythoncom",
]

win32_submodules = [m for m in collect_submodules("win32com") if not m.endswith(".gen_py")]
hiddenimports += win32_submodules

excludes = [
    "pystray",
    "PIL",
    "pillow",
    "matplotlib",
    "numpy",
    "scipy",
    "pandas",
    "IPython",
    "jupyter",
    "notebook",
    "test",
    "unittest",
    "doctest",
    "pdb",
    "profile",
]

a = Analysis(
    ["sn_reader.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure, optimize=2)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Fiona-SN-Reader-v1.9",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version_info.txt",
)