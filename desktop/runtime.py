"""Resource and child-process paths for source and compiled Qt applications."""
from pathlib import Path
import shutil
import sys


def nuitka_application():
    try:
        __compiled__
    except NameError:
        return False
    return True


def compiled_application():
    return nuitka_application() or bool(getattr(sys, 'frozen', False))


def application_executable():
    if nuitka_application():
        # Some standalone CPython flavors leave sys.executable pointing at a
        # nonexistent bundled "python". argv[0] names the actual application.
        value = sys.argv[0]
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = Path(shutil.which(value) or candidate.absolute())
        if candidate.is_file():
            return str(candidate.resolve())
        fallback = resource_root() / ('RestorePoint.exe' if sys.platform == 'win32' else 'RestorePoint')
        if fallback.is_file():
            return str(fallback.resolve())
        raise RuntimeError('Cannot locate the compiled application executable.')
    return sys.executable


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))


def helper_source(name):
    if name not in ('snapshot.py', 'portable.py', 'gui_bridge.py'):
        raise ValueError('Unknown Linux helper')
    # Nuitka compiles executable Python modules, so separate text copies are
    # shipped only for uploading the original helpers to a Linux test machine.
    packaged = resource_root() / 'helpers' / (name + '.txt')
    return packaged if packaged.is_file() else resource_root() / name
