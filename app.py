#!/usr/bin/env python3
"""Desktop entry point; privileged helpers run before loading Qt."""
import sys


def runtime_info():
    import hashlib
    from desktop.backend import engine_command
    from desktop.runtime import compiled_application, helper_source, resource_root
    from desktop.credentials import system_backend
    helpers = {}
    for name in ('snapshot.py', 'portable.py', 'gui_bridge.py'):
        path = helper_source(name)
        helpers[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return {'compiled': compiled_application(), 'resources': str(resource_root()),
            'credential_backend': type(system_backend()).__module__,
            'engine_command': engine_command('gui_bridge', ['doctor']), 'helpers': helpers}


def credential_self_test():
    """Exercise a disposable credential in Windows CI, then remove it."""
    import secrets
    import tempfile
    from desktop.credentials import CredentialStore
    from desktop.models import Profile
    if sys.platform != 'win32':
        raise SystemExit('Credential self-test requires Windows.')
    with tempfile.TemporaryDirectory(prefix='restorepoint-credential-check-') as directory:
        store = CredentialStore(directory)
        profile = Profile(host='isolated.invalid')
        value = secrets.token_hex(24)
        try:
            store.save(profile, {'login': value})
            if store.load(profile) != {'login': value}:
                raise RuntimeError('Credential roundtrip failed')
        finally:
            store.delete(profile)
        if store.load(profile):
            raise RuntimeError('Credential cleanup failed')
    return {'ok': True}


def key_self_test():
    """Check bundled key generation and permissions in a disposable folder."""
    import secrets
    import tempfile
    from desktop.models import Profile
    from desktop.ssh_keys import prepare_key
    class Context:
        def check(self): pass
        def get_credential(self, kind):
            return secret if kind == 'setup_key' else None
    with tempfile.TemporaryDirectory(prefix='restorepoint-key-check-') as directory:
        for encrypted in (False, True):
            secret = secrets.token_urlsafe(24)
            profile = Profile(host='encrypted.invalid' if encrypted else 'plain.invalid')
            key = prepare_key(directory, profile, Context(), encrypted)
            if not key.created or not key.public.startswith('ssh-ed25519 '):
                raise RuntimeError('Key generation failed')
            repeated = prepare_key(directory, profile, Context(), encrypted)
            if repeated.created or repeated.fingerprint != key.fingerprint:
                raise RuntimeError('Key reuse failed')
            if sys.platform == 'win32':
                from desktop.windows_security import private_acl_valid
                if not private_acl_valid(key.path):
                    raise RuntimeError('Private key access control failed')
    return {'ok': True}


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
    if sys.argv[1:2] == ['--key-self-test'] and len(sys.argv) == 3:
        import json
        from pathlib import Path
        Path(sys.argv[2]).write_text(json.dumps(key_self_test()), encoding='utf-8')
    elif sys.argv[1:2] == ['--credential-self-test'] and len(sys.argv) == 3:
        import json
        from pathlib import Path
        Path(sys.argv[2]).write_text(json.dumps(credential_self_test()), encoding='utf-8')
    elif sys.argv[1:2] == ['--runtime-info']:
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
