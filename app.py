#!/usr/bin/env python3
"""Desktop entry point; privileged helpers run before loading Qt."""
from pathlib import Path
import runpy
import sys


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--engine':
        name = sys.argv[2]
        if name not in ('snapshot', 'portable', 'gui_bridge'):
            raise SystemExit('Unknown engine')
        root = resource_root()
        sys.path.insert(0, str(root))
        sys.argv = [str(root / (name + '.py'))] + sys.argv[3:]
        runpy.run_path(sys.argv[0], run_name='__main__')
    else:
        from desktop.window import main
        raise SystemExit(main())
