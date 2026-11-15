#!/usr/bin/env python3
"""Build the Qt application on the target OS, without running backup operations."""
from pathlib import Path
import os
import subprocess
import sys

root = Path(__file__).resolve().parent
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                '--distpath', str(root / 'dist'), '--workpath', str(root / 'build'),
                str(root / 'packaging/RestorePoint.spec')], cwd=root, check=True,
               env=dict(os.environ, PYINSTALLER_CONFIG_DIR=str(root / 'build/pyinstaller-cache')))
print('Application created in: ' + str(root / 'dist/RestorePoint'))
