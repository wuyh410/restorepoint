#!/usr/bin/env python3
"""Desktop entry point; privileged helpers run before loading Qt."""
import sys


def runtime_info():
    import hashlib
    from desktop.backend import engine_command
    from desktop.runtime import compiled_application, helper_source, resource_root
    helpers = {}
    for name in ('snapshot.py', 'portable.py', 'gui_bridge.py'):
        path = helper_source(name)
        helpers[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return {'compiled': compiled_application(), 'resources': str(resource_root()),
            'engine_command': engine_command('gui_bridge', ['doctor']), 'helpers': helpers}


def run_engine(name, arguments):
    if sys.platform != 'linux':
        raise SystemExit('Local system engines require Linux.')
    sys.argv = [name + '.py'] + arguments
    # Static imports let Nuitka include the engines as compiled modules. They
    # are entered before Qt is imported, including when started through sudo.
    if name == 'snapshot':
        import snapshot
        snapshot.main()
    elif name == 'portable':
        import portable
        portable.main()
    elif name == 'gui_bridge':
        import gui_bridge
        gui_bridge.main()
    else:
        raise SystemExit('Unknown engine')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--runtime-info']:
        import json
        from pathlib import Path
        payload = json.dumps(runtime_info(), ensure_ascii=True, indent=2)
        if len(sys.argv) == 3:
            Path(sys.argv[2]).write_text(payload, encoding='utf-8')
        else:
            print(payload)
    elif len(sys.argv) >= 3 and sys.argv[1] == '--engine':
        try:
            run_engine(sys.argv[2], sys.argv[3:])
        except KeyboardInterrupt:
            raise SystemExit(130)
        except Exception as exc:
            print('错误：' + str(exc), file=sys.stderr)
            raise SystemExit(1)
    else:
        from desktop.window import main
        raise SystemExit(main())
