# -*- mode: python ; coding: utf-8 -*-
"""cbstudio.spec — CBStudio 打包配置（PyInstaller 6.x）

形态（与现场约定一致）：

* **单目录**：产出 ``CBStudio.exe`` + ``_internal/``（Python 运行时），
  exe 与 ``web/ sdk/ scenarios/ config.json can_engine.py`` 同级；
* **保留控制台**：双击能看到地址/串口/CAN 状态与报错，Ctrl+C 退出；
* **外置可改**：``web/ scenarios/ config.json data/ sdk/ can_engine.py`` 全部留在
  exe 外面，现场改配置、改前端、换 SDK 都不用重新打包；
  其中 ``web/`` 也顺手打进包里做兜底（外置缺失时才用包内副本）。

不打包的东西及原因：

* ``can_engine.py``：必须由**外部 32 位 Python** 当脚本执行（ZLG SDK 只有 32 位 DLL），
  冻进 exe 后子进程就找不到了，所以它留在磁盘上；
* ``sdk/zlgcan``：驱动 DLL + kerneldlls，被 32 位子进程按目录加载；
* ``platform-tools/adb.exe``、32 位解释器：外部工具，按需另放。

构建：``build_exe.bat``（内部就是 ``pyinstaller cbstudio.spec``）。
"""

import os
import re

ROOT = os.path.abspath(SPECPATH)


def _read_version():
    """从源码里取版本号，避免 exe 属性与界面版本号各写一份。"""
    with open(os.path.join(ROOT, 'studio', 'studio_app.py'), encoding='utf-8') as handle:
        match = re.search(r'^VERSION\s*=\s*"([^"]+)"', handle.read(), re.M)
    return match.group(1) if match else '0.0.0'


VERSION = _read_version()
_quad = [int(part) if part.isdigit() else 0 for part in VERSION.split('.')][:4]
_quad += [0] * (4 - len(_quad))

VERSION_FILE = os.path.join(workpath, 'cbstudio_version.txt')
with open(VERSION_FILE, 'w', encoding='utf-8') as _handle:
    _handle.write('''VSVersionInfo(
  ffi=FixedFileInfo(filevers=%r, prodvers=%r, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404b0', [
        StringStruct('CompanyName', 'CBStudio'),
        StringStruct('FileDescription', 'CBStudio 台架剧本工作台'),
        StringStruct('FileVersion', '%s'),
        StringStruct('InternalName', 'CBStudio'),
        StringStruct('OriginalFilename', 'CBStudio.exe'),
        StringStruct('ProductName', 'CBStudio'),
        StringStruct('ProductVersion', '%s')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
''' % (tuple(_quad), tuple(_quad), VERSION, VERSION))

a = Analysis(
    [os.path.join(ROOT, 'app.py')],
    pathex=[ROOT],
    binaries=[],
    # 前端兜底副本（外置 web/ 存在时不会被用到）
    datas=[(os.path.join(ROOT, 'web'), 'web')],
    hiddenimports=[
        'serial',
        'serial.tools',
        'serial.tools.list_ports',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 主进程实际只依赖 pyserial；其余不进包，产物更小、启动更快
    excludes=['numpy', 'PIL', 'openpyxl', 'tkinter', 'matplotlib', 'pandas'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CBStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                 # 保留控制台窗口（排障用）
    version=VERSION_FILE,         # exe 属性里的版本号（取自 studio/studio_app.py）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CBStudio',
    contents_directory='_internal',
)
