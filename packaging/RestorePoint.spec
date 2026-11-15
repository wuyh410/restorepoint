# PyInstaller specification. Run on Linux to produce Linux, Windows for Windows.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).parent
data = [(str(root / name), '.') for name in ('snapshot.py', 'portable.py', 'gui_bridge.py', 'GUI.md', 'README.md', 'LOCAL.md', 'REMOTE.md')]
data += [(str(root / 'assets'), 'assets')]
for dependency in ('PySide6', 'PySide6_Essentials', 'shiboken6', 'paramiko', 'cryptography'):
    data += copy_metadata(dependency)
hidden = ['tarfile', 'runpy']
if sys.platform == 'linux':
    hidden += ['fcntl']
a = Analysis([str(root / 'app.py')], pathex=[str(root)], binaries=[], datas=data,
             hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQml', 'PySide6.QtQuick'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='RestorePoint',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=(sys.platform != 'win32'), disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='RestorePoint')
